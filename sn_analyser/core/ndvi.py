# -*- coding: utf-8 -*-
"""
core/ndvi.py
------------
NDVI = (NIR - RED) / (NIR + RED), calcule sur la REFLECTANCE (pas le DN brut).
Indice de vegetation normalise, utilise pour le suivi de la couverture
vegetale et la detection de deforestation/degradation.
"""

import numpy as np
from osgeo import gdal

# Landsat Collection 2 Level-2 Surface Reflectance : conversion DN -> reflectance
# de surface (0-1), IDENTIQUE pour TM/ETM+/OLI (documentation USGS). Sans cette
# conversion, le NDVI est calcule directement sur les DN bruts : le ratio
# resultant est fortement compresse vers 0 (l offset -0.2, constant sur les
# deux bandes, ne s annule PAS dans le ratio (NIR-RED)/(NIR+RED)), ce qui
# sous-estime systematiquement la vegetation - constate empiriquement : NDVI
# quasi divise par 2 par rapport au calcul correct (verifie sur Bambilor 1984,
# zone agricole : NDVI reel ~0.18 contre ~0.10 calcule sans cette conversion).
_L2_SCALE = 0.0000275
_L2_OFFSET = -0.2


def _dn_vers_reflectance(dn):
    """Convertit un tableau de DN Collection 2 L2 en reflectance de surface
    (0.0 preserve comme sentinelle nodata, coherent avec le reste du pipeline)."""
    reflectance = dn * _L2_SCALE + _L2_OFFSET
    # Bornage [0.0001, 1] : les reflectances L2 legerement negatives (eau,
    # ombres) faisaient exploser les indices normalises (NDBI/NDWI > 8).
    reflectance = np.clip(reflectance, 0.0001, 1.0)
    return np.where(dn == 0, 0.0, reflectance)


def calculer_ndvi(raster_nir, raster_red, raster_sortie):
    """Calcule le NDVI a partir de DEUX fichiers separes (NIR et RED).

    :param raster_nir: chemin de la bande NIR
    :param raster_red: chemin de la bande RED
    :param raster_sortie: chemin du GeoTIFF NDVI a produire
    """
    nir_ds = gdal.Open(raster_nir)
    red_ds = gdal.Open(raster_red)
    if nir_ds is None or red_ds is None:
        raise RuntimeError("Impossible d ouvrir les bandes NIR/RED fournies")

    nir = nir_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    red = red_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)

    return _ecrire_ndvi(nir, red, nir_ds.GetGeoTransform(), nir_ds.GetProjection(),
                         nir_ds.RasterXSize, nir_ds.RasterYSize, raster_sortie)


def calculer_ndvi_depuis_extrait(raster_extrait, raster_sortie, idx_nir=1, idx_red=2):
    """Calcule le NDVI a partir d UN SEUL extrait multi-bandes (ex: telecharge
    via core.landsat.telecharger_extrait avec bandes=["nir", "red"]).

    :param raster_extrait: chemin du GeoTIFF multi-bandes en entree
    :param raster_sortie: chemin du GeoTIFF NDVI a produire
    :param idx_nir: index de bande (1-based) correspondant au NIR
    :param idx_red: index de bande (1-based) correspondant au RED
    """
    ds = gdal.Open(raster_extrait)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir l extrait fourni")

    nir = ds.GetRasterBand(idx_nir).ReadAsArray().astype(np.float32)
    red = ds.GetRasterBand(idx_red).ReadAsArray().astype(np.float32)

    return _ecrire_ndvi(nir, red, ds.GetGeoTransform(), ds.GetProjection(),
                         ds.RasterXSize, ds.RasterYSize, raster_sortie)


def _ecrire_ndvi(nir, red, geotransform, projection, largeur, hauteur, raster_sortie):
    nir_refl = _dn_vers_reflectance(nir)
    red_refl = _dn_vers_reflectance(red)
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (nir_refl - red_refl) / (nir_refl + red_refl)
    ndvi = np.nan_to_num(ndvi, nan=0.0, posinf=0.0, neginf=0.0)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(raster_sortie, largeur, hauteur, 1, gdal.GDT_Float32)
    out_ds.SetGeoTransform(geotransform)
    out_ds.SetProjection(projection)
    out_ds.GetRasterBand(1).SetNoDataValue(0.0)
    out_ds.GetRasterBand(1).WriteArray(ndvi)
    out_ds.FlushCache()
    out_ds = None
    return raster_sortie
