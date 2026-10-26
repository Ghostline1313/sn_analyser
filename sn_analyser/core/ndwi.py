# -*- coding: utf-8 -*-
"""
core/ndwi.py
------------
NDWI = (GREEN - NIR) / (GREEN + NIR), calcule sur la REFLECTANCE (pas le DN
brut), pour la meme raison que core/ndvi.py : l offset de conversion Landsat
Collection 2 L2 (-0.2, constant sur les deux bandes) ne s annule pas dans le
ratio et biaise systematiquement l indice si on l omet.
"""

import numpy as np
from osgeo import gdal

# Landsat Collection 2 Level-2 Surface Reflectance : conversion DN -> reflectance
# de surface (0-1), identique pour TM/ETM+/OLI (documentation USGS). Voir
# core/ndvi.py pour le detail empirique de l impact de cette conversion.
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


def calculer_ndwi(raster_green, raster_nir, raster_sortie):
    """Calcule le NDWI a partir de DEUX fichiers separes (GREEN et NIR)."""
    green_ds = gdal.Open(raster_green)
    nir_ds = gdal.Open(raster_nir)
    if green_ds is None or nir_ds is None:
        raise RuntimeError("Impossible d ouvrir les bandes GREEN/NIR fournies")

    green = green_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    nir = nir_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)

    return _ecrire_ndwi(green, nir, green_ds.GetGeoTransform(), green_ds.GetProjection(),
                         green_ds.RasterXSize, green_ds.RasterYSize, raster_sortie)


def calculer_ndwi_depuis_extrait(raster_extrait, raster_sortie, idx_green=1, idx_nir=2):
    """Calcule le NDWI a partir d UN SEUL extrait multi-bandes (telecharge via
    core.landsat.telecharger_extrait avec bandes=["green", "nir"]).
    """
    ds = gdal.Open(raster_extrait)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir l extrait fourni")

    green = ds.GetRasterBand(idx_green).ReadAsArray().astype(np.float32)
    nir = ds.GetRasterBand(idx_nir).ReadAsArray().astype(np.float32)

    return _ecrire_ndwi(green, nir, ds.GetGeoTransform(), ds.GetProjection(),
                         ds.RasterXSize, ds.RasterYSize, raster_sortie)


def _ecrire_ndwi(green, nir, geotransform, projection, largeur, hauteur, raster_sortie):
    green_refl = _dn_vers_reflectance(green)
    nir_refl = _dn_vers_reflectance(nir)
    with np.errstate(divide="ignore", invalid="ignore"):
        ndwi = (green_refl - nir_refl) / (green_refl + nir_refl)
    ndwi = np.nan_to_num(ndwi, nan=0.0, posinf=0.0, neginf=0.0)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(raster_sortie, largeur, hauteur, 1, gdal.GDT_Float32)
    out_ds.SetGeoTransform(geotransform)
    out_ds.SetProjection(projection)
    out_ds.GetRasterBand(1).SetNoDataValue(0.0)
    out_ds.GetRasterBand(1).WriteArray(ndwi)
    out_ds.FlushCache()
    out_ds = None
    return raster_sortie
