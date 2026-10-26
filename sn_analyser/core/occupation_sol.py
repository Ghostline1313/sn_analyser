# -*- coding: utf-8 -*-
"""
core/occupation_sol.py
----------------------
Occupation du sol ESA WorldCover 10 m (version 2021 v200, repli 2020 v100),
servie publiquement par Microsoft Planetary Computer (collection
"esa-worldcover", GeoTIFF Cloud-Optimises : seules les tuiles couvrant la
zone sont lues).

Sert a corriger les risques qui melangeaient bati et milieu naturel :
  - degradation des terres / biodiversite : indices de vegetation calcules
    UNIQUEMENT sur les pixels naturels ou agricoles (un toit ou une route a
    un NDVI faible qui etait lu comme une "perte de vegetation") ;
  - urbanisation : part reelle de surfaces baties (classe 50) au lieu du
    seul NDBI Landsat, qui confond bati et sable/sol nu en zone sahelienne.
"""

import os
import requests

STAC_SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"

CLASSES = {
    10: "arbres", 20: "arbustes", 30: "prairies", 40: "cultures", 50: "bati",
    60: "sol_nu", 70: "neige", 80: "eau", 90: "zones_humides", 95: "mangroves", 100: "mousses",
}
LIBELLES = {
    "arbres": "couvert arbore", "arbustes": "arbustes", "prairies": "prairies/herbacees",
    "cultures": "cultures", "bati": "bati", "sol_nu": "sol nu/sable", "eau": "eau",
    "zones_humides": "zones humides", "mangroves": "mangroves", "neige": "neige", "mousses": "mousses",
}
# Milieux pris en compte pour la vegetation (degradation / biodiversite) :
# tout sauf bati (50), eau (80) et neige (70).
CLASSES_NATURELLES = (10, 20, 30, 40, 60, 90, 95, 100)
# Habitats (biodiversite) : vegetation naturelle + zones humides/mangroves.
CLASSES_HABITAT = (10, 20, 30, 90, 95, 100)
CLASSE_BATI = 50
CLASSE_EAU = 80


def _items(bbox):
    from .landsat import _sign_href
    resp = requests.post(STAC_SEARCH_URL, json={"collections": ["esa-worldcover"], "bbox": bbox, "limit": 50}, timeout=30)
    resp.raise_for_status()
    feats = resp.json().get("features", [])
    if not feats:
        raise RuntimeError("ESA WorldCover : aucune tuile pour cette zone")
    v2 = [f for f in feats if str(f["properties"].get("esa_worldcover:product_version", "")).startswith("2")]
    choisis = v2 or feats
    version = "2021 (v200)" if v2 else "2020 (v100)"
    urls = ["/vsicurl/" + _sign_href(f["assets"]["map"]["href"], "esa-worldcover") for f in choisis]
    return urls, version


def occupation_du_sol(bbox, geojson_path, geotransform, projection, forme, work_dir, prefix):
    """Retourne l occupation du sol de la zone :
      - "classes" : tableau numpy (codes WorldCover) ALIGNE pixel a pixel sur
        la grille des rasters d indices (geotransform/projection/forme), par
        resampling "mode" (classe majoritaire de chaque pixel de 30 m) ;
      - "fractions" : dict {nom_classe: part 0-1} calculees a 10 m (resolution
        native) a l interieur exact du polygone de la zone ;
      - "version", "source".
    """
    from osgeo import gdal
    import numpy as np

    gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF")
    gdal.SetConfigOption("GDAL_HTTP_TIMEOUT", "60")

    from .landsat import invalider_jetons
    vrt = os.path.join(work_dir, prefix + "_worldcover.vrt")
    for tentative in range(3):
        urls, version = _items(bbox)
        res = gdal.BuildVRT(vrt, urls)
        if res is not None:
            res = None
            if os.path.exists(vrt) and gdal.Open(vrt) is not None:
                break
        invalider_jetons()  # jeton expire ou refuse : on en redemande un
        import time
        time.sleep(3)
    else:
        raise RuntimeError("tuiles ESA WorldCover illisibles (" + (gdal.GetLastErrorMsg() or "acces refuse") + ")")

    hauteur, largeur = forme
    gt = geotransform
    xmin, ymax = gt[0], gt[3]
    xmax, ymin = xmin + gt[1] * largeur, ymax + gt[5] * hauteur
    aligne = os.path.join(work_dir, prefix + "_worldcover_30m.tif")
    gdal.Warp(aligne, vrt, outputBounds=(xmin, ymin, xmax, ymax), width=largeur, height=hauteur,
              dstSRS=projection, resampleAlg="mode", dstNodata=0)
    classes = gdal.Open(aligne).ReadAsArray().astype(np.int16)

    natif = os.path.join(work_dir, prefix + "_worldcover_10m.tif")
    gdal.Warp(natif, vrt, cutlineDSName=geojson_path, cropToCutline=True, dstSRS="EPSG:32628",
              xRes=10, yRes=10, resampleAlg="near", dstNodata=0)
    a10 = gdal.Open(natif).ReadAsArray()
    valides = a10[a10 > 0]
    fractions = {}
    if valides.size:
        codes, nb = np.unique(valides, return_counts=True)
        for c, n in zip(codes, nb):
            nom = CLASSES.get(int(c))
            if nom:
                fractions[nom] = fractions.get(nom, 0.0) + n / float(valides.size)

    for f in (vrt, aligne, natif):
        try:
            os.remove(f)
        except OSError:  # nosec B110 - erreur non bloquante, ignoree volontairement
            pass  # nosec B110

    return {"classes": classes, "fractions": fractions, "version": version,
            "source": "ESA WorldCover 10 m " + version}


def resume_fractions(fractions, seuil=0.02):
    """Texte court : '62 % bati, 18 % sol nu/sable, ...' (classes >= seuil)."""
    items = sorted(((v, k) for k, v in fractions.items() if v >= seuil), reverse=True)
    return ", ".join("%.0f %% %s" % (v * 100, LIBELLES.get(k, k)) for v, k in items)


def densite_locale(masque, rayon=1):
    """Part de pixels True dans une fenetre (2*rayon+1)^2 autour de chaque
    pixel (moyenne glissante, numpy pur) -- donne une carte continue de la
    densite de bati au lieu d une carte binaire bati/non bati."""
    import numpy as np
    a = masque.astype(np.float32)
    k = 2 * rayon + 1
    p = np.pad(a, rayon, mode="edge")
    c = np.cumsum(np.cumsum(p, axis=0), axis=1)
    c = np.pad(c, ((1, 0), (1, 0)))
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return s / float(k * k)
