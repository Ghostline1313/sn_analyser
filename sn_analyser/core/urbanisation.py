# -*- coding: utf-8 -*-
"""
core/urbanisation.py
---------------------
Module d analyse de l urbanisation :
- NDBI (via core/ndbi.py)
- Seuillage automatique (methode d Otsu) ou manuel
- Calcul de la surface urbanisee
- Taux de croissance annuel
"""

import numpy as np
from osgeo import gdal


def _seuil_otsu_depuis_valeurs(valeurs, nb_bins=256):
    """Calcule le seuil optimal (methode d Otsu) a partir d un tableau de
    valeurs deja filtrees (nodata exclu). Fonction interne partagee par
    calculer_seuil_otsu (une seule scene) et calculer_seuil_otsu_multi
    (plusieurs scenes/dates poolees).
    """
    if valeurs.size == 0:
        raise ValueError("Aucune valeur exploitable pour calculer le seuil")

    hist, bin_edges = np.histogram(valeurs, bins=nb_bins)
    hist = hist.astype(np.float64)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2.0

    poids_total = hist.sum()
    if poids_total == 0:
        raise ValueError("Histogramme vide")

    poids1 = np.cumsum(hist)
    poids2 = poids_total - poids1

    somme_totale = np.sum(hist * bin_centers)
    somme1 = np.cumsum(hist * bin_centers)

    with np.errstate(divide="ignore", invalid="ignore"):
        moyenne1 = np.where(poids1 > 0, somme1 / poids1, 0.0)
        moyenne2 = np.where(poids2 > 0, (somme_totale - somme1) / poids2, 0.0)

    variance_inter = poids1 * poids2 * (moyenne1 - moyenne2) ** 2

    idx_optimal = int(np.argmax(variance_inter))
    return float(bin_centers[idx_optimal])


def _valeurs_exploitables(raster_path):
    """Ouvre un raster d indice (NDBI/NDVI/NDWI) et retourne ses valeurs
    finies, nodata (0.0) exclu."""
    ds = gdal.Open(raster_path)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir le raster fourni : " + str(raster_path))
    arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    ds = None
    valeurs = arr[np.isfinite(arr)]
    valeurs = valeurs[valeurs != 0]  # exclut le nodata (0.0 utilise par calculer_ndbi/ndvi/ndwi)
    return valeurs


def calculer_seuil_otsu(raster_ndbi, nb_bins=256):
    """Calcule le seuil optimal (methode d Otsu) separant deux populations
    de pixels (bati / non-bati) dans un raster NDBI.

    La methode d Otsu cherche le seuil qui maximise la variance inter-classe
    (ou, de maniere equivalente, minimise la variance intra-classe) sur
    l histogramme des valeurs. Elle ne suppose aucune valeur fixe et s adapte
    a chaque scene/capteur/source de donnees.

    ATTENTION comparaison multi-temporelle : ce seuil est calcule sur UNE
    SEULE scene. Pour comparer deux dates (evolution urbaine, deforestation,
    trait de cote...), utiliser calculer_seuil_otsu_multi() sur les rasters
    des deux dates afin de garder une definition coherente de la classe dans
    le temps ; sinon un seuil different par date peut a lui seul produire une
    fausse hausse/baisse de surface qui ne reflete rien de reel.

    :param raster_ndbi: chemin du raster NDBI
    :param nb_bins: nombre de classes de l histogramme utilise pour le calcul
    :return: seuil NDBI optimal (float)
    """
    valeurs = _valeurs_exploitables(raster_ndbi)
    if valeurs.size == 0:
        raise ValueError("Aucune valeur NDBI exploitable pour calculer le seuil")
    return _seuil_otsu_depuis_valeurs(valeurs, nb_bins)


def calculer_seuil_otsu_multi(rasters, nb_bins=256):
    """Calcule un seuil Otsu UNIQUE, partage entre plusieurs rasters d indice
    (typiquement le NDBI/NDVI/NDWI de deux ou plusieurs dates comparees).

    Indispensable pour une comparaison multi-temporelle valide : la classe
    "bati"/"vegetation"/"eau" doit garder la meme definition (le meme seuil)
    d une date a l autre. Calculer un seuil independant par date (via
    calculer_seuil_otsu) fait deriver cette definition avec les conditions de
    prise de vue (capteur, saison, nuages...) et peut a lui seul produire une
    fausse tendance (ex: fausse baisse de surface batie) sans lien avec un
    changement reel sur le terrain.

    :param rasters: liste de chemins de rasters d indice (memes bandes/unite,
        ex: tous des NDBI)
    :param nb_bins: nombre de classes de l histogramme utilise pour le calcul
    :return: seuil optimal (float), commun a tous les rasters fournis
    """
    if not rasters:
        raise ValueError("Aucun raster fourni pour calculer_seuil_otsu_multi")

    toutes_valeurs = [_valeurs_exploitables(r) for r in rasters]
    valeurs = np.concatenate(toutes_valeurs) if len(toutes_valeurs) > 1 else toutes_valeurs[0]
    if valeurs.size == 0:
        raise ValueError("Aucune valeur exploitable pour calculer le seuil commun")
    return _seuil_otsu_depuis_valeurs(valeurs, nb_bins)


def classifier_bati(raster_ndbi, raster_sortie, seuil=None):
    """Classifie le raster NDBI en bati (1) / non-bati (0).

    :param raster_ndbi: chemin du raster NDBI en entree
    :param raster_sortie: chemin du raster classe (GeoTIFF, Byte) a produire
    :param seuil: seuil NDBI au-dessus duquel un pixel est bati. Si None
        (par defaut), le seuil est calcule automatiquement par la methode
        d Otsu (recommande : s adapte a chaque scene/source de donnees).
    :return: (chemin_sortie, seuil_utilise)
    """
    ds = gdal.Open(raster_ndbi)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir le raster NDBI fourni")

    if seuil is None:
        seuil = calculer_seuil_otsu(raster_ndbi)

    arr = ds.GetRasterBand(1).ReadAsArray()
    nodata_mask = (arr == 0) | ~np.isfinite(arr)
    classe = (arr > seuil).astype(np.uint8)
    classe[nodata_mask] = 255

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(
        raster_sortie, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte
    )
    out_ds.SetGeoTransform(ds.GetGeoTransform())
    out_ds.SetProjection(ds.GetProjection())
    out_ds.GetRasterBand(1).SetNoDataValue(255)
    out_ds.GetRasterBand(1).WriteArray(classe)
    out_ds.FlushCache()
    out_ds = None
    ds = None
    return raster_sortie, seuil


def surface_urbanisee(raster_classifie):
    """Calcule la surface batie (ha) a partir d un raster classe (1=bati).

    Le raster doit etre dans un CRS projete (unites metriques), pas en
    coordonnees geographiques (degres).

    :param raster_classifie: chemin du raster classe (deja rogne a la zone d etude)
    :return: surface en hectares (float)
    """
    ds = gdal.Open(raster_classifie)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir le raster classifie fourni")

    gt = ds.GetGeoTransform()
    pixel_area_m2 = abs(gt[1] * gt[5])
    arr = ds.GetRasterBand(1).ReadAsArray()
    nb_pixels_batis = int((arr == 1).sum())
    ds = None

    surface_m2 = nb_pixels_batis * pixel_area_m2
    return surface_m2 / 10000.0


def taux_croissance_annuel(surface_debut, surface_fin, nb_annees):
    """Calcule le taux de croissance annuel moyen de la surface urbanisee (%)."""
    if surface_debut <= 0 or nb_annees <= 0:
        raise ValueError("surface_debut et nb_annees doivent etre positifs")
    return ((surface_fin / surface_debut) ** (1 / nb_annees) - 1) * 100
