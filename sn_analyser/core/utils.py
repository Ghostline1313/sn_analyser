# -*- coding: utf-8 -*-
"""
core/utils.py
-------------
Fonctions utilitaires communes à tous les modules SN Analyser.
"""

import os


def get_temp_dir(plugin_dir):
    """Retourne (et crée si besoin) le dossier temp/ du plugin."""
    temp_dir = os.path.join(plugin_dir, "temp")
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def get_reports_dir(plugin_dir):
    """Retourne (et crée si besoin) le dossier reports/ du plugin."""
    reports_dir = os.path.join(plugin_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    return reports_dir


def annees_comparaison(annee_debut, annee_fin, jalons=(1984, 1994, 2004, 2014, 2026)):
    """Construit la liste des paires d'années à comparer (1984-1994, 1994-2004, ...)
    en se limitant à l'intervalle [annee_debut, annee_fin].
    """
    jalons_filtres = [a for a in jalons if annee_debut <= a <= annee_fin]
    return list(zip(jalons_filtres[:-1], jalons_filtres[1:]))


def formater_pourcentage(valeur):
    """Formate une variation en pourcentage avec signe (+/-)."""
    signe = "+" if valeur >= 0 else ""
    return f"{signe}{valeur:.1f} %"

def moyenne_raster(raster_path, nodata=0.0):
    """Calcule la moyenne des valeurs exploitables d un raster mono-bande
    (nodata exclu). Utilise pour les indicateurs de l IRE (NDVI/NBR/BSI/NDBI
    moyens sur la zone d etude, apres decoupage a la geometrie).

    :param raster_path: chemin du raster (deja rogne a la zone d etude)
    :param nodata: valeur consideree comme nodata (exclue du calcul)
    :return: moyenne (float). 0.0 si aucun pixel exploitable.
    """
    from osgeo import gdal
    import numpy as np

    ds = gdal.Open(raster_path)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir le raster fourni : " + str(raster_path))
    arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    ds = None

    valeurs = arr[np.isfinite(arr)]
    valeurs = valeurs[valeurs != nodata]
    if valeurs.size == 0:
        return 0.0
    return float(valeurs.mean())


def clip_raster_to_geometry(raster_in, geojson_path, raster_out, nodata=0, cutline_srs="EPSG:4326"):
    """Rogne un raster a l emprise exacte d une geometrie (GeoJSON).

    Le GeoJSON de decoupe est suppose en EPSG:4326 (WGS84, standard GeoJSON) ;
    GDAL le reprojette a la volee dans le CRS du raster source.

    :param raster_in: chemin du raster source
    :param geojson_path: chemin d un fichier GeoJSON contenant la geometrie de decoupe
    :param raster_out: chemin du raster de sortie (rogne)
    :param nodata: valeur nodata a affecter en dehors de la geometrie
    :param cutline_srs: CRS du GeoJSON de decoupe (par defaut EPSG:4326)
    """
    from osgeo import gdal
    gdal.Warp(
        raster_out,
        raster_in,
        cutlineDSName=geojson_path,
        cutlineSRS=cutline_srs,
        cropToCutline=True,
        dstNodata=nodata,
    )
    return raster_out


# Libelles humains pour les cles de resultats produites par les pipelines
# d analyse (urbanisation, environnement, erosion), utilises par les modules
# d export (PDF/Word/Excel) pour construire les tableaux de synthese.
INDICATOR_LABELS = {
    "surface_urbanisee_debut": "Surface batie annee debut (ha)",
    "surface_urbanisee_fin": "Surface batie annee fin (ha)",
    "taux_croissance_urbaine": "Croissance urbaine annuelle (%/an)",
    "surface_vegetation_debut": "Surface vegetalisee annee debut (ha)",
    "surface_vegetation_fin": "Surface vegetalisee annee fin (ha)",
    "erosion_ha": "Surface erodee, terre vers eau (ha)",
    "accretion_ha": "Surface gagnee, eau vers terre (ha)",
    "bilan_erosion_ha": "Bilan cotier net (ha, positif = recul)",
    "ire_score": "Indice de Risque Environnemental - IRE (score /100)",
    "ire_niveau": "Niveau de risque environnemental (IRE)",
    "idt_score": "Sous-indice Degradation des Terres - IDT (score /100)",
    "iu_score": "Sous-indice Urbanisation - IU (score /100)",
    "ic_score": "Sous-indice Climatique partiel - IC, LST uniquement (score /100)",
    "iv_score": "Sous-indice Vulnerabilite - IV (score /100)",
    "ire_phase": "IRE - phase d implementation",
    "ire_composantes": "IRE - composantes prises en compte",
    "ire_composantes_manquantes": "IRE - composantes non encore implementees",
}


def lire_raster(raster_path, nodata=0.0):
    """Lit un raster mono-bande et retourne (array, geotransform, projection,
    masque_valide). Complement de moyenne_raster : evite de relire deux fois
    le meme fichier quand on a besoin a la fois de la moyenne ET du tableau
    complet (ex : generation d un raster de risque classifie par pixel).

    :param nodata: valeur consideree comme nodata (exclue via masque_valide)
    :return: (array float64, geotransform, projection (wkt str),
        masque_valide (array bool, True = pixel exploitable))
    """
    from osgeo import gdal
    import numpy as np

    ds = gdal.Open(raster_path)
    if ds is None:
        raise RuntimeError("Impossible d ouvrir le raster fourni : " + str(raster_path))
    arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    geotransform = ds.GetGeoTransform()
    projection = ds.GetProjection()
    ds = None

    masque_valide = np.isfinite(arr) & (arr != nodata)
    return arr, geotransform, projection, masque_valide


def ouvrir_url(req, timeout=30):
    """urllib.request.urlopen limite au protocole HTTPS (aucun file:// ni
    schema personnalise : toutes les URL du plugin sont des services web
    publics en https)."""
    import urllib.request
    url = getattr(req, "full_url", req)
    if not str(url).lower().startswith("https://"):
        raise ValueError("URL non autorisee (https requis) : " + str(url)[:80])
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310 - schema https verifie ci-dessus
