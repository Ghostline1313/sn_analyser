# -*- coding: utf-8 -*-
"""
core/evi.py
-----------
EVI (Enhanced Vegetation Index) = 2.5 * (NIR - RED) / (NIR + 6*RED - 7.5*BLUE + 1),
calcule sur la REFLECTANCE (meme conversion DN->reflectance que core/ndvi.py).

Ameliore le NDVI dans deux cas frequents au Senegal :
  - canopee dense (zones boisees du sud) : le NDVI sature (s approche de 1
    sans discriminer davantage), l EVI reste sensible grace au terme de
    correction atmospherique/canopee.
  - sol nu/faible couverture (nord semi-aride) : le terme -7.5*BLUE corrige
    en partie le bruit du sol, la ou le NDVI seul peut etre perturbe par la
    reflectance du sol nu sous-jacent.
"""

import numpy as np
from osgeo import gdal

_L2_SCALE = 0.0000275
_L2_OFFSET = -0.2


def _dn_vers_reflectance(dn):
    reflectance = dn * _L2_SCALE + _L2_OFFSET
    # Bornage [0.0001, 1] : les reflectances L2 legerement negatives (eau,
    # ombres) faisaient exploser les indices normalises (NDBI/NDWI > 8).
    reflectance = np.clip(reflectance, 0.0001, 1.0)
    return np.where(dn == 0, 0.0, reflectance)


def calculer_evi_depuis_extrait(raster_extrait, raster_sortie, idx_nir, idx_red, idx_blue):
    """Calcule l EVI a partir d UN SEUL extrait multi-bandes.

    :param raster_extrait: chemin du GeoTIFF multi-bandes en entree
    :param raster_sortie: chemin du GeoTIFF EVI a produire
    :param idx_nir: index de bande (1-based) correspondant au NIR
    :param idx_red: index de bande (1-based) correspondant au RED
    :param idx_blue: index de bande (1-based) correspondant au BLUE
    """
    ds = gdal.Open(raster_extrait)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir l extrait fourni")

    nir = _dn_vers_reflectance(ds.GetRasterBand(idx_nir).ReadAsArray().astype(np.float32))
    red = _dn_vers_reflectance(ds.GetRasterBand(idx_red).ReadAsArray().astype(np.float32))
    blue = _dn_vers_reflectance(ds.GetRasterBand(idx_blue).ReadAsArray().astype(np.float32))

    with np.errstate(divide="ignore", invalid="ignore"):
        evi = 2.5 * (nir - red) / (nir + 6.0 * red - 7.5 * blue + 1.0)
    evi = np.nan_to_num(evi, nan=0.0, posinf=0.0, neginf=0.0)
    # bornes physiques usuelles de l EVI (au-dela, artefact numerique a filtrer)
    evi = np.clip(evi, -1.0, 1.0)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(raster_sortie, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Float32)
    out_ds.SetGeoTransform(ds.GetGeoTransform())
    out_ds.SetProjection(ds.GetProjection())
    out_ds.GetRasterBand(1).SetNoDataValue(0.0)
    out_ds.GetRasterBand(1).WriteArray(evi)
    out_ds.FlushCache()
    out_ds = None
    return raster_sortie
