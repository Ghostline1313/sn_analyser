# -*- coding: utf-8 -*-
"""
core/pollution.py
------------------
Composante NO2 (surface) du sous-indice de Pollution (IP) via l API
Open-Meteo Air Quality (modele CAMS - Copernicus Atmosphere Monitoring
Service), le meme service que celui utilise par le plugin sen_climate
(air_quality_service.py). Une seule requete HTTP par point (centroide de la
zone), reponse quasi instantanee, aucune cle API requise.

Alternative rapide au pipeline Sentinel-5P (core/no2.py) : ce dernier mesure
la colonne tropospherique de NO2 par satellite (plus precis spatialement,
plus proche de la "verite terrain" en zone urbaine dense), mais telecharge
plusieurs fichiers NetCDF de 50-70 Mo (plusieurs minutes). Open-Meteo est
utilise par defaut dans l IRE ; Sentinel-5P reste disponible en option
(case a cocher dediee dans l UI) pour un resultat plus precis quand le temps
de calcul n est pas contraignant.
"""

import json
import urllib.request
from .utils import ouvrir_url
import urllib.parse

API_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"


def recuperer_no2_open_meteo(lat, lon, timeout=15):
    """Recupere la concentration NO2 de surface (microg/m3, modele CAMS) au
    point donne.

    :param lat: latitude WGS84
    :param lon: longitude WGS84
    :return: (no2_ugm3, donnees_completes) ou (None, None) si echec/indisponible.
        donnees_completes contient egalement pm2_5, pm10, ozone, so2, co,
        european_aqi si disponibles (utile pour un futur affichage detaille).
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "nitrogen_dioxide,pm2_5,pm10,ozone,sulphur_dioxide,carbon_monoxide,european_aqi",
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "SnAnalyser/1.0"})
    try:
        with ouvrir_url(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        current = data.get("current", {})
        no2 = current.get("nitrogen_dioxide")
        if no2 is None:
            return None, None
        return float(no2), current
    except Exception:
        return None, None
