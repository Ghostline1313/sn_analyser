# -*- coding: utf-8 -*-
"""
core/landsat.py
----------------
Recherche et telechargement d extraits Landsat cibles sur la zone d etude.

Source principale : ArcGIS Living Atlas "Landsat/MS" Image Service (acces
public, sans authentification, donnees radiometriques brutes S16, mise a
jour quasi temps reel). Un export cible (bbox + bandes) est demande
directement au serveur : quelques centaines de Ko a 1-2 Mo par requete,
au lieu de telecharger une scene complete (~80 Mo/bande).

Source de secours : catalogue STAC public de Microsoft Planetary Computer
(Landsat Collection 2 Level-2), utilise si le service Living Atlas echoue
(scenes anciennes, indisponibilite temporaire, etc.). Chaque bande demandee
est telechargee integralement puis les bandes sont fusionnees et decoupees
a la bbox de la zone d etude via GDAL, pour produire un extrait multi-bandes
comparable a celui de Living Atlas.
"""

import os
import json
import requests

LIVING_ATLAS_URL = "https://landsat2.arcgis.com/arcgis/rest/services/Landsat/MS/ImageServer"
STAC_SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SAS_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

# Index de bandes (0-based) du service Living Atlas Landsat/MS
BAND_INDEX = {
    "coastal": 0,
    "blue": 1,
    "green": 2,
    "red": 3,
    "nir": 4,
    "swir1": 5,
    "swir2": 6,
    "cirrus": 7,
    "qa": 8,
    "thermal1": 9,
    "thermal2": 10,
}

# Correspondance vers les noms d assets Planetary Computer (secours)
_PC_ASSET_NAMES = {
    "coastal": "coastal", "blue": "blue", "green": "green", "red": "red",
    "nir": "nir08", "swir1": "swir16", "swir2": "swir22", "thermal1": "lwir11",
    "qa": "qa_pixel",
}

# Resolution native des bandes multispectrales Landsat (hors panchromatique).
# Utilisee pour fixer explicitement la taille des extraits : sans cela, le
# service peut renvoyer une taille par defaut qui ne correspond pas a la
# resolution native (ex: 400x400 px pour une bbox de plusieurs dizaines de km,
# soit ~110 m/pixel au lieu de 30 m), ce qui degrade fortement le detail
# spatial ET peut varier d une requete a l autre (donc entre les dates
# comparees), faussant la comparaison multi-temporelle.
RESOLUTION_M = 30


def _dimensions_export(bbox, resolution_m=RESOLUTION_M, sr_metrique=32628):
    """Calcule la taille (largeur_px, hauteur_px) d un export a resolution
    metrique fixe pour une bbox en EPSG:4326, en la reprojetant dans un CRS
    metrique (UTM 28N par defaut, coherent avec imageSR/dstSRS utilise pour
    le telechargement).

    Appeler cette fonction avec la MEME bbox pour toutes les dates comparees
    garantit une taille de sortie identique (meme grille de pixels), ce qui
    est indispensable pour que les analyses (surfaces, comparaison eau/terre)
    restent coherentes entre deux dates.
    """
    from osgeo import osr

    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(sr_metrique)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(src, dst)

    xmin, ymin, xmax, ymax = bbox
    x1, y1, _ = transform.TransformPoint(xmin, ymin)
    x2, y2, _ = transform.TransformPoint(xmax, ymax)
    largeur_m = abs(x2 - x1)
    hauteur_m = abs(y2 - y1)

    largeur_px = max(1, round(largeur_m / resolution_m))
    hauteur_px = max(1, round(hauteur_m / resolution_m))
    return largeur_px, hauteur_px


def rechercher_scenes(bbox, annee, nuages_max=30, limite=5, mois_cible=None, path_row_cible=None):
    """Recherche des scenes Landsat couvrant la zone (Living Atlas en priorite,
    Planetary Computer en secours si le service Living Atlas echoue).

    :param bbox: [xmin, ymin, xmax, ymax] en EPSG:4326
    :param annee: annee cible (int)
    :param nuages_max: pourcentage de nuages maximum tolere (0-100)
    :param limite: nombre max de scenes retournees
    :param mois_cible: mois (1-12) a privilegier, pour rapprocher la saison de
        cette recherche de celle d une autre date deja choisie dans une
        comparaison multi-temporelle (evite de comparer p.ex. une scene de
        saison des pluies a une scene de saison seche, ce qui confond le
        signal saisonnier avec un veritable changement inter-annuel -
        particulierement critique pour le NDVI et, dans une moindre mesure,
        le NDWI).
    :param path_row_cible: identifiant path/row Landsat (ex "206050") a
        privilegier, pour garder la MEME empreinte satellite qu une autre
        date deja choisie. PLUS IMPORTANT que le mois : deux scenes de path
        different peuvent ne couvrir la zone d etude que partiellement et de
        facon differente (recouvrement en bord de couloir), ce qui produit
        des statistiques radiometriques incomparables et peut a lui seul
        fausser gravement une comparaison multi-temporelle (surfaces
        aberrantes). Priorite : 1) meme path/row, 2) mois le plus proche,
        3) nuages les plus faibles.
    Si aucun candidat ne satisfait mois_cible/path_row_cible, on retombe sur
    le meilleur candidat disponible (l archive Landsat est parfois tres
    clairsemee sur les annees anciennes, ex: une seule scene disponible en
    1984 pour une zone donnee) : mieux vaut une comparaison imparfaite qu une
    analyse qui echoue faute de scene.
    :return: liste de dicts {id, datetime, cloud_cover, source, mois,
        path_row, ...}, triee par pertinence.
    """
    tri_actif = mois_cible is not None or path_row_cible is not None
    limite_recherche = limite if not tri_actif else max(limite * 6, 20)
    # Planetary Computer (Landsat Collection 2 LEVEL-2 : reflectance de
    # surface + temperature de surface) EN PRIORITE : c est le seul produit
    # compatible avec les facteurs d echelle utilises par core/ndvi, ndbi,
    # lst... Living Atlas (Level-1, reflectance TOA x10000) n est plus qu un
    # secours, converti a la volee (voir _convertir_living_atlas_vers_l2).
    try:
        scenes = _rechercher_scenes_planetary_computer(bbox, annee, nuages_max, limite_recherche)
    except Exception:
        scenes = []
    if not scenes:
        scenes = _rechercher_scenes_living_atlas(bbox, annee, nuages_max, limite_recherche)

    for s in scenes:
        s["mois"] = _mois_depuis_epoch_ms(s.get("epoch_ms") or s.get("datetime"))
        s["path_row"] = _path_row_depuis_id(s.get("id"))

    if tri_actif:
        def _cle_tri(s):
            ecart_path_row = 0 if (path_row_cible is None or s.get("path_row") == path_row_cible) else 1
            ecart_mois = _distance_mois(s.get("mois"), mois_cible) if mois_cible is not None else 0
            nuages = s.get("cloud_cover") if s.get("cloud_cover") is not None else 999
            return (ecart_path_row, ecart_mois, nuages)
        scenes.sort(key=_cle_tri)

    return scenes[:limite]


def _path_row_depuis_id(scene_id):
    """Extrait l identifiant path/row (ex "206050") depuis l identifiant de
    scene, quel que soit le format (Collection 2 moderne type
    LC09_L1TP_206050_... ou ancien type p206r050_...). Retourne None si non
    reconnu."""
    import re

    if not scene_id:
        return None
    m = re.search(r"_(\d{6})_", scene_id)
    if m:
        return m.group(1)
    m = re.search(r"[pP](\d{3})[rR](\d{3})", scene_id)
    if m:
        return m.group(1) + m.group(2)
    return None


def _mois_depuis_epoch_ms(valeur):
    """Extrait le mois (1-12) d une date fournie en epoch ms (Living Atlas) ou
    en chaine ISO (Planetary Computer). Retourne None si non interpretable."""
    import datetime as _dt

    if valeur is None:
        return None
    try:
        if isinstance(valeur, (int, float)):
            return _dt.datetime.utcfromtimestamp(valeur / 1000.0).month
        return _dt.datetime.fromisoformat(str(valeur).replace("Z", "+00:00")).month
    except Exception:
        return None


def _distance_mois(mois, mois_cible):
    """Distance circulaire entre deux mois (1-12), ex: distance(12, 1) = 1."""
    if mois is None:
        return 99  # scene sans date exploitable : reléguée en dernier
    d = abs(mois - mois_cible)
    return min(d, 12 - d)


def _rechercher_scenes_living_atlas(bbox, annee, nuages_max, limite):
    where = (
        "CloudCover < " + str(nuages_max / 100.0) +
        " AND AcquisitionDate >= timestamp '" + str(annee) + "-01-01 00:00:00'" +
        " AND AcquisitionDate <= timestamp '" + str(annee) + "-12-31 23:59:59'"
    )
    params = {
        "f": "json",
        "geometry": ",".join(str(c) for c in bbox),
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "where": where,
        "outFields": "AcquisitionDate,CloudCover,Name",
        "orderByFields": "CloudCover ASC",
        "resultRecordCount": limite,
        "returnGeometry": "false",
    }
    resp = requests.get(LIVING_ATLAS_URL + "/query", params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(str(data["error"]))

    scenes = []
    for feat in data.get("features", []):
        attrs = feat["attributes"]
        scenes.append({
            "id": attrs.get("Name"),
            "datetime": attrs.get("AcquisitionDate"),
            "cloud_cover": (attrs.get("CloudCover") or 0) * 100,
            "source": "living_atlas",
            "epoch_ms": attrs.get("AcquisitionDate"),
        })
    return scenes


def _rechercher_scenes_planetary_computer(bbox, annee, nuages_max, limite):
    payload = {
        "collections": ["landsat-c2-l2"],
        "bbox": bbox,
        "datetime": str(annee) + "-01-01T00:00:00Z/" + str(annee) + "-12-31T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": nuages_max}},
        "limit": limite,
        "sortby": [{"field": "eo:cloud_cover", "direction": "asc"}],
    }
    resp = requests.post(STAC_SEARCH_URL, json=payload, timeout=15)
    resp.raise_for_status()
    features = resp.json().get("features", [])

    scenes = []
    for feat in features:
        scenes.append({
            "id": feat["id"],
            "datetime": feat["properties"].get("datetime"),
            "cloud_cover": feat["properties"].get("eo:cloud_cover"),
            "source": "planetary_computer",
            "assets": feat["assets"],
        })
    return scenes


def telecharger_extrait(scene, bbox, bandes, chemin_sortie):
    """Telecharge un extrait multi-bandes limite a la zone d etude (bbox).

    :param scene: dict retourne par rechercher_scenes()
    :param bbox: [xmin, ymin, xmax, ymax] en EPSG:4326
    :param bandes: liste de cles de BAND_INDEX, ex: ["nir", "swir1"]
    :param chemin_sortie: chemin du GeoTIFF multi-bandes a ecrire
        (bandes dans l ordre demande)
    """
    if scene.get("source") == "living_atlas":
        return _telecharger_extrait_living_atlas(scene, bbox, bandes, chemin_sortie)
    return _telecharger_extrait_planetary_computer(scene, bbox, bandes, chemin_sortie)


def _telecharger_extrait_living_atlas(scene, bbox, bandes, chemin_sortie):
    # On demande les bandes utiles + la bande QA (masque nuages/ombres) en un
    # seul appel, pour ne pas doubler le nombre de requetes. La bande QA est
    # ensuite utilisee pour masquer les pixels contamines puis retiree du
    # fichier final (chemin_sortie ne contient que les bandes demandees,
    # comme avant - le masquage est transparent pour les appelants).
    #
    # Repli de resolution : le service Living Atlas (ArcGIS ImageServer)
    # refuse les exports trop grands en nombre de pixels ("The requested
    # image exceeds the size limit"), ce qui arrive pour les grandes zones
    # (region entiere, ex. Tambacounda) exportees a la resolution native
    # (30 m). On degrade alors progressivement la resolution (60 m, 120 m)
    # jusqu a ce que l export passe, plutot que d abandonner la scene -
    # perte de detail acceptable au niveau region, bien preferable a un
    # echec complet de l analyse.
    band_ids = ",".join(str(BAND_INDEX[b]) for b in bandes)
    band_ids_avec_qa = band_ids + "," + str(BAND_INDEX["qa"])

    derniere_erreur = None
    for resolution_m in (RESOLUTION_M, RESOLUTION_M * 2, RESOLUTION_M * 4):
        largeur_px, hauteur_px = _dimensions_export(bbox, resolution_m=resolution_m)
        params = {
            "bbox": ",".join(str(c) for c in bbox),
            "bboxSR": 4326,
            "imageSR": 32628,  # UTM 28N (metres) - le Senegal est couvert par cette zone
            "size": str(largeur_px) + "," + str(hauteur_px),
            "format": "tiff",
            "pixelType": "S16",
            "noDataInterpretation": "esriNoDataMatchAny",
            "interpolation": "RSP_NearestNeighbor",
            "bandIds": band_ids_avec_qa,
            "time": scene["epoch_ms"],
            "f": "image",
        }
        resp = requests.get(LIVING_ATLAS_URL + "/exportImage", params=params, timeout=30)
        resp.raise_for_status()

        # Le service renvoie parfois une erreur JSON avec un en-tete
        # Content-Type "image/tiff" trompeur (verifie empiriquement) : on
        # detecte donc l erreur sur le CONTENU (commence par '{'), pas sur
        # l en-tete declare.
        contenu = resp.content
        if contenu[:1] == b"{":
            try:
                erreur_json = json.loads(contenu.decode("utf-8", errors="replace"))
                message = erreur_json.get("error", {}).get("message", "")
                details = ", ".join(erreur_json.get("error", {}).get("details", []))
            except Exception:
                message, details = contenu[:200].decode("utf-8", errors="replace"), ""
            derniere_erreur = RuntimeError(
                "Reponse d erreur du service Living Atlas (resolution " + str(resolution_m) +
                " m) : " + message + (" - " + details if details else "")
            )
            if "size limit" in (message + details).lower():
                continue  # tente la resolution degradee suivante
            raise derniere_erreur  # autre type d erreur : pas la peine de reessayer a resolution degradee
        derniere_erreur = None
        break

    if derniere_erreur is not None:
        raise derniere_erreur

    from osgeo import gdal
    import uuid

    vsi_path = "/vsimem/living_atlas_" + uuid.uuid4().hex + ".tif"
    gdal.FileFromMemBuffer(vsi_path, contenu)
    try:
        _masquer_nuages_et_ecrire(vsi_path, len(bandes), chemin_sortie, bandes_living_atlas=list(bandes))
    finally:
        gdal.Unlink(vsi_path)
    return chemin_sortie


# Bits Landsat Collection 2 QA_PIXEL a exclure (union) :
# bit0=fill, bit1=dilated cloud, bit2=cirrus, bit3=cloud, bit4=cloud shadow.
_MASQUE_NUAGE_QA_PIXEL = 0b0000000000011111


# Living Atlas "Landsat/MS" sert des scenes Level-1 en reflectance TOA x 10000
# (verifie le 26/10/2026 sur Ngor : rouge ~ 900 pour une reflectance ~ 0.09),
# alors que tout le pipeline (core/ndvi.py, ndbi.py, lst.py...) attend des DN
# Collection 2 LEVEL-2 (reflectance = DN * 0.0000275 - 0.2). Sans conversion,
# les reflectances calculees etaient NEGATIVES (NDVI ~ 0 partout) et la LST
# tombait a ~ -26 C. On convertit donc les bandes reflectives vers l echelle
# L2 (approximation TOA ~ surface, acceptable en secours) et on invalide la
# bande thermique (pas de produit Surface Temperature cote Living Atlas).
_BANDES_THERMIQUES = ("thermal1", "thermal2")


def _convertir_living_atlas_vers_l2(arr, nom_bande):
    import numpy as np
    if nom_bande in _BANDES_THERMIQUES:
        return np.zeros_like(arr, dtype=np.int32)
    valide = arr > 0
    reflectance = arr.astype(np.float64) / 10000.0
    dn = np.round((reflectance + 0.2) / 0.0000275)
    return np.where(valide, np.clip(dn, 1, 65535), 0).astype(np.int32)


def _masquer_nuages_et_ecrire(chemin_source, nb_bandes_utiles, chemin_sortie, bandes_living_atlas=None):
    """Lit un raster source dont la DERNIERE bande est la bande QA (encodage
    standard QA_PIXEL Landsat Collection 2, verifie empiriquement), met a
    nodata (0, coherent avec la convention deja utilisee par
    calculer_ndbi/ndvi/ndwi) les pixels fill/nuage/nuage-dilate/cirrus/
    ombre-de-nuage dans les nb_bandes_utiles premieres bandes, puis ecrit le
    resultat (SANS la bande QA) dans chemin_sortie.

    Sans ce masquage, des pixels nuageux residuels (frequents des qu une
    scene n a pas 0% de couverture nuageuse) contaminent le calcul des
    indices et introduisent du bruit dans les 3 analyses (urbanisation,
    environnement, erosion), notamment quand une seule des deux dates
    comparees est affectee.
    """
    from osgeo import gdal
    import numpy as np

    ds = gdal.Open(chemin_source)
    if ds is None:
        raise RuntimeError("Impossible de lire l export pour masquage nuages")

    qa = ds.GetRasterBand(ds.RasterCount).ReadAsArray()

    # Verification de coherence AVANT d appliquer le masque bit a bit :
    # l encodage QA_PIXEL standard (Landsat Collection 2, capteurs OLI/ETM+
    # recents) code de nombreuses combinaisons de bits, avec des valeurs
    # typiques largement superieures a 63 (ex: 21824=clair/terre,
    # 21952=clair/eau...). Certaines scenes anciennes (Landsat TM) exposees
    # par ce service mosaique renvoient a la place une bande "qa" simplifiee
    # en 0/1 dont la convention n est PAS garantie identique (verifie
    # empiriquement : 1 = pixel valide, 0 = hors-scene - l inverse de
    # QA_PIXEL standard ou bit0=1 signifie fill/invalide). Appliquer le
    # masque bit a bit dans ce cas efface la totalite de l image valide au
    # lieu des nuages. Par securite, on n applique le masquage QUE si la
    # bande QA presente une richesse de valeurs coherente avec un encodage
    # QA_PIXEL multi-bit ; sinon on l ignore pour cette scene (mieux vaut ne
    # pas masquer que detruire l image).
    encodage_qa_pixel_plausible = int(qa.max()) > 63
    if encodage_qa_pixel_plausible:
        masque_nuage = (qa.astype(np.uint16) & _MASQUE_NUAGE_QA_PIXEL) != 0
    else:
        # Encodage simplifie 0/1 (scenes Landsat TM anciennes, cas frequent
        # pour les annees ~1984-1990 exposees par ce service mosaique) :
        # convention INVERSEE par rapport a QA_PIXEL standard, verifiee
        # empiriquement (1 = pixel valide, 0 = hors-scene/invalide). Avant ce
        # correctif, ce cas ne masquait RIEN (np.zeros) : les pixels
        # hors-scene/invalides (0) restaient inclus dans le composite et
        # faussaient les indices (NDVI notamment) pour ces vieilles scenes -
        # observe concretement sur la vegetation Dakar 1984, non representative
        # de la realite. On masque donc qa == 0. Cet encodage simplifie ne
        # code PAS l information nuage/ombre : les nuages residuels sur ces
        # scenes anciennes ne peuvent pas etre filtres par ce moyen (limite
        # de la source de donnees, pas un bug corrigible ici) ; le compositage
        # median multi-scenes attenue partiellement ce residu si plusieurs
        # scenes sont disponibles sur l annee.
        masque_nuage = (qa == 0)

    driver = gdal.GetDriverByName("GTiff")
    # Int32 (et non Int16) : les DN Level-2 Planetary Computer sont des
    # UInt16 (temperature de surface ~ 40 000-50 000, reflectances claires
    # > 32 767). En Int16 ils etaient TRONQUES a 32 767 (LST constante et
    # fausse, sols clairs/sable sature).
    out_ds = driver.Create(chemin_sortie, ds.RasterXSize, ds.RasterYSize, nb_bandes_utiles, gdal.GDT_Int32)
    out_ds.SetGeoTransform(ds.GetGeoTransform())
    out_ds.SetProjection(ds.GetProjection())
    for i in range(1, nb_bandes_utiles + 1):
        arr = ds.GetRasterBand(i).ReadAsArray().astype(np.int32)
        arr[masque_nuage] = 0
        if bandes_living_atlas is not None:
            arr = _convertir_living_atlas_vers_l2(arr, bandes_living_atlas[i - 1])
        # SetNoDataValue AVANT WriteArray (voir core/environnement.py pour le
        # detail du bug GDAL evite : ecrire puis fixer le nodata peut faire
        # ecraser silencieusement des donnees deja ecrites par le driver GTiff
        # quand un bloc entier a la meme valeur).
        out_ds.GetRasterBand(i).SetNoDataValue(0)
        out_ds.GetRasterBand(i).WriteArray(arr)
    out_ds.FlushCache()
    out_ds = None
    ds = None


_JETONS_SAS = {}  # "compte/conteneur" -> (jeton, expiration epoch)


def invalider_jetons():
    _JETONS_SAS.clear()


def _expiration(data):
    import time
    from datetime import datetime, timezone
    try:
        exp = datetime.strptime(data["msft:expiry"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return exp.timestamp()
    except Exception:
        return time.time() + 30 * 60


def _jeton(url_jeton):
    import time
    for tentative in range(3):
        try:
            resp = requests.get(url_jeton, timeout=25)
            if resp.status_code in (429, 502, 503, 504):
                time.sleep(1.5 + 2 * tentative)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["token"], _expiration(data)
        except Exception:
            time.sleep(1 + tentative)
    return None, 0


def _sign_href(href, collection=None):
    """Signe une URL d asset Planetary Computer (lecture publique).

    Un jeton SAS par conteneur, mis en cache jusqu a son expiration REELLE
    (champ msft:expiry). Ordre d essai : jeton par collection (le plus
    fiable), jeton par compte/conteneur, puis signature unitaire de l URL.
    Evite les erreurs 429 (une signature par bande et par scene) et les 504
    observes sur l endpoint compte/conteneur (echec WorldCover du 26/10/2026)."""
    import time
    from urllib.parse import urlparse
    u = urlparse(href)
    compte = u.netloc.split(".")[0]
    conteneur = u.path.lstrip("/").split("/")[0]
    cle = compte + "/" + conteneur
    jeton, expiration = _JETONS_SAS.get(cle, (None, 0))
    if jeton is None or time.time() > expiration - 300:
        jeton = None
        # Jeton du compte/conteneur EXACT de l asset (le jeton "par
        # collection" peut viser un autre stockage : 403 sur le MNT
        # Copernicus le 26/10/2026).
        sources = ["https://planetarycomputer.microsoft.com/api/sas/v1/token/" + compte + "/" + conteneur]
        for src in sources:
            jeton, expiration = _jeton(src)
            if jeton and expiration - time.time() < 120:
                # Le serveur renvoie le meme jeton a tous jusqu a son
                # expiration : s il ne lui reste que quelques secondes, on
                # attend son renouvellement plutot que d echouer en 403 en
                # pleine lecture (echec du MNT le 26/10/2026).
                time.sleep(max(0.0, expiration - time.time()) + 3)
                jeton, expiration = _jeton(src)
            if jeton:
                _JETONS_SAS[cle] = (jeton, expiration)
                break
    if jeton:
        return href + ("&" if "?" in href else "?") + jeton
    try:
        resp = requests.get(SAS_SIGN_URL, params={"href": href}, timeout=20)
        resp.raise_for_status()
        return resp.json()["href"]
    except Exception:
        if not collection:
            raise
        jeton, _exp = _jeton("https://planetarycomputer.microsoft.com/api/sas/v1/token/" + collection)
        if not jeton:
            raise
        return href + ("&" if "?" in href else "?") + jeton

def _telecharger_bande_pc(scene, nom_bande, chemin_sortie):
    """Telecharge UNE bande complete (scene entiere) depuis Planetary Computer.

    Rend la main a la boucle d evenements Qt PENDANT le telechargement (pas
    seulement entre deux scenes) : une bande complete peut peser plusieurs
    dizaines de Mo, et sans ce relachement periodique, le thread principal
    reste bloque assez longtemps pour que Windows/QGIS affiche l application
    comme \"ne repond plus\" (observe empiriquement, notamment sur composites
    de plusieurs scenes/bandes).
    """
    from qgis.PyQt.QtWidgets import QApplication
    import time

    nom_asset = _PC_ASSET_NAMES.get(nom_bande, nom_bande)
    asset = scene["assets"].get(nom_asset)
    if asset is None:
        raise ValueError("Bande " + repr(nom_asset) + " absente de la scene " + scene["id"])
    url = _sign_href(asset["href"], "landsat-c2-l2")
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()
    dernier_relachement = time.monotonic()
    with open(chemin_sortie, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            if chunk:
                f.write(chunk)
                # relache la main a Qt au plus toutes les ~0.2s (pas a
                # chaque chunk : appeler processEvents() trop souvent
                # ralentirait inutilement le telechargement).
                maintenant = time.monotonic()
                if maintenant - dernier_relachement > 0.2:
                    QApplication.processEvents()
                    dernier_relachement = maintenant
    return chemin_sortie


def _nom_asset_pc(scene, nom_bande):
    nom_asset = _PC_ASSET_NAMES.get(nom_bande, nom_bande)
    if nom_asset not in scene["assets"] and nom_bande == "thermal1" and "lwir" in scene["assets"]:
        nom_asset = "lwir"  # Landsat 4/5 TM et 7 ETM+ : une seule bande thermique
    return nom_asset


def _asset_url_vsicurl(scene, nom_bande):
    """URL /vsicurl/ (signee) d une bande d une scene Planetary Computer,
    pour lecture directe par GDAL sans telechargement prealable du fichier
    complet (les assets Landsat C2L2 PC sont des GeoTIFF Cloud-Optimises :
    GDAL ne recupere via HTTP range-request QUE les tuiles couvrant la bbox
    demandee).
    """
    nom_asset = _nom_asset_pc(scene, nom_bande)
    asset = scene["assets"].get(nom_asset)
    if asset is None:
        raise ValueError("Bande " + repr(nom_asset) + " absente de la scene " + scene["id"])
    url = _sign_href(asset["href"], "landsat-c2-l2")
    return "/vsicurl/" + url


def _telecharger_extrait_planetary_computer(scene, bbox, bandes, chemin_sortie):
    """Secours Planetary Computer : lit CHAQUE bande demandee directement en
    flux (/vsicurl/, pas de telechargement de la scene complete au prealable
    - voir _asset_url_vsicurl), les fusionne en un seul raster multi-bandes
    (meme ordre que `bandes`), puis decoupe/reprojette a la bbox de la zone
    d etude en UTM 28N (EPSG:32628), pour rester coherent avec la source
    principale.

    AVANT ce correctif : chaque bande (scene Landsat COMPLETE, ~45 Mo) etait
    telechargee integralement puis recoupee localement, alors que la zone
    d etude (commune) ne represente souvent qu une infime fraction de
    l emprise satellite. Pour un composite de plusieurs scenes/bandes, cela
    represente plusieurs centaines de Mo et plusieurs minutes de
    telechargement, largement suffisant pour bloquer QGIS ("ne repond plus")
    et depasser le timeout de la connexion MCP. En lisant directement via
    /vsicurl/ (GeoTIFF Cloud-Optimise), GDAL ne recupere que les tuiles
    necessaires a la bbox demandee : le volume transfere et le temps
    d execution chutent dans les memes proportions que le rapport
    (surface commune / surface scene Landsat).

    :param bandes: peut contenir une ou plusieurs bandes (contrairement a la
        version precedente qui ne supportait qu une seule bande)
    """
    from osgeo import gdal
    import tempfile
    import shutil

    # Options GDAL recommandees pour la lecture distante COG : evite les
    # requetes de listage de repertoire (non pertinentes/coûteuses sur un
    # stockage objet), autorise seulement les extensions attendues, et
    # augmente le timeout HTTP (reseau parfois lent en conditions reelles).
    gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF")
    gdal.SetConfigOption("GDAL_HTTP_TIMEOUT", "60")

    tmpdir = tempfile.mkdtemp(prefix="pc_bands_")
    try:
        # bandes utiles + bande QA (masque nuages/ombres), meme logique que
        # pour la source principale Living Atlas
        toutes_bandes = list(bandes) + ["qa"]
        vrt_path = os.path.join(tmpdir, "stack.vrt")

        def _vrt():
            urls_bandes = [_asset_url_vsicurl(scene, nom_bande) for nom_bande in toutes_bandes]
            return gdal.BuildVRT(vrt_path, urls_bandes, separate=True)
        vds = avec_reessai(_vrt)
        vds = None

        warp_path = os.path.join(tmpdir, "warp.tif")
        gdal.Warp(
            warp_path, vrt_path,
            outputBounds=bbox, outputBoundsSRS="EPSG:4326",
            dstSRS="EPSG:32628",
            xRes=RESOLUTION_M, yRes=RESOLUTION_M,  # resolution native Landsat, coherente avec Living Atlas
            resampleAlg="near",
        )
        _masquer_nuages_et_ecrire(warp_path, len(bandes), chemin_sortie)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return chemin_sortie


def rechercher_scenes_annee(bbox, annee, nuages_max=80, path_row_cible=None, max_scenes=10):
    """Recherche TOUTES les scenes disponibles sur une annee complete (tous
    mois, contrairement a rechercher_scenes qui ne retourne que les
    meilleurs candidats pour un usage scene-unique), pour construire un
    composite annuel sans nuages.

    Utilise Living Atlas EN PRIORITE (rapide, sans authentification, comme
    pour rechercher_scenes) et ne bascule sur Planetary Computer que si
    Living Atlas ne renvoie aucune scene ou echoue (service indisponible,
    scenes anciennes non couvertes, etc.) - avant ce correctif, cette
    fonction interrogeait UNIQUEMENT Planetary Computer, ce qui rendait
    TOUTES les analyses (urbanisation/erosion/environnement) totalement
    dependantes de la disponibilite de ce seul service externe.

    :param path_row_cible: si fourni, ne garde que les scenes de ce
        path/row (empreinte satellite identique entre les annees comparees).
    :param max_scenes: nombre max de scenes conservees (les moins nuageuses
        en priorite), pour limiter le temps de telechargement/traitement.
    :return: liste de dicts {id, datetime, cloud_cover, source, path_row, mois, ...}
    """
    limite_recherche = max_scenes * 3

    # Planetary Computer Level-2 EN PRIORITE (voir rechercher_scenes),
    # Living Atlas en secours seulement.
    scenes = []
    if True:
        payload = {
            "collections": ["landsat-c2-l2"],
            "bbox": bbox,
            "datetime": str(annee) + "-01-01T00:00:00Z/" + str(annee) + "-12-31T23:59:59Z",
            "query": {"eo:cloud_cover": {"lt": nuages_max}},
            "limit": 100,
            "sortby": [{"field": "eo:cloud_cover", "direction": "asc"}],
        }
        try:
            resp = requests.post(STAC_SEARCH_URL, json=payload, timeout=30)
            resp.raise_for_status()
            features = resp.json().get("features", [])
        except Exception:
            features = []

        scenes = []
        for feat in features:
            scenes.append({
                "id": feat["id"],
                "datetime": feat["properties"].get("datetime"),
                "cloud_cover": feat["properties"].get("eo:cloud_cover"),
                "source": "planetary_computer",
                "assets": feat["assets"],
            })

    if not scenes:
        try:
            scenes = _rechercher_scenes_living_atlas(bbox, annee, nuages_max, limite_recherche)
        except Exception:
            scenes = []

    for s in scenes:
        s["mois"] = _mois_depuis_epoch_ms(s.get("epoch_ms") or s.get("datetime"))
        s["path_row"] = _path_row_depuis_id(s.get("id"))

    scenes.sort(key=lambda s: s.get("cloud_cover") if s.get("cloud_cover") is not None else 999)

    if path_row_cible is not None:
        avec_le_bon_path_row = [s for s in scenes if s.get("path_row") == path_row_cible]
        if avec_le_bon_path_row:
            scenes = avec_le_bon_path_row
        # sinon (aucune scene sur ce path/row cette annee-la) : on garde
        # toutes les scenes disponibles plutot que d echouer completement.

    return scenes[:max_scenes]


def telecharger_composite_annuel(bbox, annee, bandes, chemin_sortie, path_row_cible=None,
                                  nuages_max=80, max_scenes=10, pulse=None):
    """Construit un composite annuel sans nuages (mediane pixel-a-pixel des
    scenes valides de l annee, apres masquage nuages/ombres par scene), au
    lieu de se fier a une seule scene "la moins nuageuse". Nettement plus
    robuste : lisse le bruit d une scene isolee, comble les trous de nuages
    d une scene par les pixels valides d une autre, et donne une image
    representative de l annee plutot que d une seule date arbitraire.

    :param bbox: [xmin, ymin, xmax, ymax] en EPSG:4326
    :param annee: annee cible
    :param bandes: liste de cles de BAND_INDEX (ex ["nir","swir1"])
    :param chemin_sortie: chemin du GeoTIFF composite a produire
    :param path_row_cible: path/row a privilegier (voir rechercher_scenes_annee)
    :param nuages_max: pourcentage de nuages maximum tolere par scene
    :param max_scenes: nombre max de scenes utilisees pour le composite
    :return: dict {chemin, nb_scenes_utilisees, path_row, mois_couverts}
    """
    import tempfile
    import shutil
    from osgeo import gdal
    import numpy as np

    scenes = rechercher_scenes_annee(bbox, annee, nuages_max=nuages_max,
                                      path_row_cible=path_row_cible, max_scenes=max_scenes)
    if not scenes:
        raise RuntimeError("Aucune scene Planetary Computer disponible pour " + str(annee))

    path_row_effectif = path_row_cible or scenes[0].get("path_row")

    from qgis.PyQt.QtWidgets import QApplication

    tmpdir = tempfile.mkdtemp(prefix="composite_")
    piles_bandes = [[] for _ in bandes]  # une liste d arrays par bande, une par scene valide
    mois_couverts = []
    path_rows_reussis = []
    nb_reussies = 0
    scenes_echouees = []  # [(scene_id, path_row, raison)] - visible dans le retour, plus jamais avale en silence
    # Telechargement des scenes EN PARALLELE (4 a la fois) : auparavant une
    # par une, ce qui representait l essentiel de la duree d une analyse.
    from concurrent.futures import ThreadPoolExecutor
    import time as _time

    def _charger(scene):
        extrait_scene = os.path.join(tmpdir, "scene_" + scene["id"][:40].replace("/", "_") + ".tif")
        telecharger_extrait(scene, bbox, list(bandes), extrait_scene)
        return extrait_scene

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futurs = {pool.submit(_charger, sc): sc for sc in scenes}
            restants = set(futurs)
            while restants:
                faits = [f for f in restants if f.done()]
                if not faits:
                    if pulse:
                        pulse()
                    else:
                        QApplication.processEvents()
                    _time.sleep(0.05)
                    continue
                for fut in faits:
                    restants.discard(fut)
                    scene = futurs[fut]
                    try:
                        extrait_scene = fut.result()
                    except Exception as exc:
                        scenes_echouees.append((scene["id"], scene.get("path_row"), repr(exc)))
                        continue
                    ds = gdal.Open(extrait_scene)
                    if ds is None:
                        scenes_echouees.append((scene["id"], scene.get("path_row"), "raster illisible apres telechargement"))
                        continue
                    if nb_reussies > 0 and (ds.RasterXSize, ds.RasterYSize) != (largeur, hauteur):
                        scenes_echouees.append((scene["id"], scene.get("path_row"), "grille differente"))
                        ds = None
                        continue
                    for i in range(len(bandes)):
                        arr = ds.GetRasterBand(i + 1).ReadAsArray().astype(np.float32)
                        arr[arr == 0] = np.nan
                        piles_bandes[i].append(arr)
                    if nb_reussies == 0:
                        geotransform = ds.GetGeoTransform()
                        projection = ds.GetProjection()
                        largeur, hauteur = ds.RasterXSize, ds.RasterYSize
                    nb_reussies += 1
                    path_rows_reussis.append(scene.get("path_row"))
                    mois_couverts.append(scene.get("mois"))
                    ds = None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if scenes_echouees:
        print("[sn_analyser] composite " + str(annee) + " : " + str(len(scenes_echouees)) +
              " scene(s) ignoree(s) sur " + str(len(scenes)) + " :")
        for sid, pr, raison in scenes_echouees:
            print("   - " + sid + " (path_row " + str(pr) + ") : " + raison)

    if nb_reussies == 0:
        raise RuntimeError("Aucune scene n a pu etre telechargee pour construire le composite " + str(annee))

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(chemin_sortie, largeur, hauteur, len(bandes), gdal.GDT_Float32)
    out_ds.SetGeoTransform(geotransform)
    out_ds.SetProjection(projection)
    couverture_pct = None
    for i in range(len(bandes)):
        pile = np.stack(piles_bandes[i], axis=0)
        with np.errstate(invalid="ignore"):
            composite = np.nanmedian(pile, axis=0)
        if couverture_pct is None:
            # % de pixels ayant obtenu une valeur valide dans le composite final
            # (au moins une scene valide a cette position) - sert a detecter un
            # composite incomplet (ex: la moitie d une zone couverte par un
            # path/row satellite dont toutes les scenes ont echoue au telechargement).
            valides = np.isfinite(composite).sum()
            couverture_pct = 100.0 * valides / composite.size
        composite = np.nan_to_num(composite, nan=0.0)
        out_ds.GetRasterBand(i + 1).SetNoDataValue(0.0)
        out_ds.GetRasterBand(i + 1).WriteArray(composite)
    out_ds.FlushCache()
    out_ds = None

    path_rows_couverts = sorted(set(pr for pr in path_rows_reussis if pr is not None))

    print("[sn_analyser] composite " + str(annee) + " : " + str(nb_reussies) + "/" + str(len(scenes)) +
          " scene(s) utilisees, path/row " + str(path_rows_couverts) +
          ", couverture " + f"{couverture_pct:.1f}%")

    return {
        "chemin": chemin_sortie,
        "nb_scenes_utilisees": nb_reussies,
        "nb_scenes_demandees": len(scenes),
        "scenes_echouees": scenes_echouees,
        "path_row": path_row_effectif,
        "path_rows_couverts": path_rows_couverts,
        "couverture_pct": couverture_pct,
        "mois_couverts": sorted(set(m for m in mois_couverts if m is not None)),
    }


def choisir_capteur(annee):
    """Selectionne le capteur Landsat adapte a l annee (4/5 TM, 7 ETM+, 8/9 OLI)."""
    if annee < 1999:
        return "LANDSAT_TM"
    elif annee < 2013:
        return "LANDSAT_ETM"
    else:
        return "LANDSAT_OLI"


def avec_reessai(fonction, *args, essais=3, **kwargs):
    """Execute une lecture distante (GDAL /vsicurl/) ; en cas d echec (jeton
    expire, 403, coupure), invalide les jetons SAS et recommence."""
    import time
    derniere = None
    for tentative in range(essais):
        try:
            res = fonction(*args, **kwargs)
            if res is None:
                raise RuntimeError("lecture distante sans resultat")
            return res
        except Exception as exc:
            derniere = exc
            invalider_jetons()
            time.sleep(2 + 2 * tentative)
    raise derniere
