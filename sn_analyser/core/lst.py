# -*- coding: utf-8 -*-
"""
core/lst.py
-----------
LST (Land Surface Temperature), en degres Celsius, a partir de la bande
thermique Landsat Collection 2 Level-2 Surface Temperature (produit ST,
DISTINCT du produit SR utilise par ndvi/ndbi/nbr/bsi - facteurs d echelle
propres, documentation USGS Landsat C2 L2 Science Product Guide) :

    Kelvin = DN * 0.00341802 + 149.0
    Celsius = Kelvin - 273.15

ATTENTION (a verifier empiriquement, meme demarche que le facteur SR documente
dans core/ndvi.py) : cette formule est celle du produit ST officiel USGS/
Planetary Computer (asset "lwir11"). Si la bande "thermal1" servie par Living
Atlas ne correspond pas exactement a ce meme produit DN, les valeurs Celsius
obtenues peuvent etre decalees - comparer un resultat a une temperature de
surface connue (ex : zone urbaine dense en saison seche, generalement 35-45C
au Senegal) avant utilisation operationnelle.
"""

import numpy as np
from osgeo import gdal

_ST_SCALE = 0.00341802
_ST_OFFSET_K = 149.0
_KELVIN_TO_CELSIUS = 273.15


def calculer_lst_depuis_extrait(raster_extrait, raster_sortie, idx_thermal=1):
    """Calcule le LST (degres Celsius) a partir d UN SEUL extrait multi-bandes
    contenant la bande thermique (ex: telecharge via
    core.landsat.telecharger_composite_annuel avec bandes=[..., "thermal1"]).

    :param raster_extrait: chemin du GeoTIFF multi-bandes en entree
    :param raster_sortie: chemin du GeoTIFF LST (Celsius) a produire
    :param idx_thermal: index de bande (1-based) correspondant a la bande thermique
    """
    ds = gdal.Open(raster_extrait)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir l extrait fourni")

    dn = ds.GetRasterBand(idx_thermal).ReadAsArray().astype(np.float32)
    kelvin = dn * _ST_SCALE + _ST_OFFSET_K
    celsius = kelvin - _KELVIN_TO_CELSIUS
    celsius = np.where(dn == 0, 0.0, celsius)
    celsius = np.nan_to_num(celsius, nan=0.0, posinf=0.0, neginf=0.0)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(raster_sortie, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Float32)
    out_ds.SetGeoTransform(ds.GetGeoTransform())
    out_ds.SetProjection(ds.GetProjection())
    out_ds.GetRasterBand(1).SetNoDataValue(0.0)
    out_ds.GetRasterBand(1).WriteArray(celsius)
    out_ds.FlushCache()
    out_ds = None
    return raster_sortie
