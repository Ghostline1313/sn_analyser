# -*- coding: utf-8 -*-
"""
core/precipitation.py
----------------------
Composante precipitation du sous-indice Climatique (IC) : cumul de
precipitations (mm) via l API Open-Meteo Historical Weather (reanalyse
ERA5/ERA5-Land, gratuite, sans cle API), en complement de la LST (deja
integree seule jusqu ici) pour approcher un risque climatique/de secheresse
plus complet (peu de pluie + forte chaleur de surface = risque cumule plus
eleve qu un seul des deux facteurs pris isolement).

Fenetre : annee civile complete (1er janvier - 31 decembre) si l annee
demandee est revolue ; sinon (annee en cours), 365 derniers jours glissants
se terminant hier, pour comparer des cumuls sur une duree toujours egale
(sans quoi une annee en cours, partiellement ecoulee, serait artificiellement
"moins pluvieuse" qu une annee complete).
"""

import json
import datetime
import urllib.request
import urllib.parse

API_URL = "https://archive-api.open-meteo.com/v1/archive"


def recuperer_precipitation_annuelle(lat, lon, annee, timeout=20):
    """Cumul de precipitations (mm) sur l annee demandee (ou 365 jours
    glissants si l annee est en cours).

    :param lat: latitude WGS84
    :param lon: longitude WGS84
    :param annee: annee de l analyse (int)
    :return: (cumul_mm, date_debut_iso, date_fin_iso) ou (None, None, None)
        si echec/donnees indisponibles.
    """
    aujourdhui = datetime.date.today()
    if annee >= aujourdhui.year:
        date_fin = aujourdhui - datetime.timedelta(days=1)
        date_debut = date_fin - datetime.timedelta(days=364)
    else:
        date_debut = datetime.date(annee, 1, 1)
        date_fin = datetime.date(annee, 12, 31)

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_debut.isoformat(),
        "end_date": date_fin.isoformat(),
        "daily": "precipitation_sum",
        "timezone": "Africa/Dakar",
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "SnAnalyser/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        valeurs = data.get("daily", {}).get("precipitation_sum", [])
        valeurs = [v for v in valeurs if v is not None]
        if not valeurs:
            return None, None, None
        return float(sum(valeurs)), date_debut.isoformat(), date_fin.isoformat()
    except Exception:
        return None, None, None


def calculer_climatologie_precipitation(lat, lon, annee_reference_fin, nb_annees=30, timeout=30):
    """Cumuls de precipitation (mm) par annee civile, sur les nb_annees
    annees precedant annee_reference_fin (non incluse) -- une seule requete
    couvrant toute la periode, regroupee par annee cote client (Open-Meteo
    ne fait pas l agregation annuelle lui-meme).

    :return: liste de cumuls annuels (mm), une valeur par annee disponible.
        Une annee sans donnees valides est simplement absente (pas comptee
        comme 0), pour ne pas biaiser la moyenne/l ecart-type.
    """
    date_debut = datetime.date(annee_reference_fin - nb_annees, 1, 1)
    date_fin = datetime.date(annee_reference_fin - 1, 12, 31)

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_debut.isoformat(),
        "end_date": date_fin.isoformat(),
        "daily": "precipitation_sum",
        "timezone": "Africa/Dakar",
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "SnAnalyser/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        dates = data.get("daily", {}).get("time", [])
        valeurs = data.get("daily", {}).get("precipitation_sum", [])
    except Exception:
        return []

    cumuls_par_annee = {}
    for date_str, valeur in zip(dates, valeurs):
        if valeur is None:
            continue
        annee = int(date_str[:4])
        cumuls_par_annee[annee] = cumuls_par_annee.get(annee, 0.0) + valeur
    return list(cumuls_par_annee.values())


def calculer_spi(lat, lon, annee, nb_annees_reference=30, timeout=30):
    """SPI (Standardized Precipitation Index) approche : z-score du cumul de
    precipitation de l annee demandee par rapport a la climatologie locale
    (moyenne + ecart-type des nb_annees_reference annees precedentes, meme
    point). Approximation courante du SPI complet (qui ajuste normalement
    une loi Gamma avant standardisation) -- suffisante pour un indicateur de
    tendance secheresse/humidite, sans le cout de calcul d un ajustement de
    distribution.

    Interpretation usuelle : SPI < -1.5 secheresse severe, -1.5 a -1.0
    secheresse moderee, -1.0 a +1.0 normal, > +1.0 humide.

    :return: SPI (float) ou None si donnees insuffisantes (moins de 10
        annees de reference exploitables, ou echec reseau).
    """
    import statistics

    precip_courante, _d1, _d2 = recuperer_precipitation_annuelle(lat, lon, annee, timeout=timeout)
    if precip_courante is None:
        return None

    cumuls_ref = calculer_climatologie_precipitation(lat, lon, annee, nb_annees_reference, timeout=timeout)
    if len(cumuls_ref) < 10:
        return None

    moyenne = statistics.mean(cumuls_ref)
    ecart_type = statistics.pstdev(cumuls_ref)
    if ecart_type == 0:
        return None
    return (precip_courante - moyenne) / ecart_type


def recuperer_climat_complementaire_annuel(lat, lon, annee, timeout=20):
    """Vent, humidite et evapotranspiration MOYENS/CUMULES sur l annee
    demandee (meme fenetre temporelle que recuperer_precipitation_annuelle),
    via la meme API Open-Meteo Historical Weather (reanalyse ERA5/ERA5-Land).

    Complement INFORMATIF du risque climatique (affiche a cote du score,
    pas encore integre au calcul -- les bornes de risque vent/humidite ne
    sont pas encore calibrees pour le contexte senegalais, contrairement a
    la LST et la secheresse/SPI deja validees).

    :return: dict {vent_moyen_kmh, humidite_moyenne_pct,
        evapotranspiration_cumul_mm}, valeurs a None individuellement si
        absentes de la reponse. Dict vide {} si la requete echoue entierement.
    """
    aujourdhui = datetime.date.today()
    if annee >= aujourdhui.year:
        date_fin = aujourdhui - datetime.timedelta(days=1)
        date_debut = date_fin - datetime.timedelta(days=364)
    else:
        date_debut = datetime.date(annee, 1, 1)
        date_fin = datetime.date(annee, 12, 31)

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_debut.isoformat(),
        "end_date": date_fin.isoformat(),
        "daily": "windspeed_10m_max,relative_humidity_2m_mean,et0_fao_evapotranspiration",
        "timezone": "Africa/Dakar",
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "SnAnalyser/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        daily = data.get("daily", {})
        vent = [v for v in daily.get("windspeed_10m_max", []) if v is not None]
        humidite = [v for v in daily.get("relative_humidity_2m_mean", []) if v is not None]
        eto = [v for v in daily.get("et0_fao_evapotranspiration", []) if v is not None]
        return {
            "vent_moyen_kmh": (sum(vent) / len(vent)) if vent else None,
            "humidite_moyenne_pct": (sum(humidite) / len(humidite)) if humidite else None,
            "evapotranspiration_cumul_mm": sum(eto) if eto else None,
        }
    except Exception:
        return {}
