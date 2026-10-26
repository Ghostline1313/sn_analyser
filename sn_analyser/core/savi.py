# -*- coding: utf-8 -*-
"""
core/savi.py
------------
SAVI (Soil Adjusted Vegetation Index) = (NIR - RED) / (NIR + RED + L) * (1+L),
L=0.5 (valeur standard, adaptee a une couverture vegetale faible a moderee -
pertinente pour le Senegal semi-aride), calcule sur la REFLECTANCE (meme
conversion DN->reflectance que core/ndvi.py).

Corrige l influence du sol nu sous-jacent sur le signal de vegetation, la ou
le NDVI seul peut etre biaise en zone de couverture vegetale clairsemee
(la majorite du territoire senegalais hors Casamance).
"""

import numpy as np
from osgeo import gdal

_L2_SCALE = 0.0000275
_L2_OFFSET = -0.2
L_SOL = 0.5


def _dn_vers_reflectance(dn):
    reflectance = dn * _L2_SCALE + _L2_OFFSET
    # Bornage [0.0001, 1] : les reflectances L2 legerement negatives (eau,
    # ombres) faisaient exploser les indices normalises (NDBI/NDWI > 8).
    reflectance = np.clip(reflectance, 0.0001, 1.0)
    return np.where(dn == 0, 0.0, reflectance)


def calculer_savi_depuis_extrait(raster_extrait, raster_sortie, idx_nir, idx_red, L=L_SOL):
    """Calcule le SAVI a partir d UN SEUL extrait multi-bandes.

    :param raster_extrait: chemin du GeoTIFF multi-bandes en entree
    :param raster_sortie: chemin du GeoTIFF SAVI a produire
    :param idx_nir: index de bande (1-based) correspondant au NIR
    :param idx_red: index de bande (1-based) correspondant au RED
    :param L: facteur d ajustement au sol (0 = equivalent NDVI, 1 = tres
        faible couverture vegetale ; 0.5 = usage general)
    """
    ds = gdal.Open(raster_extrait)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir l extrait fourni")

    nir = _dn_vers_reflectance(ds.GetRasterBand(idx_nir).ReadAsArray().astype(np.float32))
    red = _dn_vers_reflectance(ds.GetRasterBand(idx_red).ReadAsArray().astype(np.float32))

    with np.errstate(divide="ignore", invalid="ignore"):
        savi = (nir - red) / (nir + red + L) * (1.0 + L)
    savi = np.nan_to_num(savi, nan=0.0, posinf=0.0, neginf=0.0)
    savi = np.clip(savi, -1.0, 1.0)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(raster_sortie, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Float32)
    out_ds.SetGeoTransform(ds.GetGeoTransform())
    out_ds.SetProjection(ds.GetProjection())
    out_ds.GetRasterBand(1).SetNoDataValue(0.0)
    out_ds.GetRasterBand(1).WriteArray(savi)
    out_ds.FlushCache()
    out_ds = None
    return raster_sortie
