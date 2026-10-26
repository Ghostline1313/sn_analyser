# -*- coding: utf-8 -*-
"""
core/environnement.py
-----------------------
Module d analyse de la degradation environnementale :
NDVI (via core/ndvi.py), classification de la couverture vegetale,
detection de deforestation/degradation entre deux dates.

Reutilise la methode d Otsu (core/urbanisation.py) pour un seuillage
NDVI auto-calibre : plus robuste qu un seuil fixe (type 0.3) qui varie
selon le capteur, la saison et l eclairage.
"""

import numpy as np
from osgeo import gdal


def classifier_vegetation(raster_ndvi, raster_sortie, seuil=None):
    """Classifie le raster NDVI en vegetation (1) / non-vegetation (0).

    :param raster_ndvi: chemin du raster NDVI en entree
    :param raster_sortie: chemin du raster classe (GeoTIFF, Byte) a produire
    :param seuil: seuil NDVI au-dessus duquel un pixel est vegetalise. Si
        None (par defaut), calcule automatiquement par la methode d Otsu.
    :return: (chemin_sortie, seuil_utilise)
    """
    from .urbanisation import calculer_seuil_otsu

    ds = gdal.Open(raster_ndvi)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir le raster NDVI fourni")

    if seuil is None:
        seuil = calculer_seuil_otsu(raster_ndvi)

    arr = ds.GetRasterBand(1).ReadAsArray()
    nodata_mask = (arr == 0) | ~np.isfinite(arr)
    classe = (arr > seuil).astype(np.uint8)
    classe[nodata_mask] = 255

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(raster_sortie, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte)
    out_ds.SetGeoTransform(ds.GetGeoTransform())
    out_ds.SetProjection(ds.GetProjection())
    # IMPORTANT : SetNoDataValue AVANT WriteArray. Dans l ordre inverse (constate
    # empiriquement, cause du bug "vegetation 1984 = 0 ha alors que le NDVI est
    # calcule correctement") : quand TOUS les pixels ecrits ont la meme valeur
    # (ex : classe entierement 0, aucun pixel au-dessus du seuil), le driver
    # GTiff peut re-remplir le bloc avec la valeur nodata au moment ou
    # SetNoDataValue() est appele apres coup, ecrasant silencieusement les
    # donnees deja ecrites (le tableau numpy en memoire reste correct, seul le
    # fichier sur disque est corrompu - d ou la difficulte a detecter le bug).
    out_ds.GetRasterBand(1).SetNoDataValue(255)
    out_ds.GetRasterBand(1).WriteArray(classe)
    out_ds.FlushCache()
    out_ds = None
    ds = None
    return raster_sortie, seuil


def surface_vegetation(raster_classifie):
    """Calcule la surface vegetalisee (ha) a partir d un raster classe (1=vegetation).

    Le raster doit etre dans un CRS projete (unites metriques).

    :param raster_classifie: chemin du raster classe (deja rogne a la zone d etude)
    :return: surface en hectares (float)
    """
    ds = gdal.Open(raster_classifie)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir le raster classifie fourni")

    gt = ds.GetGeoTransform()
    pixel_area_m2 = abs(gt[1] * gt[5])
    arr = ds.GetRasterBand(1).ReadAsArray()
    nb_pixels_veg = int((arr == 1).sum())
    ds = None

    return (nb_pixels_veg * pixel_area_m2) / 10000.0


def calculer_savi(raster_nir, raster_red, raster_sortie, l=0.5):
    """SAVI = ((NIR - RED) / (NIR + RED + L)) * (1 + L) - indice de vegetation
    ajuste au sol (utile en zones a couverture vegetale faible/moyenne, type Sahel).
    """
    raise NotImplementedError("A implementer : calcul SAVI (meme structure que calculer_ndvi)")


def calculer_evi(raster_nir, raster_red, raster_blue, raster_sortie):
    """EVI - indice de vegetation ameliore, moins sensible a l atmosphere et au sol."""
    raise NotImplementedError("A implementer : calcul EVI")


def calculer_nbr(raster_nir, raster_swir2, raster_sortie):
    """NBR = (NIR - SWIR2) / (NIR + SWIR2) - indice de zones brulees / degradation."""
    raise NotImplementedError("A implementer : calcul NBR")


def detecter_deforestation(surface_veg_debut, surface_veg_fin):
    """Calcule la variation de surface vegetalisee entre deux dates.

    :return: dict {variation_ha, variation_pct, degradation (bool)}
    """
    variation_ha = surface_veg_fin - surface_veg_debut
    variation_pct = (
        (variation_ha / surface_veg_debut * 100) if surface_veg_debut > 0 else 0.0
    )
    return {
        "variation_ha": variation_ha,
        "variation_pct": variation_pct,
        "degradation": variation_ha < 0,
    }


def detecter_artificialisation(ndbi_t1, ndbi_t2, seuil_gain=0.10):
    """Detecte les zones nouvellement artificialisees entre deux dates (delta NDBI)."""
    raise NotImplementedError("A implementer : detection d artificialisation")


def calculer_seuil_otsu_multi(rasters, nb_bins=256):
    """Seuil Otsu UNIQUE partage entre plusieurs rasters (voir
    core.urbanisation.calculer_seuil_otsu_multi pour le detail et la raison
    d etre : indispensable pour une comparaison multi-temporelle valide)."""
    from .urbanisation import calculer_seuil_otsu_multi as _calc
    return _calc(rasters, nb_bins)
