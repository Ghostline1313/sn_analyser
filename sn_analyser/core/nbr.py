# -*- coding: utf-8 -*-
"""
core/nbr.py
-----------
NBR = (NIR - SWIR2) / (NIR + SWIR2), calcule sur la REFLECTANCE (pas le DN
brut), meme raison qu'expliquee dans core/ndvi.py : l'offset -0.2 (constant
sur les deux bandes) ne s'annule pas dans le ratio.
Indice de zones brulees / degradation de la vegetation. Une vegetation saine
a un NBR eleve (~0.1 a 0.4+) ; une zone brulee ou fortement degradee a un NBR
faible ou negatif.
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


def calculer_nbr(raster_nir, raster_swir2, raster_sortie):
    """Calcule le NBR a partir de DEUX fichiers separes (NIR et SWIR2).

    :param raster_nir: chemin de la bande NIR
    :param raster_swir2: chemin de la bande SWIR2
    :param raster_sortie: chemin du GeoTIFF NBR a produire
    """
    nir_ds = gdal.Open(raster_nir)
    swir2_ds = gdal.Open(raster_swir2)
    if nir_ds is None or swir2_ds is None:
        raise RuntimeError("Impossible d ouvrir les bandes NIR/SWIR2 fournies")

    nir = nir_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    swir2 = swir2_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)

    return _ecrire_nbr(nir, swir2, nir_ds.GetGeoTransform(), nir_ds.GetProjection(),
                        nir_ds.RasterXSize, nir_ds.RasterYSize, raster_sortie)


def calculer_nbr_depuis_extrait(raster_extrait, raster_sortie, idx_nir=1, idx_swir2=2):
    """Calcule le NBR a partir d UN SEUL extrait multi-bandes (ex: telecharge
    via core.landsat.telecharger_extrait/telecharger_composite_annuel avec
    bandes=[..., "nir", ..., "swir2", ...]).

    :param raster_extrait: chemin du GeoTIFF multi-bandes en entree
    :param raster_sortie: chemin du GeoTIFF NBR a produire
    :param idx_nir: index de bande (1-based) correspondant au NIR
    :param idx_swir2: index de bande (1-based) correspondant au SWIR2
    """
    ds = gdal.Open(raster_extrait)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir l extrait fourni")

    nir = ds.GetRasterBand(idx_nir).ReadAsArray().astype(np.float32)
    swir2 = ds.GetRasterBand(idx_swir2).ReadAsArray().astype(np.float32)

    return _ecrire_nbr(nir, swir2, ds.GetGeoTransform(), ds.GetProjection(),
                        ds.RasterXSize, ds.RasterYSize, raster_sortie)


def _ecrire_nbr(nir, swir2, geotransform, projection, largeur, hauteur, raster_sortie):
    nir_refl = _dn_vers_reflectance(nir)
    swir2_refl = _dn_vers_reflectance(swir2)
    with np.errstate(divide="ignore", invalid="ignore"):
        nbr = (nir_refl - swir2_refl) / (nir_refl + swir2_refl)
    nbr = np.nan_to_num(nbr, nan=0.0, posinf=0.0, neginf=0.0)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(raster_sortie, largeur, hauteur, 1, gdal.GDT_Float32)
    out_ds.SetGeoTransform(geotransform)
    out_ds.SetProjection(projection)
    out_ds.GetRasterBand(1).SetNoDataValue(0.0)
    out_ds.GetRasterBand(1).WriteArray(nbr)
    out_ds.FlushCache()
    out_ds = None
    return raster_sortie
