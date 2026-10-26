# -*- coding: utf-8 -*-
"""
core/aires_protegees.py
------------------------
Detection de proximite avec une aire protegee (parc national, reserve
naturelle...) via OSM (tags "boundary"="protected_area" et
"leisure"="nature_reserve"), pour enrichir l interpretation du risque.

Volontairement INFORMATIF plutot qu integre au score IRE : le but est
d affiner les recommandations (ex. "zone jouxtant une aire protegee ->
vigilance particuliere sur la degradation/l urbanisation en peripherie"),
pas d ajouter un 6e sous-indice qui complexifierait encore la ponderation
deja a 5 composantes (IDT/IU/IC/IV/IP).
"""

import json
import math
import urllib.request
import urllib.parse

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]


def _distance_haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def zone_protegee_proche(lat_centroide, lon_centroide, rayon_recherche_km=10, timeout=20):
    """Cherche l aire protegee la plus proche du centroide de la zone
    d etude (OSM boundary=protected_area / leisure=nature_reserve).

    :return: (trouvee: bool, nom: str ou None, distance_km: float ou None)
    """
    rayon_m = rayon_recherche_km * 1000
    requete = (
        "[out:json][timeout:{timeout}];"
        "("
        "node[\"boundary\"=\"protected_area\"](around:{rayon_m},{lat},{lon});"
        "way[\"boundary\"=\"protected_area\"](around:{rayon_m},{lat},{lon});"
        "relation[\"boundary\"=\"protected_area\"](around:{rayon_m},{lat},{lon});"
        "node[\"leisure\"=\"nature_reserve\"](around:{rayon_m},{lat},{lon});"
        "way[\"leisure\"=\"nature_reserve\"](around:{rayon_m},{lat},{lon});"
        "relation[\"leisure\"=\"nature_reserve\"](around:{rayon_m},{lat},{lon});"
        ");"
        "out center tags;"
    ).format(timeout=timeout, rayon_m=rayon_m, lat=lat_centroide, lon=lon_centroide)

    resultat = None
    for url in OVERPASS_MIRRORS:
        try:
            data = urllib.parse.urlencode({"data": requete}).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "SnAnalyser/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resultat = json.loads(resp.read().decode("utf-8"))
            break
        except Exception:
            continue
    if resultat is None:
        return False, None, None

    elements = resultat.get("elements", [])
    if not elements:
        return False, None, None

    meilleur = None
    for el in elements:
        if el.get("type") == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            centre = el.get("center")
            if not centre:
                continue
            lat, lon = centre.get("lat"), centre.get("lon")
        if lat is None or lon is None:
            continue
        d = _distance_haversine_km(lat_centroide, lon_centroide, lat, lon)
        if meilleur is None or d < meilleur[1]:
            nom = el.get("tags", {}).get("name") or el.get("tags", {}).get("name:fr") or "Aire protegee (nom non renseigne dans OSM)"
            meilleur = (nom, d)

    if meilleur is None:
        return False, None, None
    return True, meilleur[0], meilleur[1]
