# -*- coding: utf-8 -*-
"""
core/bsi.py
-----------
BSI = ((SWIR1 + RED) - (NIR + BLUE)) / ((SWIR1 + RED) + (NIR + BLUE))
Calcule sur la REFLECTANCE (pas le DN brut), meme raison qu'expliquee dans
core/ndvi.py.
Indice de sol nu : eleve sur les sols nus/artificialises, faible sur la
vegetation dense et l eau.
"""

import numpy as np
from osgeo import gdal

# Landsat Collection 2 Level-2 Surface Reflectance : conversion DN -> reflectance
# de surface (0-1). Voir core/ndvi.py pour le detail de cette constante.
_L2_SCALE = 0.0000275
_L2_OFFSET = -0.2


def _dn_vers_reflectance(dn):
    reflectance = dn * _L2_SCALE + _L2_OFFSET
    # Bornage [0.0001, 1] : les reflectances L2 legerement negatives (eau,
    # ombres) faisaient exploser les indices normalises (NDBI/NDWI > 8).
    reflectance = np.clip(reflectance, 0.0001, 1.0)
    return np.where(dn == 0, 0.0, reflectance)


def calculer_bsi_depuis_extrait(raster_extrait, raster_sortie,
                                 idx_blue=1, idx_red=2, idx_nir=3, idx_swir1=4):
    """Calcule le BSI a partir d UN SEUL extrait multi-bandes (ex: telecharge
    via core.landsat.telecharger_composite_annuel avec
    bandes=["blue", "red", "nir", "swir1", ...]).

    :param raster_extrait: chemin du GeoTIFF multi-bandes en entree
    :param raster_sortie: chemin du GeoTIFF BSI a produire
    :param idx_blue/idx_red/idx_nir/idx_swir1: index de bande (1-based)
    """
    ds = gdal.Open(raster_extrait)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir l extrait fourni")

    blue = ds.GetRasterBand(idx_blue).ReadAsArray().astype(np.float32)
    red = ds.GetRasterBand(idx_red).ReadAsArray().astype(np.float32)
    nir = ds.GetRasterBand(idx_nir).ReadAsArray().astype(np.float32)
    swir1 = ds.GetRasterBand(idx_swir1).ReadAsArray().astype(np.float32)

    blue_refl = _dn_vers_reflectance(blue)
    red_refl = _dn_vers_reflectance(red)
    nir_refl = _dn_vers_reflectance(nir)
    swir1_refl = _dn_vers_reflectance(swir1)

    with np.errstate(divide="ignore", invalid="ignore"):
        bsi = ((swir1_refl + red_refl) - (nir_refl + blue_refl)) / \
              ((swir1_refl + red_refl) + (nir_refl + blue_refl))
    bsi = np.nan_to_num(bsi, nan=0.0, posinf=0.0, neginf=0.0)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(raster_sortie, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Float32)
    out_ds.SetGeoTransform(ds.GetGeoTransform())
    out_ds.SetProjection(ds.GetProjection())
    out_ds.GetRasterBand(1).SetNoDataValue(0.0)
    out_ds.GetRasterBand(1).WriteArray(bsi)
    out_ds.FlushCache()
    out_ds = None
    return raster_sortie
