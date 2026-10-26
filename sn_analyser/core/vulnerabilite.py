# -*- coding: utf-8 -*-
"""
core/vulnerabilite.py
----------------------
Sous-indice de Vulnerabilite (IV) : composantes d exposition/vulnerabilite
pour l Indice de Risque Environnemental (IRE).

Deux composantes :
  1. Densite de population (raster WorldPop 2020, 1km, bundle dans
     data/sen_population_2020_1km.tif - voir core/ire.py) : plus la
     densite est elevee, plus l exposition aux risques environnementaux
     (population impactee) est grande.
  2. Proximite aux infrastructures sensibles (hopitaux/cliniques/ecoles,
     via l API Overpass/OpenStreetMap) : plus la distance a la structure la
     plus proche est grande, moins la zone est bien desservie en cas de
     crise (acces aux secours, evacuation...) -> vulnerabilite plus elevee.
"""

import json
import math
import urllib.request
import urllib.parse

from .utils import clip_raster_to_geometry, moyenne_raster

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Miroirs de secours : overpass-api.de est parfois sature/lent (observe
# empiriquement en session -- une requete similaire qui reussit en isolation
# peut echouer dans le pipeline complet a cause de la charge du service au
# moment de l appel). On tente chaque miroir a son tour plutot que d abandonner
# IV a la seule densite de population des le premier echec.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]


def moyenne_densite_population(raster_population, geojson_path, raster_clip_sortie):
    """Decoupe le raster WorldPop bundle a la geometrie de la zone d etude et
    calcule la densite de population moyenne (habitants / pixel ~1km2).

    :param raster_population: chemin du raster WorldPop bundle (data/sen_population_2020_1km.tif)
    :param geojson_path: chemin du GeoJSON de la zone d etude (EPSG:4326)
    :param raster_clip_sortie: chemin du raster decoupe a produire
    :return: densite moyenne (habitants par pixel ~1km2, float)
    """
    clip_raster_to_geometry(raster_population, geojson_path, raster_clip_sortie, nodata=-99999.0)
    return moyenne_raster(raster_clip_sortie, nodata=-99999.0)


def _distance_haversine_km(lat1, lon1, lat2, lon2):
    """Distance a vol d oiseau (km) entre deux points WGS84 (formule de
    Haversine, rayon terrestre moyen 6371 km)."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


# Categories d infrastructures sensibles regroupees par fonction. La
# distance est calculee SEPAREMENT pour chaque groupe (voir
# distances_infrastructures_par_categorie) plutot qu une seule distance "au
# plus proche tout type confondu" : une ecole a cote de chez soi ne compense
# pas l absence d hopital a proximite, les deux vulnerabilites sont
# independantes. cle OSM "amenity" pour tous, sauf marche qui peut aussi
# porter shop=marketplace selon le contributeur -- les deux sont couverts.
CATEGORIES_GROUPES = {
    "sante": ("hospital", "clinic", "doctors", "pharmacy"),
    "education": ("school", "university", "college"),
    "securite_urgence": ("police", "fire_station"),
    "marche": ("marketplace",),
}

LIBELLES_GROUPES = {
    "sante": "Sante (hopital/clinique/medecin/pharmacie)",
    "education": "Education (ecole/universite)",
    "securite_urgence": "Securite/urgence (police/pompiers)",
    "marche": "Marche",
}


def distance_infrastructure_proche(bbox, lat_centroide, lon_centroide,
                                    categories=("hospital", "clinic", "school"),
                                    rayon_recherche_km=20, timeout=45):
    """CONSERVEE POUR COMPATIBILITE ASCENDANTE (usage externe eventuel) :
    distance a l infrastructure la plus proche, tout type confondu. Pour le
    calcul de l IV, preferer distances_infrastructures_par_categorie (voir
    docstring de CATEGORIES_GROUPES ci-dessus pour la justification).
    """
    resultat = _requete_overpass_around(categories, lat_centroide, lon_centroide,
                                         rayon_recherche_km, timeout)
    if resultat is None:
        return None
    distance_min = None
    for el in resultat.get("elements", []):
        lat, lon = _coord_element(el)
        if lat is None:
            continue
        d = _distance_haversine_km(lat_centroide, lon_centroide, lat, lon)
        if distance_min is None or d < distance_min:
            distance_min = d
    return distance_min


def distances_infrastructures_par_categorie(lat_centroide, lon_centroide,
                                              rayon_recherche_km=20, timeout=45):
    """Distance (km) a l infrastructure la plus proche, calculee SEPAREMENT
    pour chaque groupe de CATEGORIES_GROUPES (une seule requete Overpass
    regroupant tous les tags amenity concernes, puis ventilation locale par
    tag renvoye -- evite de multiplier les appels reseau).

    :return: dict {groupe: distance_km ou None si aucune structure de ce
        groupe trouvee dans le rayon de recherche / requete echouee}
    """
    toutes_categories = sorted(set(c for cats in CATEGORIES_GROUPES.values() for c in cats))
    resultat = _requete_overpass_around(toutes_categories, lat_centroide, lon_centroide,
                                         rayon_recherche_km, timeout)
    distances = {g: None for g in CATEGORIES_GROUPES}
    if resultat is None:
        return distances

    for el in resultat.get("elements", []):
        amenity = el.get("tags", {}).get("amenity")
        if not amenity:
            continue
        groupe = next((g for g, cats in CATEGORIES_GROUPES.items() if amenity in cats), None)
        if groupe is None:
            continue
        lat, lon = _coord_element(el)
        if lat is None:
            continue
        d = _distance_haversine_km(lat_centroide, lon_centroide, lat, lon)
        if distances[groupe] is None or d < distances[groupe]:
            distances[groupe] = d

    return distances


def distance_industrie_proche(lat_centroide, lon_centroide, rayon_recherche_km=20, timeout=45):
    """Distance (km) a l infrastructure industrielle la plus proche
    (landuse=industrial ou man_made=works, OpenStreetMap/Overpass) --
    utilisee comme composante du risque Pollution (proximite industrielle =
    source potentielle d emissions, voir core/ire.calculer_ip).

    Requete separee de distances_infrastructures_par_categorie car portant
    sur des cles OSM differentes (landuse/man_made, pas amenity).

    :return: distance_km (float) ou None si aucune zone industrielle trouvee
        dans le rayon de recherche / requete echouee.
    """
    rayon_m = rayon_recherche_km * 1000
    requete = (
        "[out:json][timeout:{timeout}];"
        "(way[\"landuse\"=\"industrial\"](around:{rayon_m},{lat},{lon});"
        "node[\"man_made\"=\"works\"](around:{rayon_m},{lat},{lon});"
        "way[\"man_made\"=\"works\"](around:{rayon_m},{lat},{lon}););"
        "out center tags;"
    ).format(timeout=timeout, rayon_m=rayon_m, lat=lat_centroide, lon=lon_centroide)

    for url in OVERPASS_MIRRORS:
        try:
            data = urllib.parse.urlencode({"data": requete}).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "SnAnalyser/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resultat = json.loads(resp.read().decode("utf-8"))
            break
        except Exception:
            resultat = None
            continue

    if resultat is None:
        return None

    distance_min = None
    for el in resultat.get("elements", []):
        lat, lon = _coord_element(el)
        if lat is None:
            continue
        d = _distance_haversine_km(lat_centroide, lon_centroide, lat, lon)
        if distance_min is None or d < distance_min:
            distance_min = d
    return distance_min


def distance_reseau_hydrographique_proche(lat_centroide, lon_centroide, rayon_recherche_km=20, timeout=45):
    """Distance (km) au cours d eau le plus proche (OpenStreetMap,
    waterway=river/stream/canal) -- composante du risque Hydrologique (voir
    core/ire.calculer_ih). Complementaire au NDWI : le reseau hydrographique
    OSM est cartographie independamment de la date de l image satellite
    (couvre aussi les cours d eau temporaires/saisonniers mal detectes par
    NDWI en saison seche).

    :return: distance_km (float) ou None si aucun cours d eau trouve dans le
        rayon de recherche / requete echouee.
    """
    rayon_m = rayon_recherche_km * 1000
    requete = (
        "[out:json][timeout:{timeout}];"
        "(way[\"waterway\"~\"^(river|stream|canal)$\"](around:{rayon_m},{lat},{lon}););"
        "out center tags;"
    ).format(timeout=timeout, rayon_m=rayon_m, lat=lat_centroide, lon=lon_centroide)
    return _distance_min_requete_overpass(requete, lat_centroide, lon_centroide)


def distance_lac_proche(lat_centroide, lon_centroide, rayon_recherche_km=20, timeout=45):
    """Distance (km) au plan d eau permanent le plus proche (OpenStreetMap,
    natural=water ou water=lake/reservoir) -- composante du risque
    Hydrologique (voir core/ire.calculer_ih). Complementaire au NDWI : capture
    les plans d eau permanents meme hors periode ou l eau est visible sur
    l image satellite utilisee pour l analyse.

    :return: distance_km (float) ou None si aucun plan d eau trouve dans le
        rayon de recherche / requete echouee.
    """
    rayon_m = rayon_recherche_km * 1000
    requete = (
        "[out:json][timeout:{timeout}];"
        "(way[\"natural\"=\"water\"](around:{rayon_m},{lat},{lon});"
        "way[\"water\"~\"^(lake|reservoir)$\"](around:{rayon_m},{lat},{lon}););"
        "out center tags;"
    ).format(timeout=timeout, rayon_m=rayon_m, lat=lat_centroide, lon=lon_centroide)
    return _distance_min_requete_overpass(requete, lat_centroide, lon_centroide)


def _distance_min_requete_overpass(requete, lat_centroide, lon_centroide):
    """Execute une requete Overpass (avec bascule sur OVERPASS_MIRRORS) et
    retourne la distance (km) a l element le plus proche du centroide, ou
    None si aucun resultat/echec."""
    resultat = None
    for url in OVERPASS_MIRRORS:
        try:
            data = urllib.parse.urlencode({"data": requete}).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "SnAnalyser/1.0"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                resultat = json.loads(resp.read().decode("utf-8"))
            break
        except Exception:
            continue

    if resultat is None:
        return None

    distance_min = None
    for el in resultat.get("elements", []):
        lat, lon = _coord_element(el)
        if lat is None:
            continue
        d = _distance_haversine_km(lat_centroide, lon_centroide, lat, lon)
        if distance_min is None or d < distance_min:
            distance_min = d
    return distance_min


def _coord_element(el):
    """Extrait (lat, lon) d un element Overpass (node direct, way/relation
    via son 'center'). Renvoie (None, None) si indisponible."""
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    centre = el.get("center")
    if not centre:
        return None, None
    return centre.get("lat"), centre.get("lon")


def _requete_overpass_around(categories, lat_centroide, lon_centroide,
                              rayon_recherche_km, timeout):
    """Requete Overpass "around" (centree sur le centroide, rayon fixe --
    voir justification dans distance_infrastructure_proche/l historique de ce
    module) pour un ensemble de valeurs "amenity", avec bascule sur
    OVERPASS_MIRRORS en cas d echec. Renvoie le JSON decode ou None."""
    rayon_m = rayon_recherche_km * 1000
    filtre_amenity = "|".join(categories)
    requete = (
        "[out:json][timeout:{timeout}];"
        "(node[\"amenity\"~\"^({filtre})$\"](around:{rayon_m},{lat},{lon});"
        "way[\"amenity\"~\"^({filtre})$\"](around:{rayon_m},{lat},{lon}););"
        "out center tags;"
    ).format(timeout=timeout, filtre=filtre_amenity, rayon_m=rayon_m,
              lat=lat_centroide, lon=lon_centroide)

    for url in OVERPASS_MIRRORS:
        try:
            data = urllib.parse.urlencode({"data": requete}).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "SnAnalyser/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            continue
    return None
