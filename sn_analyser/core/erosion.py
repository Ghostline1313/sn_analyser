# -*- coding: utf-8 -*-
"""
core/erosion.py
----------------
Module d analyse de l erosion cotiere :
- Classification eau/terre par NDWI (seuillage Otsu auto-calibre)
- Comparaison inter-dates : gain de surface en eau (erosion) / perte
  de surface en eau (accretion, avancee du trait de cote)

v1.0 : approche par comparaison de surfaces (plus simple et robuste que
la methode par transects/DSAS, qui necessite une extraction vectorielle
fine du trait de cote et pourra etre ajoutee ulterieurement pour un calcul
de taux de recul lineaire en m/an).
"""

import os

import numpy as np
from osgeo import gdal


def classifier_eau(raster_ndwi, raster_sortie, seuil=None):
    """Classifie le raster NDWI en eau (1) / terre (0).

    :param raster_ndwi: chemin du raster NDWI en entree
    :param raster_sortie: chemin du raster classe (GeoTIFF, Byte) a produire
    :param seuil: seuil NDWI au-dessus duquel un pixel est de l eau. Si None
        (par defaut), calcule automatiquement par la methode d Otsu.
    :return: (chemin_sortie, seuil_utilise)
    """
    from .urbanisation import calculer_seuil_otsu

    ds = gdal.Open(raster_ndwi)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir le raster NDWI fourni")

    if seuil is None:
        seuil = calculer_seuil_otsu(raster_ndwi)

    arr = ds.GetRasterBand(1).ReadAsArray()
    nodata_mask = (arr == 0) | ~np.isfinite(arr)
    classe = (arr > seuil).astype(np.uint8)
    classe[nodata_mask] = 255

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(raster_sortie, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte)
    out_ds.SetGeoTransform(ds.GetGeoTransform())
    out_ds.SetProjection(ds.GetProjection())
    out_ds.GetRasterBand(1).SetNoDataValue(255)
    out_ds.GetRasterBand(1).WriteArray(classe)
    out_ds.FlushCache()
    out_ds = None
    ds = None
    return raster_sortie, seuil


def filtrer_petites_composantes_eau(raster_classe, taille_min_pixels=15):
    '''Nettoie un raster classe eau(1)/terre(0)/nodata(255) EN PLACE :
    reclasse en "terre" (0) toute composante connexe de la classe "eau" dont
    la taille est INFERIEURE a taille_min_pixels (mares, bassins de
    retenue, sols tres humides, ombres mal classees, isolees et sans
    rapport avec le littoral). Les composantes assez grandes (mer, y
    compris ses eventuels segments disjoints de part et d autre d une
    presqu ile) sont conservees telles quelles.

    Seuil par defaut (15 pixels, ~1.35 ha a 30 m de resolution Landsat) :
    au-dela d une mare/bassin de retenue usuel, en-deca de tout segment de
    mer credible touchant une commune cotiere.

    Approche PUREMENT topologique (composantes connexes, scipy.ndimage.label)
    : ne depend d aucune donnee externe (pas de trait de cote OSM a
    recuperer/bufferiser), donc robuste meme si la geometrie de la commune
    ou le trait de cote OSM sont imprecis pres du littoral.

    :param raster_classe: chemin du raster classe (modifie sur place)
    :return: (nb_composantes_eau_totales, nb_composantes_eau_supprimees)
    '''
    from scipy import ndimage

    ds = gdal.Open(raster_classe, gdal.GA_Update)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir le raster classe fourni (mode ecriture)")

    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray()

    masque_eau = (arr == 1)
    etiquettes, nb_composantes = ndimage.label(masque_eau, structure=np.ones((3, 3)))

    if nb_composantes == 0:
        ds = None
        return 0, 0

    tailles = ndimage.sum(masque_eau, etiquettes, index=range(1, nb_composantes + 1))
    petites_etiquettes = {i + 1 for i, taille in enumerate(tailles) if taille < taille_min_pixels}

    if not petites_etiquettes:
        ds = None
        return nb_composantes, 0

    a_reclasser = np.isin(etiquettes, list(petites_etiquettes))
    arr[a_reclasser] = 0

    band.WriteArray(arr)
    band.FlushCache()
    ds = None

    return nb_composantes, len(petites_etiquettes)


def surface_eau(raster_classifie):
    """Calcule la surface en eau (ha) a partir d un raster classe (1=eau).

    Le raster doit etre dans un CRS projete (unites metriques).
    """
    ds = gdal.Open(raster_classifie)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir le raster classifie fourni")

    gt = ds.GetGeoTransform()
    pixel_area_m2 = abs(gt[1] * gt[5])
    arr = ds.GetRasterBand(1).ReadAsArray()
    nb_pixels_eau = int((arr == 1).sum())
    ds = None

    return (nb_pixels_eau * pixel_area_m2) / 10000.0


def comparer_trait_cote(raster_eau_debut, raster_eau_fin):
    """Compare deux rasters eau/terre classes (memes dimensions/emprise) et
    quantifie l erosion (gain de surface en eau, terre -> eau) et l accretion
    (perte de surface en eau, eau -> terre) entre les deux dates.

    :return: dict {erosion_ha, accretion_ha, bilan_ha, bilan_pct}
        bilan_ha > 0 signifie un recul net du trait de cote (perte de terre)
    """
    ds1 = gdal.Open(raster_eau_debut)
    ds2 = gdal.Open(raster_eau_fin)
    if ds1 is None or ds2 is None:
        raise RuntimeError("Impossible d ouvrir les rasters eau/terre fournis")

    arr1 = ds1.GetRasterBand(1).ReadAsArray()
    arr2 = ds2.GetRasterBand(1).ReadAsArray()
    if arr1.shape != arr2.shape:
        raise ValueError("Les deux rasters n ont pas les memes dimensions (memes bbox/resolution requises)")

    gt = ds1.GetGeoTransform()
    pixel_area_m2 = abs(gt[1] * gt[5])
    ds1 = None
    ds2 = None

    valide = (arr1 != 255) & (arr2 != 255)
    terre_vers_eau = int(((arr1 == 0) & (arr2 == 1) & valide).sum())  # erosion
    eau_vers_terre = int(((arr1 == 1) & (arr2 == 0) & valide).sum())  # accretion

    erosion_ha = (terre_vers_eau * pixel_area_m2) / 10000.0
    accretion_ha = (eau_vers_terre * pixel_area_m2) / 10000.0
    bilan_ha = erosion_ha - accretion_ha

    return {
        "erosion_ha": erosion_ha,
        "accretion_ha": accretion_ha,
        "bilan_ha": bilan_ha,
        "recul_net": bilan_ha > 0,
    }


def extraire_trait_cote(raster_classe, raster_sortie_gpkg, nom_couche="trait_cote"):
    """Extrait le trait de cote (limite terre/eau) sous forme vectorielle
    (ligne) a partir d un raster classe eau/terre (0=terre, 1=eau,
    255=nodata) : genere un contour au niveau 0.5, exactement a la frontiere
    entre les deux classes (gdal.ContourGenerate, plus direct et plus propre
    qu un Polygonize + extraction du contour du polygone).

    :param raster_classe: chemin du raster classe (voir classifier_eau)
    :param raster_sortie_gpkg: chemin du GeoPackage de sortie (ligne)
    :param nom_couche: nom de la couche dans le GeoPackage
    :return: (raster_sortie_gpkg, nom_couche)
    """
    from osgeo import ogr, osr

    ds = gdal.Open(raster_classe)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir le raster classe fourni")
    band = ds.GetRasterBand(1)

    srs = osr.SpatialReference()
    srs.ImportFromWkt(ds.GetProjection())

    driver = ogr.GetDriverByName("GPKG")
    if os.path.exists(raster_sortie_gpkg):
        driver.DeleteDataSource(raster_sortie_gpkg)
    out_ds = driver.CreateDataSource(raster_sortie_gpkg)
    out_layer = out_ds.CreateLayer(nom_couche, srs=srs, geom_type=ogr.wkbLineString)
    champ_niveau = ogr.FieldDefn("niveau", ogr.OFTReal)
    out_layer.CreateField(champ_niveau)

    # useNoData=1 + noDataValue=255 : les pixels 255 (hors zone d etude, voir
    # clip_raster_to_geometry) sont exclus du calcul, evite de generer un faux
    # contour artificiel sur la limite de decoupage de la zone d etude.
    gdal.ContourGenerate(band, 0, 0, [0.5], 1, 255, out_layer, -1, 0)

    out_ds = None
    ds = None
    return raster_sortie_gpkg, nom_couche


def _iter_parties_ligne(geom):
    """Genere chaque partie LineString d une geometrie OGR (gere le cas
    MultiLineString produit par ContourGenerate)."""
    from osgeo import ogr

    t = geom.GetGeometryType()
    if t in (ogr.wkbLineString, ogr.wkbLineString25D):
        yield geom
    elif t in (ogr.wkbMultiLineString, ogr.wkbMultiLineString25D):
        for i in range(geom.GetGeometryCount()):
            yield geom.GetGeometryRef(i)


def generer_transects(trait_cote_gpkg, nom_couche, longueur_m=300, pas_m=100):
    """Genere des transects perpendiculaires regulierement espaces le long
    d un trait de cote de reference (methode inspiree de DSAS - Digital
    Shoreline Analysis System, simplifiee).

    :param trait_cote_gpkg: GeoPackage produit par extraire_trait_cote (sert
        de ligne de reference/baseline pour le positionnement des transects)
    :param longueur_m: longueur totale de chaque transect (de part et d autre
        du trait de cote), en metres
    :param pas_m: espacement entre deux transects consecutifs, en metres
    :return: liste de dicts {id, x, y, dir_x, dir_y, x_a, y_a, x_b, y_b}
        (dir_x/dir_y = vecteur perpendiculaire unitaire au trait de cote)
    """
    from osgeo import ogr

    ds = ogr.Open(trait_cote_gpkg)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir le trait de cote fourni")
    layer = ds.GetLayerByName(nom_couche)

    transects = []
    tid = 0
    for feat in layer:
        geom = feat.GetGeometryRef()
        if geom is None:
            continue
        for partie in _iter_parties_ligne(geom):
            pts = [partie.GetPoint(i) for i in range(partie.GetPointCount())]
            if len(pts) < 2:
                continue
            cible = 0.0
            dist_parcourue = 0.0
            seg_idx = 0
            while seg_idx < len(pts) - 1:
                x1, y1 = pts[seg_idx][0], pts[seg_idx][1]
                x2, y2 = pts[seg_idx + 1][0], pts[seg_idx + 1][1]
                seg_len = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
                if seg_len == 0:
                    seg_idx += 1
                    continue
                if dist_parcourue + seg_len >= cible:
                    t = (cible - dist_parcourue) / seg_len
                    x = x1 + t * (x2 - x1)
                    y = y1 + t * (y2 - y1)
                    tx, ty = (x2 - x1) / seg_len, (y2 - y1) / seg_len
                    px, py = -ty, tx  # perpendiculaire unitaire
                    demi = longueur_m / 2.0
                    transects.append({
                        "id": tid,
                        "x": x, "y": y,
                        "dir_x": px, "dir_y": py,
                        "x_a": x - px * demi, "y_a": y - py * demi,
                        "x_b": x + px * demi, "y_b": y + py * demi,
                    })
                    tid += 1
                    cible += pas_m
                else:
                    dist_parcourue += seg_len
                    seg_idx += 1
    ds = None
    return transects


def _extraire_points_geom(geom):
    """Extrait tous les points (x, y) d une geometrie OGR resultant d une
    intersection (Point, MultiPoint, LineString residuel, ou collection)."""
    from osgeo import ogr

    t = geom.GetGeometryType()
    pts = []
    if t in (ogr.wkbPoint, ogr.wkbPoint25D):
        pts.append((geom.GetX(), geom.GetY()))
    elif t in (ogr.wkbMultiPoint, ogr.wkbMultiPoint25D):
        for i in range(geom.GetGeometryCount()):
            sub = geom.GetGeometryRef(i)
            pts.append((sub.GetX(), sub.GetY()))
    elif t in (ogr.wkbLineString, ogr.wkbLineString25D):
        n = geom.GetPointCount()
        if n:
            x, y = geom.GetPoint(n // 2)[0], geom.GetPoint(n // 2)[1]
            pts.append((x, y))
    elif t in (ogr.wkbGeometryCollection, ogr.wkbMultiLineString, ogr.wkbGeometryCollection25D):
        for i in range(geom.GetGeometryCount()):
            pts.extend(_extraire_points_geom(geom.GetGeometryRef(i)))
    return pts


def _intersection_plus_proche(transect_line, geoms, cx, cy):
    """Point d intersection entre transect_line et la liste de geometries
    geoms, le plus proche du centre (cx, cy) du transect. None si aucune
    intersection."""
    meilleur = None
    meilleure_dist = None
    for g in geoms:
        inter = transect_line.Intersection(g)
        if inter is None or inter.IsEmpty():
            continue
        for (x, y) in _extraire_points_geom(inter):
            d = (x - cx) ** 2 + (y - cy) ** 2
            if meilleure_dist is None or d < meilleure_dist:
                meilleure_dist = d
                meilleur = (x, y)
    return meilleur


def _valeur_raster_au_point(raster_path, x, y):
    """Valeur du pixel du raster classe le plus proche du point (x, y),
    donne en coordonnees du CRS du raster. None si hors emprise."""
    ds = gdal.Open(raster_path)
    if ds is None:
        return None
    gt = ds.GetGeoTransform()
    col = int((x - gt[0]) / gt[1])
    row = int((y - gt[3]) / gt[5])
    if col < 0 or row < 0 or col >= ds.RasterXSize or row >= ds.RasterYSize:
        ds = None
        return None
    val = ds.GetRasterBand(1).ReadAsArray(col, row, 1, 1)
    ds = None
    if val is None:
        return None
    return int(val[0][0])


def mesurer_transects(transects, trait_debut_gpkg, nom_couche_debut,
                       trait_fin_gpkg, nom_couche_fin, raster_classe_debut):
    """Pour chaque transect, mesure la distance entre le trait de cote de
    debut et celui de fin (intersection la plus proche du centre du
    transect avec chacun des deux traits), et determine le sens (erosion =
    recul du trait de cote vers la terre, accretion = avancee vers la mer)
    en comparant au sens "vers la terre" localement (deduit du raster classe
    de l annee de debut).

    :return: liste de dicts {id, x1, y1, x2, y2, distance_m, sens}
        (un transect sans intersection sur l un des deux traits est ignore)
    """
    from osgeo import ogr

    ds1 = ogr.Open(trait_debut_gpkg)
    layer1 = ds1.GetLayerByName(nom_couche_debut)
    geoms1 = [f.GetGeometryRef().Clone() for f in layer1]

    ds2 = ogr.Open(trait_fin_gpkg)
    layer2 = ds2.GetLayerByName(nom_couche_fin)
    geoms2 = [f.GetGeometryRef().Clone() for f in layer2]

    resultats = []
    eps = 15.0  # m, environ un demi-pixel Landsat (30m) : distance de test
    # pour determiner localement de quel cote du trait de cote se trouve la terre.
    for t in transects:
        transect_line = ogr.Geometry(ogr.wkbLineString)
        transect_line.AddPoint(t["x_a"], t["y_a"])
        transect_line.AddPoint(t["x_b"], t["y_b"])

        p1 = _intersection_plus_proche(transect_line, geoms1, t["x"], t["y"])
        p2 = _intersection_plus_proche(transect_line, geoms2, t["x"], t["y"])
        if p1 is None or p2 is None:
            continue

        dir_x, dir_y = t["dir_x"], t["dir_y"]
        val_plus = _valeur_raster_au_point(raster_classe_debut, p1[0] + dir_x * eps, p1[1] + dir_y * eps)
        val_moins = _valeur_raster_au_point(raster_classe_debut, p1[0] - dir_x * eps, p1[1] - dir_y * eps)
        if val_plus == 0:
            landward = (dir_x, dir_y)
        elif val_moins == 0:
            landward = (-dir_x, -dir_y)
        else:
            landward = None

        distance_m = ((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2) ** 0.5
        sens = "indetermine"
        if landward is not None and distance_m > 0.5:
            vecx, vecy = p2[0] - p1[0], p2[1] - p1[1]
            signe = vecx * landward[0] + vecy * landward[1]
            sens = "erosion" if signe > 0 else "accretion"
        elif distance_m <= 0.5:
            sens = "stable"

        resultats.append({
            "id": t["id"],
            "x1": p1[0], "y1": p1[1], "x2": p2[0], "y2": p2[1],
            "distance_m": round(distance_m, 1),
            "sens": sens,
        })

    ds1 = None
    ds2 = None
    return resultats


def taux_annuel_recul(distance_m, nb_annees):
    """Calcule le taux annuel de recul du trait de cote (m/an)."""
    if nb_annees <= 0:
        raise ValueError("nb_annees doit etre positif")
    return distance_m / nb_annees


def carte_risque(distances_recul, seuils=(5, 15, 30)):
    """Classe les transects en niveaux de risque (faible/moyen/eleve/critique)
    selon le taux annuel de recul.
    """
    raise NotImplementedError("A implementer : classification du risque cotier")


def calculer_seuil_otsu_multi(rasters, nb_bins=256):
    """Seuil Otsu UNIQUE partage entre plusieurs rasters (voir
    core.urbanisation.calculer_seuil_otsu_multi pour le detail et la raison
    d etre : indispensable pour une comparaison multi-temporelle valide)."""
    from .urbanisation import calculer_seuil_otsu_multi as _calc
    return _calc(rasters, nb_bins)
