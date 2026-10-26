# -*- coding: utf-8 -*-
"""
core/relief_hydro.py
--------------------
Relief, hydrographie et sols de la zone d etude, a partir de sources libres
sans authentification :

  - Elevation / relief / pente / courbes de niveau : Copernicus DEM GLO-30
    (30 m, Planetary Computer, collection "cop-dem-glo-30").
  - Bassins versants, reseau hydrographique derive du relief, indice
    d humidite topographique (TCI/TWI) et aire drainee : GRASS r.watershed
    + r.stream.extract (fournis avec QGIS), calcules sur le MNT etendu d une
    marge autour de la zone (pour capter l amont).
  - Type de sol (classification WRB la plus probable) et taux d argile
    (0-5 cm) : SoilGrids 2.0 (ISRIC, 250 m, service WCS).

Toutes les fonctions renvoient des chemins de fichiers (pour chargement dans
QGIS) et des statistiques zonales (pour le score de risque hydrologique).
"""

import os
import re
import math
import requests

STAC_SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SOILGRIDS_WCS = "https://maps.isric.org/mapserv"
EPSG_METRIQUE = "EPSG:32628"  # UTM 28N, couvre le Senegal


def _gdal_options():
    from osgeo import gdal
    gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF")
    gdal.SetConfigOption("GDAL_HTTP_TIMEOUT", "60")


def resolution_adaptee(surface_km2):
    """Resolution de travail du MNT selon la taille de la zone (GRASS
    r.watershed sur une region entiere a 30 m serait tres long)."""
    if surface_km2 <= 2500:
        return 30
    if surface_km2 <= 15000:
        return 60
    return 120


def _bbox_marge(bbox, marge_deg):
    return [bbox[0] - marge_deg, bbox[1] - marge_deg, bbox[2] + marge_deg, bbox[3] + marge_deg]


def telecharger_mnt(bbox, chemin_sortie, resolution=30, marge_deg=0.0):
    """MNT Copernicus GLO-30 reprojete en UTM 28N (metres)."""
    from osgeo import gdal
    from .landsat import _sign_href
    _gdal_options()
    emprise = _bbox_marge(bbox, marge_deg)
    resp = requests.post(STAC_SEARCH_URL, json={"collections": ["cop-dem-glo-30"], "bbox": emprise, "limit": 100}, timeout=30)
    resp.raise_for_status()
    feats = resp.json().get("features", [])
    if not feats:
        raise RuntimeError("Copernicus DEM : aucune tuile pour cette zone")
    from .landsat import avec_reessai
    vrt = chemin_sortie + ".vrt"

    def _vrt():
        urls = ["/vsicurl/" + _sign_href(f["assets"]["data"]["href"], "cop-dem-glo-30") for f in feats]
        return gdal.BuildVRT(vrt, urls)
    vds = avec_reessai(_vrt)
    vds = None
    avec_reessai(gdal.Warp, chemin_sortie, vrt, outputBounds=emprise, outputBoundsSRS="EPSG:4326", dstSRS=EPSG_METRIQUE,
              xRes=resolution, yRes=resolution, resampleAlg="bilinear", dstNodata=-9999,
              creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
    try:
        os.remove(vrt)
    except OSError:  # nosec B110 - erreur non bloquante, ignoree volontairement
        pass  # nosec B110
    return chemin_sortie


def _decouper(raster, geojson_path, sortie, nodata):
    from osgeo import gdal
    gdal.Warp(sortie, raster, cutlineDSName=geojson_path, cropToCutline=True, dstNodata=nodata,
              creationOptions=["COMPRESS=DEFLATE"])
    return sortie


def _valeurs(raster, nodata):
    from osgeo import gdal
    import numpy as np
    a = gdal.Open(raster).ReadAsArray().astype(np.float64)
    m = np.isfinite(a) & (a != nodata)
    return a, m


def analyser_relief(mnt_large, geojson_path, work_dir, prefix):
    """Elevation, pente et ombrage (relief) sur la zone.

    :return: dict {mnt, pente, ombrage (chemins), alt_min, alt_moy, alt_max,
        denivele, pente_moy_pct, part_basse_altitude (alt < 5 m),
        part_plate (pente < 2 %)}
    """
    from osgeo import gdal
    import numpy as np
    mnt = _decouper(mnt_large, geojson_path, os.path.join(work_dir, prefix + "_mnt.tif"), -9999)
    pente = os.path.join(work_dir, prefix + "_pente.tif")
    gdal.DEMProcessing(pente, mnt, "slope", slopeFormat="percent", computeEdges=True)
    ombrage = os.path.join(work_dir, prefix + "_ombrage.tif")
    gdal.DEMProcessing(ombrage, mnt_large, "hillshade", zFactor=2,
                       computeEdges=True, multiDirectional=True)
    ombrage = _decouper(ombrage, geojson_path, os.path.join(work_dir, prefix + "_relief.tif"), 0)

    z, mz = _valeurs(mnt, -9999)
    p, mp = _valeurs(pente, -9999)
    mp &= mz
    if not mz.any():
        raise RuntimeError("MNT vide sur la zone")
    zz = z[mz]
    pp = p[mp] if mp.any() else np.array([0.0])
    return {
        "mnt": mnt, "pente": pente, "ombrage": ombrage,
        "alt_min": float(zz.min()), "alt_moy": float(zz.mean()), "alt_max": float(zz.max()),
        "denivele": float(zz.max() - zz.min()),
        "pente_moy_pct": float(pp.mean()),
        "part_basse_altitude": float((zz < 5.0).mean()),
        "part_plate": float((pp < 2.0).mean()),
    }


def intervalle_courbes(denivele):
    for seuil, pas in ((10, 1), (30, 2), (80, 5), (200, 10), (500, 20), (1000, 50)):
        if denivele <= seuil:
            return pas
    return 100


def courbes_de_niveau(mnt_zone, sortie_gpkg, intervalle):
    """Courbes de niveau (GeoPackage, champ 'altitude')."""
    from osgeo import gdal, ogr, osr
    if os.path.exists(sortie_gpkg):
        os.remove(sortie_gpkg)
    src = gdal.Open(mnt_zone)
    srs = osr.SpatialReference(wkt=src.GetProjection())
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(sortie_gpkg)
    lyr = ds.CreateLayer("courbes_niveau", srs, ogr.wkbLineString)
    lyr.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
    lyr.CreateField(ogr.FieldDefn("altitude", ogr.OFTReal))
    gdal.ContourGenerate(src.GetRasterBand(1), float(intervalle), 0.0, [], 1, -9999, lyr, 0, 1)
    n = lyr.GetFeatureCount()
    ds = None
    return sortie_gpkg, n


def bassins_et_reseau(mnt_large, geojson_path, work_dir, prefix, resolution, aire_seuil_km2):
    """Bassins versants, reseau hydrographique derive du MNT, TCI et aire
    drainee (GRASS r.watershed + r.stream.extract).

    :param aire_seuil_km2: aire drainee minimale pour former un cours d eau /
        un bassin elementaire.
    :return: dict {bassins (gpkg), reseau (gpkg), tci (tif), accumulation
        (tif), tci_moy, aire_drainee_max_km2, densite_drainage_km_km2,
        nb_bassins}
    """
    import processing
    from osgeo import gdal, ogr
    import numpy as np

    cellule_km2 = (resolution * resolution) / 1e6
    seuil = max(20, int(round(aire_seuil_km2 / cellule_km2)))
    out = lambda n: os.path.join(work_dir, prefix + "_" + n)

    res = processing.run("grass:r.watershed", {
        "elevation": mnt_large, "threshold": seuil, "convergence": 5, "memory": 500,
        "-s": False, "-m": False, "-4": False, "-a": True, "-b": False,
        "accumulation": out("accumulation.tif"), "basin": out("bassins.tif"), "tci": out("tci.tif"),
        "drainage": "TEMPORARY_OUTPUT", "stream": "TEMPORARY_OUTPUT",
        "half_basin": "TEMPORARY_OUTPUT", "length_slope": "TEMPORARY_OUTPUT",
        "slope_steepness": "TEMPORARY_OUTPUT", "spi": "TEMPORARY_OUTPUT",
        "GRASS_REGION_PARAMETER": None, "GRASS_REGION_CELLSIZE_PARAMETER": 0,
        "GRASS_RASTER_FORMAT_OPT": "", "GRASS_RASTER_FORMAT_META": "",
    })
    accumulation, bassins_r, tci = res["accumulation"], res["basin"], res["tci"]

    res2 = processing.run("grass:r.stream.extract", {
        "elevation": mnt_large, "accumulation": accumulation, "threshold": seuil,
        "d8cut": None, "mexp": 0, "stream_length": 0, "memory": 500,
        "stream_raster": "TEMPORARY_OUTPUT", "direction": "TEMPORARY_OUTPUT",
        "stream_vector": out("reseau_brut.gpkg"),
        "GRASS_REGION_PARAMETER": None, "GRASS_REGION_CELLSIZE_PARAMETER": 0,
        "GRASS_RASTER_FORMAT_OPT": "", "GRASS_RASTER_FORMAT_META": "",
        "GRASS_OUTPUT_TYPE_PARAMETER": 2, "GRASS_VECTOR_DSCO": "", "GRASS_VECTOR_LCO": "",
        "GRASS_VECTOR_EXPORT_NOCAT": False,
    })
    reseau_brut = res2["stream_vector"]

    # Bassins vectoriels (polygonisation), restreints a ceux qui touchent la zone
    bassins_brut = out("bassins_brut.gpkg")
    if os.path.exists(bassins_brut):
        os.remove(bassins_brut)
    processing.run("gdal:polygonize", {"INPUT": bassins_r, "BAND": 1, "FIELD": "bassin",
                                         "EIGHT_CONNECTEDNESS": False, "OUTPUT": bassins_brut})
    bassins = out("bassins_versants.gpkg")
    reseau = out("reseau_hydrographique.gpkg")
    for f in (bassins, reseau):
        if os.path.exists(f):
            os.remove(f)
    processing.run("native:extractbylocation", {"INPUT": bassins_brut, "PREDICATE": [0], "INTERSECT": geojson_path,
                                                  "OUTPUT": bassins})
    processing.run("native:clip", {"INPUT": reseau_brut, "OVERLAY": bassins, "OUTPUT": reseau})

    # Statistiques sur la zone
    tci_z = _decouper(tci, geojson_path, out("tci_zone.tif"), -9999)
    acc_z = _decouper(accumulation, geojson_path, out("accumulation_zone.tif"), -9999)
    t, mt = _valeurs(tci_z, -9999)
    a, ma = _valeurs(acc_z, -9999)
    tci_moy = float(t[mt].mean()) if mt.any() else None
    aire_max = float(np.abs(a[ma]).max() * cellule_km2) if ma.any() else None

    longueur_km = 0.0
    surface_km2 = 0.0
    zone = ogr.Open(geojson_path)
    zl = zone.GetLayer()
    from osgeo import osr
    cible = osr.SpatialReference()
    cible.ImportFromEPSG(32628)
    cible.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    geom_zone = None
    for f in zl:
        g = f.GetGeometryRef().Clone()
        src = zl.GetSpatialRef() or osr.SpatialReference()
        if not zl.GetSpatialRef():
            src.ImportFromEPSG(4326)
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        g.Transform(osr.CoordinateTransformation(src, cible))
        geom_zone = g if geom_zone is None else geom_zone.Union(g)
    if geom_zone is not None:
        surface_km2 = geom_zone.GetArea() / 1e6
        ds_r = ogr.Open(reseau)
        if ds_r is not None:
            lr = ds_r.GetLayer()
            srs_r = lr.GetSpatialRef()
            if srs_r is not None:
                srs_r.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            for f in lr:
                g = f.GetGeometryRef()
                if g is None:
                    continue
                g = g.Clone()
                if srs_r is not None and not srs_r.IsSame(cible):
                    g.Transform(osr.CoordinateTransformation(srs_r, cible))
                inter = g.Intersection(geom_zone)
                if inter is not None and not inter.IsEmpty():
                    longueur_km += inter.Length() / 1000.0
    ds_b = ogr.Open(bassins)
    nb_bassins = ds_b.GetLayer().GetFeatureCount() if ds_b is not None else 0

    return {
        "bassins": bassins, "reseau": reseau, "tci": tci, "accumulation": accumulation,
        "tci_moy": tci_moy, "aire_drainee_max_km2": aire_max,
        "densite_drainage_km_km2": (longueur_km / surface_km2) if surface_km2 > 0 else None,
        "longueur_reseau_km": longueur_km, "nb_bassins": nb_bassins,
        "seuil_km2": seuil * cellule_km2,
    }


def _wcs_soilgrids(carte, couverture, bbox, sortie):
    params = [("map", carte), ("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
              ("COVERAGEID", couverture), ("FORMAT", "image/tiff"),
              ("SUBSET", "long(%f,%f)" % (bbox[0], bbox[2])), ("SUBSET", "lat(%f,%f)" % (bbox[1], bbox[3])),
              ("SUBSETTINGCRS", "http://www.opengis.net/def/crs/EPSG/0/4326"),
              ("OUTPUTCRS", "http://www.opengis.net/def/crs/EPSG/0/4326")]
    r = requests.get(SOILGRIDS_WCS, params=params, timeout=90)
    r.raise_for_status()
    if r.content[:1] in (b"<", b"{"):
        raise RuntimeError("SoilGrids : " + r.content[:200].decode("utf-8", "replace"))
    with open(sortie, "wb") as f:
        f.write(r.content)
    return sortie


def legende_wrb(chemin_qml):
    """{code: nom WRB} lu dans le style officiel SoilGrids (data/soilgrids_wrb.qml)."""
    try:
        txt = open(chemin_qml, encoding="utf-8", errors="ignore").read()
    except OSError:
        return {}
    leg = {}
    for m in re.finditer(r"<paletteEntry[^>]*?/>", txt):
        e = m.group(0)
        v = re.search(r'value="(\d+)"', e)
        l = re.search(r'label="([^"]*)"', e)
        if v and l:
            leg[int(v.group(1))] = l.group(1)
    return leg


def analyser_sols(bbox, geojson_path, work_dir, prefix, chemin_qml):
    """Type de sol WRB le plus probable + argile 0-5 cm (SoilGrids 2.0).

    :return: dict {types_sol (tif, style a appliquer : chemin_qml),
        sol_dominant, repartition [(nom, part)], argile_moy_pct}
    """
    import numpy as np
    marge = 0.01
    emprise = _bbox_marge(bbox, marge)
    wrb = _wcs_soilgrids("/map/wrb.map", "MostProbable", emprise, os.path.join(work_dir, prefix + "_wrb_brut.tif"))
    argile = _wcs_soilgrids("/map/clay.map", "clay_0-5cm_mean", emprise, os.path.join(work_dir, prefix + "_argile_brut.tif"))
    wrb_z = _decouper(wrb, geojson_path, os.path.join(work_dir, prefix + "_types_sol.tif"), 255)
    arg_z = _decouper(argile, geojson_path, os.path.join(work_dir, prefix + "_argile.tif"), 0)

    leg = legende_wrb(chemin_qml)
    w, mw = _valeurs(wrb_z, 255)
    repartition = []
    if mw.any():
        codes, nb = np.unique(w[mw].astype(int), return_counts=True)
        total = float(nb.sum())
        repartition = sorted(((leg.get(int(c), "classe " + str(int(c))), n / total) for c, n in zip(codes, nb)),
                             key=lambda t: -t[1])
    a, ma = _valeurs(arg_z, 0)
    ma &= a > 0  # SoilGrids ne publie pas d argile en zone urbaine (pixels 0 / -1)
    argile_moy = float(a[ma].mean() / 10.0) if int(ma.sum()) >= 3 else None  # g/kg -> %
    return {
        "impermeabilite": impermeabilite_sols(repartition),
        "types_sol": wrb_z, "argile": arg_z,
        "sol_dominant": repartition[0][0] if repartition else None,
        "repartition": repartition,
        "argile_moy_pct": argile_moy,
    }


# Aptitude au ruissellement / faible infiltration par grand groupe de sols WRB
# (0 = tres permeable, 1 = tres peu permeable / engorgement), d apres les
# proprietes de drainage decrites dans la WRB (FAO/IUSS) et l equivalence
# usuelle avec les groupes hydrologiques de sols (A sableux -> D argileux
# ou hydromorphes). Utilise quand SoilGrids ne fournit pas d argile (zones
# urbaines masquees dans les cartes de proprietes, ex : Ngor).
IMPERMEABILITE_WRB = {
    "Arenosols": 0.1, "Podzols": 0.2, "Regosols": 0.3, "Ferralsols": 0.3, "Andosols": 0.3,
    "Cambisols": 0.4, "Calcisols": 0.4, "Gypsisols": 0.4, "Nitisols": 0.4, "Umbrisols": 0.4,
    "Phaeozems": 0.4, "Kastanozems": 0.4, "Chernozems": 0.4,
    "Lixisols": 0.5, "Luvisols": 0.5, "Acrisols": 0.5, "Anthrosols": 0.5,
    "Leptosols": 0.6, "Durisols": 0.6, "Albeluvisols": 0.6, "Alisols": 0.6,
    "Plinthosols": 0.7, "Fluvisols": 0.7,
    "Solonchaks": 0.8, "Histosols": 0.8, "Cryosols": 0.8, "Technosols": 0.8,
    "Gleysols": 0.9, "Vertisols": 0.9, "Planosols": 0.9, "Solonetz": 0.9, "Stagnosols": 0.9,
}


def impermeabilite_sols(repartition):
    """Moyenne ponderee (0-1) de l impermeabilite des types de sol presents."""
    total, somme = 0.0, 0.0
    for nom, part in repartition or []:
        if nom in IMPERMEABILITE_WRB:
            somme += IMPERMEABILITE_WRB[nom] * part
            total += part
    return somme / total if total > 0 else None


def aligner_sur_grille(raster, geotransform, projection, forme, nodata, methode="bilinear"):
    """Lit un raster re-echantillonne sur une grille cible (celle des indices
    satellites), pour combiner relief et indices pixel a pixel."""
    from osgeo import gdal
    import numpy as np
    hauteur, largeur = forme
    gt = geotransform
    xmin, ymax = gt[0], gt[3]
    xmax, ymin = xmin + gt[1] * largeur, ymax + gt[5] * hauteur
    ds = gdal.Warp("", raster, format="MEM", outputBounds=(xmin, ymin, xmax, ymax), width=largeur, height=hauteur,
                   dstSRS=projection, resampleAlg=methode, srcNodata=nodata, dstNodata=nodata)
    a = ds.ReadAsArray().astype(np.float64)
    a[a == nodata] = np.nan
    return a


def risque_topographique(alt=None, pente=None, tci=None):
    """Susceptibilite topographique a l accumulation d eau (0-1), scalaire ou
    tableau : basse altitude (0 m -> 1, 10 m et plus -> 0), relief plat
    (pente 0 % -> 1, 5 % et plus -> 0) et indice d humidite topographique
    TCI (6 -> 0, 12 et plus -> 1). Moyenne des composantes disponibles."""
    import numpy as np
    comp = []
    if alt is not None:
        comp.append(1.0 - np.clip(np.asarray(alt, dtype=float) / 10.0, 0.0, 1.0))
    if pente is not None:
        comp.append(1.0 - np.clip(np.asarray(pente, dtype=float) / 5.0, 0.0, 1.0))
    if tci is not None:
        comp.append(np.clip((np.asarray(tci, dtype=float) - 6.0) / 6.0, 0.0, 1.0))
    if not comp:
        return None
    if all(np.ndim(c) == 0 for c in comp):
        return float(sum(comp) / len(comp))
    pile = np.stack([np.broadcast_to(c, np.shape(next(x for x in comp if np.ndim(x) > 0))) for c in comp])
    with np.errstate(invalid="ignore"):
        return np.nanmean(pile, axis=0)


# ---------------------------------------------------------------------------
# Bassins versants et reseau hydrographique MERIT-Hydro (Global Watersheds,
# M. Heberger, https://mghydro.com/watersheds) -- API publique :
#   /app/watershed_api       : bassin versant en amont d un exutoire
#   /app/upstream_rivers_api : troncons de riviere en amont (ordre Strahler)
# Citation : Heberger, M. 2022. Global Watersheds (web application).
# ---------------------------------------------------------------------------
MGHYDRO_URL = "https://mghydro.com/app/"


def longueur_dans_zone_km(chemin_lignes, geojson_path):
    """(longueur des lignes a l interieur de la zone en km, surface zone km2)."""
    from osgeo import ogr, osr
    cible = osr.SpatialReference()
    cible.ImportFromEPSG(32628)
    cible.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    zone = ogr.Open(geojson_path)
    geom_zone = None
    for f in zone.GetLayer():
        g = f.GetGeometryRef().Clone()
        g.Transform(osr.CoordinateTransformation(wgs, cible))
        geom_zone = g if geom_zone is None else geom_zone.Union(g)
    if geom_zone is None:
        return 0.0, 0.0
    longueur = 0.0
    ds = ogr.Open(chemin_lignes)
    if ds is not None:
        lyr = ds.GetLayer()
        srs = lyr.GetSpatialRef() or wgs
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = None if srs.IsSame(cible) else osr.CoordinateTransformation(srs, cible)
        for f in lyr:
            g = f.GetGeometryRef()
            if g is None:
                continue
            g = g.Clone()
            if tr is not None:
                g.Transform(tr)
            inter = g.Intersection(geom_zone)
            if inter is not None and not inter.IsEmpty():
                longueur += inter.Length() / 1000.0
    return longueur, geom_zone.GetArea() / 1e6


def exutoires_principaux(accumulation_tif, geojson_path, work_dir, prefix, resolution, nb=3, dist_min_m=1500.0):
    """Exutoires de la zone = cellules de plus forte accumulation (aire
    drainee GRASS) a l interieur de la zone, espacees d au moins dist_min_m.

    :return: [(lat, lng, aire_drainee_km2)] par aire decroissante
    """
    from osgeo import gdal, osr
    import numpy as np
    clip = _decouper(accumulation_tif, geojson_path, os.path.join(work_dir, prefix + "_acc_exutoires.tif"), -9999)
    ds = gdal.Open(clip)
    gt = ds.GetGeoTransform()
    a = ds.ReadAsArray().astype(np.float64)
    srs = osr.SpatialReference(wkt=ds.GetProjection())
    ds = None
    try:
        os.remove(clip)
    except OSError:  # nosec B110 - erreur non bloquante, ignoree volontairement
        pass  # nosec B110
    a[(a == -9999) | ~np.isfinite(a)] = 0
    a = np.abs(a)
    if a.max() <= 0:
        return []
    cellule_km2 = (resolution * resolution) / 1e6
    seuil = a.max() * 0.05
    ordre = np.argsort(a, axis=None)[::-1]
    choisis = []
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(srs, wgs)
    for idx in ordre[:20000]:
        r, c = divmod(int(idx), a.shape[1])
        v = a[r, c]
        if v < seuil and choisis:
            break
        x = gt[0] + (c + 0.5) * gt[1]
        y = gt[3] + (r + 0.5) * gt[5]
        if any(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 < dist_min_m for cx, cy, _v in choisis):
            continue
        choisis.append((x, y, v))
        if len(choisis) >= nb:
            break
    sortie = []
    for x, y, v in choisis:
        lng, lat, _z = tr.TransformPoint(x, y)
        sortie.append((round(lat, 5), round(lng, 5), v * cellule_km2))
    return sortie


def _appel_mghydro(api, lat, lng, precision="high"):
    import time
    derniere = None
    for tentative in range(3):
        try:
            r = requests.get(MGHYDRO_URL + api, params={"lat": lat, "lng": lng, "precision": precision,
                                                        "simplify": "true"}, timeout=120)
            if r.status_code == 404:
                return None  # pas de bassin / pas de riviere en amont (ex : exutoire en mer)
            if r.status_code in (429, 500, 502, 503, 504):
                derniere = RuntimeError("mghydro HTTP %d" % r.status_code)
                time.sleep(2 + 3 * tentative)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            derniere = exc
            time.sleep(1 + tentative)
    raise derniere or RuntimeError("mghydro injoignable")


def bassins_mghydro(exutoires, geojson_path, work_dir, prefix):
    """Bassins versants et rivieres amont MERIT-Hydro pour chaque exutoire.

    :return: dict {bassins, rivieres (chemins GeoJSON ou None), nb_bassins,
        aire_amont_max_km2, ordre_max, nb_troncons, longueur_zone_km,
        densite_drainage_km_km2, exutoires}
    """
    import json
    from concurrent.futures import ThreadPoolExecutor
    from osgeo import ogr
    if not exutoires:
        raise RuntimeError("aucun exutoire determine")
    with ThreadPoolExecutor(max_workers=6) as pool:
        fb = [pool.submit(_appel_mghydro, "watershed_api", lat, lng) for lat, lng, _a in exutoires]
        fr = [pool.submit(_appel_mghydro, "upstream_rivers_api", lat, lng) for lat, lng, _a in exutoires]
        res_b = [f.result() for f in fb]
        res_r = [f.result() for f in fr]

    # Bassins : on ecarte ceux entierement contenus dans un autre (exutoire en amont)
    polys = []
    for (lat, lng, _a), gj in zip(exutoires, res_b):
        if not gj:
            continue
        for feat in gj.get("features", []):
            try:
                g = ogr.CreateGeometryFromJson(json.dumps(feat["geometry"]))
                aire = float(feat["properties"].get("area_km2") or 0)
            except Exception:  # nosec B112 - erreur non bloquante, ignoree volontairement
                continue  # nosec B112
            if g is not None:
                polys.append((aire, g, feat, lat, lng))
    polys.sort(key=lambda t: -t[0])
    garde = []
    for aire, g, feat, lat, lng in polys:
        if any(g2.Contains(g.Buffer(-1e-5)) or g2.Equals(g) for _a2, g2, _f2, _la, _lo in garde):
            continue
        garde.append((aire, g, feat, lat, lng))
    feats_b = []
    for aire, g, feat, lat, lng in garde:
        feats_b.append({"type": "Feature", "geometry": feat["geometry"],
                        "properties": {"aire_km2": aire, "exutoire_lat": lat, "exutoire_lng": lng,
                                       "source": "MERIT-Hydro (mghydro.com)"}})

    vus, feats_r, ordre_max = set(), [], 0
    for gj in res_r:
        for feat in (gj or {}).get("features", []):
            comid = feat["properties"].get("comid")
            if comid in vus:
                continue
            vus.add(comid)
            so = int(feat["properties"].get("sorder") or 1)
            ordre_max = max(ordre_max, so)
            feats_r.append({"type": "Feature", "geometry": feat["geometry"],
                            "properties": {"comid": comid, "strahler": so}})

    bassins = rivieres = None
    if feats_b:
        bassins = os.path.join(work_dir, prefix + "_bassins_mghydro.geojson")
        with open(bassins, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": feats_b}, f)
    longueur = 0.0
    surface = 0.0
    if feats_r:
        rivieres = os.path.join(work_dir, prefix + "_rivieres_mghydro.geojson")
        with open(rivieres, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": feats_r}, f)
        longueur, surface = longueur_dans_zone_km(rivieres, geojson_path)
    else:
        _l, surface = longueur_dans_zone_km(geojson_path, geojson_path)
    if bassins is None and rivieres is None and all(x is None for x in res_b):
        raise RuntimeError("aucun bassin MERIT-Hydro pour ces exutoires")
    return {
        "bassins": bassins, "rivieres": rivieres, "nb_bassins": len(feats_b),
        "aire_amont_max_km2": max((a for a, *_r in garde), default=None),
        "ordre_max": ordre_max or None, "nb_troncons": len(feats_r),
        "longueur_zone_km": longueur,
        "densite_drainage_km_km2": (longueur / surface) if surface > 0 else None,
        "exutoires": exutoires,
    }
