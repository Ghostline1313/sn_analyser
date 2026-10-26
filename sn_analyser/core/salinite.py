# -*- coding: utf-8 -*-
"""
core/salinite.py
-----------------
SI (Salinity Index, Khan et al. 2001) = sqrt(BLUE * RED), calcule sur la
REFLECTANCE (meme conversion DN->reflectance que core/ndvi.py).

Distinct du BSI (Bare Soil Index, deja utilise dans l IDT) : le BSI reagit a
toute absence de vegetation (sol nu generique), alors que le SI cible
specifiquement la brillance dans le bleu/rouge caracteristique des croutes
salines et sols salinises -- pertinent pour les zones estuariennes du
Senegal (mangroves degradees et tannes du Sine-Saloum et de la Casamance,
avancee du biseau sale dans le delta du fleuve Senegal).

Bornes de normalisation APPROXIMATIVES (0.02 = vegetation/sol sombre, risque
nul ; 0.25 = croute saline/sol tres reflechissant, risque maximal) : a
recalibrer avec des mesures de terrain (conductivite electrique du sol) si
disponibles, comme deja signale pour les autres indices spectraux du plugin.
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


def calculer_si_depuis_extrait(raster_extrait, raster_sortie, idx_blue, idx_red):
    """Calcule le SI (Salinity Index) a partir d UN SEUL extrait multi-bandes.

    :param raster_extrait: chemin du GeoTIFF multi-bandes en entree
    :param raster_sortie: chemin du GeoTIFF SI a produire
    :param idx_blue: index de bande (1-based) correspondant au BLUE
    :param idx_red: index de bande (1-based) correspondant au RED
    """
    ds = gdal.Open(raster_extrait)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir l extrait fourni")

    blue = _dn_vers_reflectance(ds.GetRasterBand(idx_blue).ReadAsArray().astype(np.float32))
    red = _dn_vers_reflectance(ds.GetRasterBand(idx_red).ReadAsArray().astype(np.float32))

    with np.errstate(invalid="ignore"):
        si = np.sqrt(np.clip(blue * red, 0.0, None))
    si = np.nan_to_num(si, nan=0.0, posinf=0.0, neginf=0.0)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(raster_sortie, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Float32)
    out_ds.SetGeoTransform(ds.GetGeoTransform())
    out_ds.SetProjection(ds.GetProjection())
    out_ds.GetRasterBand(1).SetNoDataValue(0.0)
    out_ds.GetRasterBand(1).WriteArray(si)
    out_ds.FlushCache()
    out_ds = None
    return raster_sortie
