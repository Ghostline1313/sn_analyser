# -*- coding: utf-8 -*-
"""
core/no2.py
-----------
Sous-indice de Pollution (IP), composante NO2 (dioxyde d azote
tropospherique) : recherche et extraction de donnees Sentinel-5P/TROPOMI
(produit L2 NO2), via le catalogue STAC de Microsoft Planetary Computer
(collection "sentinel-5p-l2-netcdf" - meme service que celui deja utilise
pour Landsat dans core/landsat.py, meme mecanisme de signature d URL SAS).

PARTICULARITE par rapport a Landsat : les fichiers sont au format NetCDF
(grille en "swath", geolocalisation par pixel via des tableaux latitude/
longitude 2D, PAS une grille reguliere), et chaque fichier pese ~50-70 Mo
(un seul passage satellite). Pour obtenir une moyenne robuste sur une petite
zone (commune), plusieurs passages recents sont telecharges, filtres
(qa_value >= 0.75, seuil recommande par l ESA pour les pixels fiables), puis
agreges - chaque fichier est supprime immediatement apres extraction (pas de
stockage des .nc bruts, volumineux et non reutilisables tels quels).

Disponibilite : Sentinel-5P vole depuis fin 2018 (donnees exploitables a
partir de 2019). Pour les annees anterieures, aucune donnee n existe.
"""

import json
import os
import urllib.request

import numpy as np
import requests

STAC_SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SAS_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

QA_MIN = 0.75  # seuil qualite recommande ESA pour le NO2 tropospherique
NB_PIXELS_MIN_FIABLE = 3  # en dessous, la moyenne n est pas jugee fiable


def _sign_href(href):
    """Delegue a core.landsat._sign_href (jetons SAS en cache, replis)."""
    from .landsat import _sign_href as _signer
    return _signer(href)

def rechercher_items_no2(bbox, date_debut, date_fin, max_items=6):
    """Recherche les items Sentinel-5P NO2 (produit L2) intersectant la bbox
    sur la periode donnee, les plus recents en premier.

    :param bbox: [xmin, ymin, xmax, ymax] en WGS84
    :param date_debut: str "AAAA-MM-JJ"
    :param date_fin: str "AAAA-MM-JJ"
    :param max_items: nombre maximal d items retournes (limite le volume
        telecharge - chaque item pese ~50-70 Mo)
    :return: liste de features STAC (dict), triee du plus recent au plus ancien
    """
    body = {
        "collections": ["sentinel-5p-l2-netcdf"],
        "bbox": bbox,
        "datetime": date_debut + "/" + date_fin,
        "limit": 100,
        "sortby": [{"field": "datetime", "direction": "desc"}],
    }
    req = urllib.request.Request(
        STAC_SEARCH_URL, data=json.dumps(body).encode("utf-8"),
        headers={"User-Agent": "SnAnalyser/1.0", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    items_no2 = [f for f in data.get("features", []) if f["properties"].get("s5p:product_name") == "no2"]
    return items_no2[:max_items]


def _extraire_no2_bbox(nc_path, bbox, qa_min=QA_MIN):
    """Extrait les valeurs NO2 tropospherique (mol/m2) valides (qa_value >=
    qa_min, hors valeurs de remplissage) dont le pixel tombe dans la bbox.

    :param nc_path: chemin du fichier NetCDF telecharge
    :param bbox: [xmin, ymin, xmax, ymax] en WGS84
    :return: tableau numpy 1D des valeurs valides (peut etre vide)
    """
    import netCDF4 as nc

    xmin, ymin, xmax, ymax = bbox
    ds = nc.Dataset(nc_path)
    try:
        product = ds.groups["PRODUCT"]
        lat = product.variables["latitude"][0, :, :]
        lon = product.variables["longitude"][0, :, :]
        no2 = product.variables["nitrogendioxide_tropospheric_column"][0, :, :]
        qa = product.variables["qa_value"][0, :, :]
        fill = product.variables["nitrogendioxide_tropospheric_column"]._FillValue

        masque = (
            (lon >= xmin) & (lon <= xmax) &
            (lat >= ymin) & (lat <= ymax) &
            (qa >= qa_min) & (no2 != fill) & np.isfinite(no2)
        )
        return np.asarray(no2[masque])
    finally:
        ds.close()


def calculer_no2_moyen_zone(bbox, annee, work_dir, jours_recherche=45, max_items=3, pulse_callback=None,
                            budget_s=180):
    """Calcule la moyenne NO2 tropospherique (mol/m2) sur une zone, agregee
    sur plusieurs passages Sentinel-5P recents (fin de periode = 31/12 de
    l annee demandee, ou aujourd hui si l annee demandee est l annee en cours).

    Chaque fichier NetCDF est supprime immediatement apres extraction.

    :param bbox: [xmin, ymin, xmax, ymax] en WGS84
    :param annee: annee de l analyse (int)
    :param work_dir: dossier de travail temporaire (fichiers .nc intermediaires)
    :param jours_recherche: fenetre de recherche (jours avant la date de fin)
    :param max_items: nombre maximal de fichiers telecharges (volume/temps)
    :param pulse_callback: fonction sans argument appelee apres chaque
        telechargement (pour rafraichir l UI QGIS, optionnel)
    :return: (moyenne_mol_m2, nb_pixels_utilises) ou (None, 0) si aucune
        donnee exploitable (annee anterieure au lancement S5P, echec reseau,
        ou pas assez de pixels fiables).
    """
    import datetime

    if annee < 2019:
        return None, 0  # Sentinel-5P : donnees exploitables a partir de 2019

    aujourdhui = datetime.date.today()
    if annee >= aujourdhui.year:
        date_fin = aujourdhui
    else:
        date_fin = datetime.date(annee, 12, 31)
    date_debut = date_fin - datetime.timedelta(days=jours_recherche)

    try:
        items = rechercher_items_no2(bbox, date_debut.isoformat(), date_fin.isoformat(), max_items=max_items)
    except Exception:
        return None, 0

    if not items:
        return None, 0

    import time
    debut = time.time()
    toutes_valeurs = []
    for item in items:
        if time.time() - debut > budget_s:
            break  # budget de temps depasse : on garde les passages deja traites
        nc_path = os.path.join(work_dir, item["id"] + ".nc")
        try:
            href = item["assets"]["no2"]["href"]
            href_signe = _sign_href(href)
            with requests.get(href_signe, stream=True, timeout=60) as resp, open(nc_path, "wb") as out:
                resp.raise_for_status()
                for bloc in resp.iter_content(chunk_size=1 << 20):
                    out.write(bloc)
                    if time.time() - debut > budget_s + 60:
                        raise TimeoutError("budget Sentinel-5P depasse")

            valeurs = _extraire_no2_bbox(nc_path, bbox)
            if valeurs.size > 0:
                toutes_valeurs.append(valeurs)
        except Exception:
            pass  # un item defaillant ne doit pas bloquer l agregation des autres
        finally:
            if os.path.exists(nc_path):
                try:
                    os.remove(nc_path)
                except OSError:
                    pass
            if pulse_callback:
                pulse_callback()

    if not toutes_valeurs:
        return None, 0

    valeurs_combinees = np.concatenate(toutes_valeurs)
    if valeurs_combinees.size < NB_PIXELS_MIN_FIABLE:
        return None, valeurs_combinees.size

    return float(valeurs_combinees.mean()), int(valeurs_combinees.size)
