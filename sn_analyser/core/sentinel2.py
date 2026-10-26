# -*- coding: utf-8 -*-
"""
core/sentinel2.py
-----------------
Composite annuel Sentinel-2 L2A (Planetary Computer, collection
"sentinel-2-l2a") a 10 m (ou 20 m pour les grandes zones), pour les annees
2017 et suivantes : pixels 9 fois plus petits que Landsat (30 m).

Le fichier produit a EXACTEMENT la meme structure que
core.landsat.telecharger_composite_annuel avec les bandes
["green", "blue", "red", "nir", "swir1", "swir2", "thermal1"] :
  - reflectances converties a l echelle Landsat Collection 2 Level-2
    (DN = (reflectance + 0.2) / 0.0000275), pour que core/ndvi, ndbi, ndwi,
    evi, savi, bsi, nbr, salinite fonctionnent sans modification ;
  - bande 7 = temperature de surface Landsat (seule source thermique),
    re-echantillonnee sur la grille Sentinel-2.

Masque nuages : bande SCL (classes 0, 1, 3, 8, 9, 10 exclues). Composite
median sur plusieurs dates. Correction de l offset radiometrique (-1000) des
produits de baseline >= 04.00 (depuis janvier 2022).
"""

import os
import requests

STAC_SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
BANDES_S2 = ["B03", "B02", "B04", "B08", "B11", "B12"]  # green, blue, red, nir, swir1, swir2
SCL_INVALIDES = (0, 1, 3, 8, 9, 10)
ANNEE_MIN = 2017


def resolution_adaptee(surface_km2):
    """10 m jusqu a 1 500 km2, 20 m jusqu a 6 000 km2, sinon None (Landsat)."""
    if surface_km2 <= 1500:
        return 10
    if surface_km2 <= 6000:
        return 20
    return None


def _rechercher(bbox, annee, nuages_max, limite):
    payload = {
        "collections": ["sentinel-2-l2a"], "bbox": bbox,
        "datetime": "%d-01-01T00:00:00Z/%d-12-31T23:59:59Z" % (annee, annee),
        "query": {"eo:cloud_cover": {"lt": nuages_max}},
        "sortby": [{"field": "eo:cloud_cover", "direction": "asc"}],
        "limit": limite,
    }
    r = requests.post(STAC_SEARCH_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json().get("features", [])


def _choisir_dates(items, max_dates):
    """Garde les max_dates dates les moins nuageuses (toutes les tuiles MGRS
    d une meme date sont conservees, pour couvrir les zones a cheval)."""
    par_date = {}
    for it in items:
        jour = it["properties"]["datetime"][:10]
        par_date.setdefault(jour, []).append(it)
    dates = sorted(par_date, key=lambda j: min(i["properties"].get("eo:cloud_cover", 100) for i in par_date[j]))
    choisis = []
    for j in dates[:max_dates]:
        choisis.extend(par_date[j])
    return choisis


def telecharger_composite_s2(bbox, annee, chemin_sortie, resolution=10, max_dates=6, nuages_max=30,
                             pulse=None):
    from osgeo import gdal
    import numpy as np
    import tempfile
    import shutil
    from .landsat import _sign_href, telecharger_composite_annuel

    if annee < ANNEE_MIN:
        raise RuntimeError("Sentinel-2 indisponible avant %d" % ANNEE_MIN)
    gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF")
    gdal.SetConfigOption("GDAL_HTTP_TIMEOUT", "60")

    items = _rechercher(bbox, annee, nuages_max, 100)
    if not items:
        items = _rechercher(bbox, annee, 70, 100)
    if not items:
        raise RuntimeError("Aucune scene Sentinel-2 pour " + str(annee))
    items = _choisir_dates(items, max_dates)

    tmp = tempfile.mkdtemp(prefix="s2_")
    piles = [[] for _ in BANDES_S2]
    geo = None
    nb_ok, erreurs = 0, []

    def _charger(it):
        """Telecharge les 6 bandes + SCL d une scene (execute dans un thread)."""
        try:
            base = float(str(it["properties"].get("s2:processing_baseline", "0")) or 0)
        except ValueError:
            base = 0.0
        offset = -1000.0 if base >= 4.0 else 0.0
        urls = ["/vsicurl/" + _sign_href(it["assets"][b]["href"], "sentinel-2-l2a") for b in BANDES_S2 + ["SCL"]]
        arrs, g = [], None
        for k, u in enumerate(urls):
            ds = gdal.Warp("", u, format="MEM", outputBounds=bbox, outputBoundsSRS="EPSG:4326", dstSRS="EPSG:32628",
                           xRes=resolution, yRes=resolution,
                           resampleAlg="near" if k == len(urls) - 1 else "bilinear", dstNodata=0)
            if ds is None:
                raise RuntimeError("lecture impossible : " + (gdal.GetLastErrorMsg() or u[:80]))
            arrs.append(ds.ReadAsArray().astype(np.float32))
            if g is None:
                g = (ds.GetGeoTransform(), ds.GetProjection(), ds.RasterXSize, ds.RasterYSize)
            ds = None
        scl = arrs[-1]
        invalide = np.isin(scl, SCL_INVALIDES)
        bandes = []
        for i in range(len(BANDES_S2)):
            a = arrs[i]
            refl = (a + offset) / 10000.0
            refl[(a == 0) | invalide] = np.nan
            bandes.append(refl)
        return g, bandes

    from concurrent.futures import ThreadPoolExecutor
    import time as _time
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            from .landsat import avec_reessai
            futurs = {pool.submit(avec_reessai, _charger, it): it for it in items}
            restants = set(futurs)
            while restants:
                faits = [f for f in restants if f.done()]
                if not faits:
                    if pulse:
                        pulse()
                    _time.sleep(0.05)
                    continue
                for f in faits:
                    restants.discard(f)
                    try:
                        g, bandes = f.result()
                    except Exception as exc:
                        erreurs.append((futurs[f]["id"], repr(exc)))
                        continue
                    if geo is None:
                        geo = g
                    if (g[2], g[3]) != (geo[2], geo[3]):
                        erreurs.append((futurs[f]["id"], "grille differente"))
                        continue
                    for i in range(len(BANDES_S2)):
                        piles[i].append(bandes[i])
                    nb_ok += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if nb_ok == 0:
        raise RuntimeError("Aucune scene Sentinel-2 exploitable (" + "; ".join(e[1] for e in erreurs[:2]) + ")")

    gt, proj, largeur, hauteur = geo
    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(chemin_sortie, largeur, hauteur, 7, gdal.GDT_Int32, options=["COMPRESS=DEFLATE", "TILED=YES"])
    out.SetGeoTransform(gt)
    out.SetProjection(proj)
    couverture = None
    for i in range(len(BANDES_S2)):
        with np.errstate(invalid="ignore"):
            med = np.nanmedian(np.stack(piles[i]), axis=0)
        if couverture is None:
            couverture = 100.0 * np.isfinite(med).mean()
        dn = np.where(np.isfinite(med), np.clip(np.round((med + 0.2) / 0.0000275), 1, 65535), 0).astype(np.int32)
        b = out.GetRasterBand(i + 1)
        b.SetNoDataValue(0)
        b.WriteArray(dn)

    # Bande 7 : temperature de surface Landsat re-echantillonnee sur la grille S2
    thermique_ok = False
    try:
        tl = chemin_sortie + "_thermal_landsat.tif"
        telecharger_composite_annuel(bbox, annee, ["thermal1"], tl, nuages_max=40, max_scenes=6, pulse=pulse)
        tw = chemin_sortie + "_thermal_10m.tif"
        xmin, ymax = gt[0], gt[3]
        xmax, ymin = xmin + gt[1] * largeur, ymax + gt[5] * hauteur
        gdal.Warp(tw, tl, outputBounds=(xmin, ymin, xmax, ymax), width=largeur, height=hauteur,
                  dstSRS=proj, resampleAlg="bilinear", srcNodata=0, dstNodata=0)
        th = gdal.Open(tw).ReadAsArray().astype(np.int32)
        thermique_ok = bool((th > 0).any())
        for f in (tl, tw):
            try:
                os.remove(f)
            except OSError:
                pass
    except Exception as exc:
        erreurs.append(("thermique Landsat", repr(exc)))
        th = np.zeros((hauteur, largeur), dtype=np.int32)
    b = out.GetRasterBand(7)
    b.SetNoDataValue(0)
    b.WriteArray(th)
    out.FlushCache()
    out = None

    print("[sn_analyser] composite Sentinel-2 %d : %d/%d scene(s), %d m, couverture %.1f%%, thermique %s"
          % (annee, nb_ok, len(items), resolution, couverture or 0, "OK" if thermique_ok else "absente"))
    return {"chemin": chemin_sortie, "nb_scenes_utilisees": nb_ok, "resolution": resolution,
            "couverture_pct": couverture, "thermique": thermique_ok, "erreurs": erreurs,
            "source": "Sentinel-2 L2A %d m (%d scenes) + LST Landsat" % (resolution, nb_ok)}
