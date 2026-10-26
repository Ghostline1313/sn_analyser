# -*- coding: utf-8 -*-
"""
core/ire.py
-----------
Indice de Risque Environnemental (IRE) : score composite instantane (une
seule annee, contrairement aux indices Urbanisation/Environnement/Erosion
qui comparent deux dates) evaluant le niveau de risque environnemental
d une commune/departement/region.

Methodologie (approche multicritere standard en SIG/etudes environnementales) :
    IRE = somme ( poids_i * indicateur_normalise_i )

PHASE 3 (implementee) : 4 sous-indices sur les 5 prevus a terme :
    - IDT (Degradation des Terres) : NDVI + NBR + BSI,                poids cible 25
    - IU  (Urbanisation)           : NDBI,                             poids cible 20
    - IC  (Climatique, PARTIEL)    : LST uniquement (SPI/CHIRPS pas
                                      encore integre - chantier a part), poids cible 25
    - IV  (Vulnerabilite)          : densite population (WorldPop) +
                                      distance infrastructure sensible
                                      la plus proche (Overpass/OSM),    poids cible 20
Les poids sont renormalises a 100% sur les seuls sous-indices disponibles
(voir POIDS_CIBLES / _renormaliser_poids), chaque sous-indice etant lui-meme
degrade proprement si une de ses composantes internes manque (ex : IV sans
distance Overpass si la requete echoue).

PHASE 4 (a venir) : IP (pollution : NO2 Sentinel-5P).

IMPORTANT : tant que IP n est pas implemente (et IC pas complet - pas de
precipitation/SPI), le score IRE ne reflete PAS un risque environnemental
complet. Les champs 'ire_phase', 'ire_composantes' et
'ire_composantes_manquantes' du resultat le rappellent explicitement.
"""

# Poids cibles a terme (une fois les 5 sous-indices pleinement implementes).
POIDS_CIBLES = {
    "IDT": 25,
    "IU": 20,
    "IC": 25,
    "IV": 20,
    "IP": 10,
}

# Classes de risque (bornes inclusives basses), standard 5 classes 0-100.
CLASSES_RISQUE = [
    (0, 20, "Tres faible"),
    (20, 40, "Faible"),
    (40, 60, "Moyen"),
    (60, 80, "Eleve"),
    (80, 101, "Tres eleve"),
]


def normaliser(valeur, vmin, vmax):
    """Ramene une valeur sur une echelle 0-1 (min-max), bornee (clip).

    Accepte aussi bien un scalaire (float) qu un tableau numpy (meme
    formule appliquee pixel par pixel) -- permet de reutiliser TOUTES les
    fonctions calculer_xxx ci-dessous, sans duplication, pour generer un
    raster de risque par pixel (voir plugin._generer_raster_risque) en plus
    du score zonal moyen habituel.

    :param valeur: valeur brute de l indicateur (float ou numpy.ndarray)
    :param vmin: valeur consideree comme le minimum (risque nul, 0)
    :param vmax: valeur consideree comme le maximum (risque maximal, 1)
    """
    import numpy as np
    if vmax == vmin:
        return np.zeros_like(valeur) if isinstance(valeur, np.ndarray) else 0.0
    n = (valeur - vmin) / (vmax - vmin)
    return np.clip(n, 0.0, 1.0)


def classifier_niveau(score_100):
    """Retourne le libelle de classe de risque (str) pour un score 0-100."""
    for borne_min, borne_max, libelle in CLASSES_RISQUE:
        if borne_min <= score_100 < borne_max:
            return libelle
    return "Tres eleve"


def _renormaliser_poids(sous_indices_disponibles):
    """Renormalise les poids cibles a 100% sur les seuls sous-indices fournis."""
    brut = sum(POIDS_CIBLES[k] for k in sous_indices_disponibles)
    return {k: POIDS_CIBLES[k] / brut for k in sous_indices_disponibles}


def calculer_idt(mean_ndvi=None, mean_nbr=None, mean_bsi=None, mean_evi=None, mean_savi=None, mean_si=None,
                  superficie_reboisee_ha=None):
    """Indice de Degradation des Terres (0-100), moyenne des composantes de
    risque DISPONIBLES ET DEMANDEES (toutes optionnelles -- panneau "sous-
    classes" du risque Degradation des terres) :
      - perte de vegetation (mean_ndvi et/ou mean_evi et/ou mean_savi -- au
        moins un des trois active la composante ; EVI/SAVI affinent le
        signal NDVI seul respectivement en canopee dense (sud boise) et en
        couverture vegetale clairsemee/sol nu dominant (majorite semi-aride).
        Voir core/evi.py, core/savi.py.)
      - brulis/degradation (mean_nbr, NBR faible = risque eleve)
      - sol nu (mean_bsi, BSI eleve = risque eleve)
      - salinisation (mean_si, SI eleve = risque eleve) -- voir
        core/salinite.py. Pertinent surtout dans les zones estuariennes
        (Sine-Saloum, Casamance, delta du fleuve Senegal) ; ailleurs SI reste
        bas et n influence pas significativement le score.

    Les bornes de normalisation sont des ordres de grandeur usuels pour ces
    indices en contexte sahelien/Senegal (a ajuster si besoin selon retours
    terrain).

    :return: score 0-100, ou None si aucune composante n est disponible.
    """
    composantes_idt = []

    risques_veg = []
    if mean_ndvi is not None:
        risques_veg.append(1.0 - normaliser(mean_ndvi, -0.1, 0.6))
    if mean_evi is not None:
        risques_veg.append(1.0 - normaliser(mean_evi, -0.1, 0.6))
    if mean_savi is not None:
        risques_veg.append(1.0 - normaliser(mean_savi, -0.1, 0.5))
    if risques_veg:
        composantes_idt.append(sum(risques_veg) / len(risques_veg))

    if mean_nbr is not None:
        composantes_idt.append(1.0 - normaliser(mean_nbr, -0.2, 0.5))
    if mean_bsi is not None:
        composantes_idt.append(normaliser(mean_bsi, -0.3, 0.3))
    if mean_si is not None:
        composantes_idt.append(normaliser(mean_si, 0.02, 0.25))

    if not composantes_idt:
        return None

    score_spectral = sum(composantes_idt) / len(composantes_idt) * 100.0

    if superficie_reboisee_ha is not None:
        # Attenuation BORNEE (10 points max), PAS un 5e composant a poids
        # egal : un effort de reboisement (donnee ANSD/DEFCCS, regionale,
        # 2023 -- voir data/reboisement_regions.json) est un signal
        # d effort/intention, pas une mesure d etat de la vegetation comme
        # les autres composantes. Le laisser peser autant que le signal
        # spectral permettrait a un fort reboisement de masquer une
        # degradation reelle et actuelle ; on le traite donc comme un
        # attenuant marginal. Bornes 0 (aucun effort) a 2500 ha (effort
        # regional le plus eleve observe en 2023, Kolda) -- calibrage a
        # ajuster si de nouvelles annees deviennent disponibles.
        import numpy as np
        attenuation = normaliser(superficie_reboisee_ha, 0.0, 2500.0) * 10.0
        return np.maximum(0.0, score_spectral - attenuation)

    return score_spectral


def calculer_iu(mean_ndbi=None, mean_pop_density=None, distances_infra_km=None,
                 taux_croissance_pct_an=None, part_bati=None):
    """Indice d Urbanisation/pression urbaine (0-100), moyenne des composantes
    DISPONIBLES ET DEMANDEES (sous-classes du risque Urbanisation) :
      - artificialisation (mean_ndbi, NDBI eleve = risque eleve)
      - densite de population (mean_pop_density, WorldPop/ANSD -- plus la
        zone est dense, plus la pression urbaine/l exposition est grande)
      - acces aux infrastructures (distances_infra_km, dict {groupe:
        distance_km}, voir core/vulnerabilite.CATEGORIES_GROUPES -- une zone
        mal desservie malgre une urbanisation dense est un signal de
        pression/etalement mal maitrise)
      - evolution du bati (taux_croissance_pct_an, %/an entre annee_debut et
        annee_fin -- voir core/urbanisation.taux_croissance_annuel, sous-
        classe "Evolution du bati (multi-dates)") : une croissance rapide
        est un risque en soi (etalement non maitrise), independamment du
        niveau d artificialisation deja atteint. Bornes 0 %/an (stable) a
        10 %/an (croissance tres rapide, ordre de grandeur des communes
        peripheriques de Dakar en forte expansion) -- valeurs negatives
        (deprise urbaine) ramenees a 0 (pas de risque de ce type).

    :return: score 0-100, ou None si aucune composante n est disponible.
    """
    composantes = []
    if part_bati is not None:
        # Part de surfaces baties (ESA WorldCover 10 m, classe 50), PRIORITAIRE
        # sur le NDBI : le NDBI Landsat confond bati et sable/sol nu en zone
        # sahelienne (une commune rurale sableuse sortait "urbanisee", une
        # commune a 73 % batie comme Ngor ne depassait pas 51/100). Bornes 0
        # (aucun bati) a 0.6 (tissu urbain continu).
        composantes.append(normaliser(part_bati, 0.0, 0.6))
    elif mean_ndbi is not None:
        composantes.append(normaliser(mean_ndbi, -0.3, 0.3))
    if mean_pop_density is not None:
        composantes.append(normaliser(mean_pop_density, 0.0, 20000.0))
    distances_disponibles = [d for d in (distances_infra_km or {}).values() if d is not None]
    if distances_disponibles:
        risques_acces = [normaliser(d, 0.0, 15.0) for d in distances_disponibles]
        composantes.append(sum(risques_acces) / len(risques_acces))
    if taux_croissance_pct_an is not None:
        composantes.append(normaliser(max(0.0, taux_croissance_pct_an), 0.0, 10.0))

    if not composantes:
        return None
    return sum(composantes) / len(composantes) * 100.0


def calculer_ic(mean_lst=None, total_precip_mm=None, spi=None):
    """Indice Climatique (0-100), moyenne des composantes disponibles :
      - temperature de surface (LST, degres Celsius) : bornes 25 C (risque
        thermique nul) a 50 C (risque maximal), ordres de grandeur usuels en
        zone urbaine/sol nu au Senegal en saison seche.
      - secheresse : SPI (Standardized Precipitation Index, voir
        core/precipitation.calculer_spi) PRIORITAIRE si disponible -- z-score
        de la precipitation par rapport a la climatologie LOCALE (30 ans, ce
        point precis), plus rigoureux qu un seuil fixe. Bornes -2.5
        (secheresse severe) a +1.0 (humide, risque nul), asymetriques car on
        ne penalise pas un exces de pluie comme un deficit (l objectif est
        le risque de secheresse/degradation, pas les inondations -- deja
        couvertes par l erosion/NDWI). A defaut de SPI (climatologie
        indisponible), repli sur total_precip_mm avec des bornes fixes 200 mm
        (nord semi-desertique) a 1200 mm (sud soudano-guineen) -- moins
        precis (pas calibre sur le point exact) mais degrade proprement.

    Si un seul des deux facteurs (LST, secheresse) est fourni, IC se
    degrade proprement a cette seule composante.

    :param mean_lst: LST moyenne (Celsius), ou None
    :param total_precip_mm: cumul de precipitation annuel (mm), utilise
        seulement si spi est None
    :param spi: Standardized Precipitation Index (z-score), ou None
    """
    composantes = []
    if mean_lst is not None:
        composantes.append(normaliser(mean_lst, 25.0, 50.0))
    if spi is not None:
        composantes.append(1.0 - normaliser(spi, -2.5, 1.0))
    elif total_precip_mm is not None:
        composantes.append(1.0 - normaliser(total_precip_mm, 200.0, 1200.0))
    if not composantes:
        return None
    return sum(composantes) / len(composantes) * 100.0


def calculer_ip(european_aqi=None, no2_s5p_mol_m2=None, no2_openmeteo_ugm3=None,
                 distance_industrie_km=None):
    """Indice de Pollution (0-100). Mesure de fond (une des trois sources
    ci-dessous, par ordre de priorite -- voir core/pollution.py et
    core/no2.py), attenuee/aggravee par la proximite industrielle :

      1. european_aqi (Open-Meteo, echelle EAQI officielle deja 0-100+,
         COMPOSITE PM2.5+PM10+NO2+SO2+CO+O3) : PRIORITAIRE des qu il est
         disponible, car c est la seule mesure qui represente la pollution
         de l air dans son ensemble plutot qu un seul polluant.
      2. no2_s5p_mol_m2 (Sentinel-5P, colonne tropospherique NO2) : mesure
         satellite directe et precise, mais mono-polluant (NO2 seul) --
         utilisee en repli si l AQI composite est indisponible. Bornes
         0.00002 (fond rural) a 0.00015 mol/m2 (zone urbaine tres polluee),
         ordres de grandeur usuels TROPOMI.
      3. no2_openmeteo_ugm3 (NO2 de surface seul, modele CAMS) : dernier
         repli si ni l AQI ni Sentinel-5P ne sont disponibles. Bornes 0 (air
         pur) a 60 microg/m3 (pollution urbaine elevee).

    :param distance_industrie_km: distance (km) a la zone industrielle la
        plus proche (OpenStreetMap/Overpass, voir
        core/vulnerabilite.distance_industrie_proche), ou None si non
        recherchee/non trouvee. Si fournie, AJUSTE (ne remplace pas) la
        mesure de fond : proximite immediate (< 15 km, borne usuelle d
        influence d un site industriel sur la qualite de l air locale) ->
        majoration ponderee (poids 25%, la mesure de fond reste
        preponderante a 75% car c est une donnee directe alors que la
        distance n est qu un proxy indirect de risque).

    :return: (score_0_100, libelle_source) ou (None, None) si aucune donnee
        de fond disponible (la distance industrielle seule ne suffit pas a
        produire un score : c est un ajustement, pas une source autonome).
    """
    if european_aqi is not None:
        score_fond = min(100.0, float(european_aqi))
        source = "Open-Meteo (AQI europeen composite : PM2.5+PM10+NO2+SO2+CO+O3)"
    elif no2_s5p_mol_m2 is not None:
        score_fond = normaliser(no2_s5p_mol_m2, 0.00002, 0.00015) * 100.0
        source = "Sentinel-5P (colonne tropospherique NO2 uniquement)"
    elif no2_openmeteo_ugm3 is not None:
        score_fond = normaliser(no2_openmeteo_ugm3, 0.0, 60.0) * 100.0
        source = "Open-Meteo (NO2 de surface uniquement)"
    else:
        return None, None

    if distance_industrie_km is not None:
        risque_industrie = 1.0 - normaliser(distance_industrie_km, 0.0, 15.0)
        score = score_fond * 0.75 + (risque_industrie * 100.0) * 0.25
        source += " + proximite industrielle (OSM, %.1f km)" % distance_industrie_km
        return score, source

    return score_fond, source


def calculer_iv(mean_pop_density, distances_infra_km=None):
    """Indice de Vulnerabilite (0-100), moyenne des composantes disponibles :
      - densite de population (WorldPop/ANSD) : plus c est dense, plus
        l exposition (population impactee) est grande.
      - acces aux infrastructures sensibles (dict {groupe: distance_km},
        voir core/vulnerabilite.CATEGORIES_GROUPES : sante, education,
        securite_urgence, marche) : chaque groupe disponible contribue a
        parts egales (moyenne des risques par groupe, pas juste "le plus
        proche tout type confondu" -- une ecole a cote ne compense pas
        l absence d hopital a proximite).

    Borne haute de densite (20 000 hab/km2) calibree sur les communes les
    plus denses de Dakar (ordre de grandeur des pics observes, ex. Grand
    Dakar/Medina/Pikine). Avec une borne a 5 000, la region Dakar entiere
    (densite moyenne ~5 580 hab/km2, verifie empiriquement) saturait deja a
    100, ne permettant plus de distinguer une commune tres dense (Plateau)
    d une commune peripherique moins dense (Sebikotane) du meme region.

    :param distances_infra_km: dict {groupe: distance_km ou None}, ou None
        si Overpass n a pas ete interroge du tout.
    """
    risque_densite = normaliser(mean_pop_density, 0.0, 20000.0)

    distances_disponibles = [d for d in (distances_infra_km or {}).values() if d is not None]
    if not distances_disponibles:
        return risque_densite * 100.0

    risques_acces = [normaliser(d, 0.0, 15.0) for d in distances_disponibles]
    risque_acces_moyen = sum(risques_acces) / len(risques_acces)
    return (risque_densite + risque_acces_moyen) / 2.0 * 100.0


def calculer_ih(mean_ndwi=None, total_precip_mm=None, spi=None,
                 distance_reseau_hydro_km=None, distance_lac_km=None,
                 bilan_erosion_ha_an=None, risque_topo=None, aire_drainee_max_km2=None,
                 densite_drainage_km_km2=None, argile_moy_pct=None, impermeabilite_sol=None):
    """Indice de risque Hydrologique (0-100), moyenne des composantes
    DISPONIBLES ET DEMANDEES (sous-classes du risque Hydrologique) :
      - precipitations excessives : SPI eleve POSITIF = risque de crue,
        bornes 0 (normal) a +2.5 (tres humide). A defaut de SPI, repli sur
        total_precip_mm (bornes fixes 800mm a 2000mm, zones les plus arrosees
        du Senegal - Casamance).
      - presence/etendue de surfaces en eau (mean_ndwi eleve = zone humide/
        inondable/mangrove DETECTEE par satellite), bornes -0.3 (aucune eau)
        a 0.3 (eau/zone humide dominante).
      - proximite au reseau hydrographique (distance_reseau_hydro_km,
        OpenStreetMap waterway=river/stream, voir
        core/vulnerabilite.distance_reseau_hydrographique_proche) : plus une
        zone est proche d un cours d eau, plus le risque de crue/debordement
        est eleve. Bornes 0 km (bord immediat) a 5 km (hors zone d influence
        directe usuelle d un cours d eau).
      - proximite a un lac/plan d eau permanent CARTOGRAPHIE
        (distance_lac_km, OpenStreetMap natural=water, voir
        core/vulnerabilite.distance_lac_proche) : complementaire au NDWI
        (qui detecte l eau au moment precis de l image satellite) --
        capture les plans d eau permanents meme hors periode d inondation
        visible. Memes bornes que le reseau hydrographique (0-5 km).

    NOTE (limite connue) : ceci ne constitue PAS une modelisation de zone
    inondable (qui necessiterait un MNT/DEM et une analyse hydraulique) ni
    un bassin versant (deja couvert par le plugin dedie Sen Hydro /
    watershed_delineator, base sur le package `delineator`) -- ces deux
    elements ne sont pas repris ici pour eviter une fausse impression de
    precision hydraulique.

    Se degrade proprement aux seules composantes disponibles/demandees.
    Retourne None si aucune donnee n est disponible.
    """
    composantes = []
    if spi is not None:
        composantes.append(normaliser(spi, 0.0, 2.5))
    elif total_precip_mm is not None:
        composantes.append(normaliser(total_precip_mm, 800.0, 2000.0))
    if mean_ndwi is not None:
        composantes.append(normaliser(mean_ndwi, -0.3, 0.3))
    if distance_reseau_hydro_km is not None:
        composantes.append(1.0 - normaliser(distance_reseau_hydro_km, 0.0, 5.0))
    if distance_lac_km is not None:
        composantes.append(1.0 - normaliser(distance_lac_km, 0.0, 5.0))
    if bilan_erosion_ha_an is not None:
        # bilan_erosion_ha_an > 0 = recul net du trait de cote (ha/an),
        # voir core/erosion.py (erosion_ha - accretion_ha) / nb_annees.
        # Bornes 0 (stable/accretion) a 5 ha/an (recul severe, ordre de
        # grandeur observe sur les communes cotieres les plus exposees du
        # Senegal, ex : Rufisque, Saint-Louis).
        composantes.append(normaliser(max(0.0, bilan_erosion_ha_an), 0.0, 5.0))
    # --- Relief, ecoulements et sols (MNT Copernicus, GRASS, SoilGrids) ---
    if risque_topo is not None:
        # basse altitude + relief plat + humidite topographique (TCI), deja
        # ramene a 0-1 par core.relief_hydro.risque_topographique
        composantes.append(risque_topo)
    if aire_drainee_max_km2 is not None:
        # aire drainee en amont du point le plus bas de la zone : 1 km2 -> 0,
        # 1 000 km2 et plus -> 1 (echelle logarithmique)
        import math
        composantes.append(normaliser(math.log10(max(aire_drainee_max_km2, 1.0)), 0.0, 3.0))
    if densite_drainage_km_km2 is not None:
        # densite du reseau hydrographique derive du relief (km/km2)
        composantes.append(normaliser(densite_drainage_km_km2, 0.0, 2.0))
    if argile_moy_pct is not None:
        # sols argileux = faible infiltration, ruissellement/stagnation
        composantes.append(normaliser(argile_moy_pct, 10.0, 45.0))
    elif impermeabilite_sol is not None:
        composantes.append(impermeabilite_sol)
    if not composantes:
        return None
    return sum(composantes) / len(composantes) * 100.0


def calculer_ibio(mean_ndvi=None, mean_nbr=None, mean_evi=None, mean_savi=None, aire_protegee_proche=False,
                   variation_vegetation_pct=None, part_bati_terrestre=None):
    """Indice de risque sur la Biodiversite (0-100), moyenne des composantes
    DISPONIBLES ET DEMANDEES (sous-classes du risque Biodiversite) :
      - perte de vegetation/habitat (mean_ndvi et/ou mean_evi et/ou
        mean_savi, faible = risque eleve) -- etat INSTANTANE (une seule annee)
      - feux de brousse/degradation recente (mean_nbr, NBR faible = risque
        eleve)
      - evolution/perte de vegetation (variation_vegetation_pct, % de
        variation de la surface vegetalisee entre annee_debut et annee_fin
        -- voir core/environnement.py, sous-classe "Evolution ou perte
        (multi-dates)") : une perte marquee est un risque en soi, distinct
        du niveau de vegetation actuel (une zone deja peu vegetalisee mais
        stable est moins a risque qu une zone qui se degrade rapidement).
        Bornes 0 % (stable/en hausse) a -30 % (perte severe sur la periode,
        ordre de grandeur d une deforestation/artificialisation marquee) ;
        une hausse (reboisement/regeneration) ramenee a 0 (pas de risque de
        ce type).

    Legerement attenue si la zone est a proximite immediate d une aire
    protegee (gestion/surveillance presumee, voir core/aires_protegees.py)
    -- attenuation marginale (5 pts), pas un critere a poids egal.

    :return: score 0-100, ou None si aucune composante n est disponible.
    """
    composantes = []

    risques_veg = []
    if mean_ndvi is not None:
        risques_veg.append(1.0 - normaliser(mean_ndvi, -0.1, 0.6))
    if mean_evi is not None:
        risques_veg.append(1.0 - normaliser(mean_evi, -0.1, 0.6))
    if mean_savi is not None:
        risques_veg.append(1.0 - normaliser(mean_savi, -0.1, 0.5))
    if risques_veg:
        composantes.append(sum(risques_veg) / len(risques_veg))

    if mean_nbr is not None:
        composantes.append(1.0 - normaliser(mean_nbr, -0.2, 0.5))

    if variation_vegetation_pct is not None:
        perte = min(0.0, variation_vegetation_pct)  # une hausse (>0) ne compte pas comme risque
        composantes.append(normaliser(-perte, 0.0, 30.0))

    if part_bati_terrestre is not None:
        # Pression d artificialisation sur les habitats (part de bati dans
        # les terres emergees, ESA WorldCover) : bornes 0 a 0.6. Les indices
        # de vegetation ci-dessus sont desormais calcules sur les SEULS
        # pixels naturels/agricoles ; cette composante rend compte du fait
        # que ces habitats sont reduits et fragmentes par le bati.
        composantes.append(normaliser(part_bati_terrestre, 0.0, 0.6))

    if not composantes:
        return None

    score = sum(composantes) / len(composantes) * 100.0
    if aire_protegee_proche:
        import numpy as np
        score = np.maximum(0.0, score - 5.0)
    return score


# Configuration des 6 risques environnementaux INDEPENDANTS proposes dans le
# panneau "Analyse de Risque Environnemental". Remplace l ancien IRE combine
# (voir calculer_ire, conserve ci-dessous pour compatibilite/reference mais
# plus appele par plugin.py).
RISQUES_CONFIG = {
    "climatique": {"label": "Risque climatique", "emoji": "\U0001F30D"},
    "pollution": {"label": "Risque de pollution", "emoji": "\U0001F4A8"},
    "degradation_terres": {"label": "Risque de degradation des terres", "emoji": "\U0001F331"},
    "hydrologique": {"label": "Risque hydrologique", "emoji": "\U0001F30A"},
    "urbanisation": {"label": "Risque lie a l'urbanisation", "emoji": "\U0001F3D9\uFE0F"},
    "biodiversite": {"label": "Risque sur la biodiversite", "emoji": "\U0001F333"},
}


def calculer_risques_independants(risques_demandes, mean_ndvi=None, mean_nbr=None, mean_bsi=None,
                                   mean_ndbi=None, mean_ndwi=None, mean_lst=None,
                                   total_precip_mm=None, spi=None,
                                   european_aqi=None, no2_s5p_mol_m2=None, no2_openmeteo_ugm3=None,
                                   distance_industrie_km=None,
                                   mean_evi=None, mean_savi=None, mean_si=None,
                                   superficie_reboisee_ha=None, aire_protegee_proche=False,
                                   mean_pop_density=None, distances_infra_km=None):
    """Calcule uniquement les risques demandes (cles de RISQUES_CONFIG),
    CHACUN INDEPENDAMMENT -- pas de score combine type IRE.

    Chaque parametre correspond a une SOUS-CLASSE cochable dans le panneau
    "Analyse de Risque Environnemental" -- tous optionnels (None = sous-
    classe decochee ou donnee indisponible) : la fonction degrade proprement
    et ignore un risque entierement si aucune de ses sous-classes n a pu
    etre calculee.

    :param risques_demandes: liste de cles parmi RISQUES_CONFIG (ex:
        ["climatique", "pollution"])
    :return: dict {cle: {"score": float, "niveau": str, "label": str,
        "emoji": str, "source": str (optionnel, ex: pollution)}} -- une
        entree seulement pour les risques a la fois demandes ET calculables.
    """
    resultats = {}

    if "degradation_terres" in risques_demandes:
        score = calculer_idt(mean_ndvi, mean_nbr, mean_bsi, mean_evi=mean_evi, mean_savi=mean_savi,
                              mean_si=mean_si, superficie_reboisee_ha=superficie_reboisee_ha)
        if score is not None:
            resultats["degradation_terres"] = {
                "score": score, "niveau": classifier_niveau(score),
                **RISQUES_CONFIG["degradation_terres"],
            }

    if "urbanisation" in risques_demandes:
        score = calculer_iu(mean_ndbi, mean_pop_density=mean_pop_density, distances_infra_km=distances_infra_km)
        if score is not None:
            resultats["urbanisation"] = {
                "score": score, "niveau": classifier_niveau(score),
                **RISQUES_CONFIG["urbanisation"],
            }

    if "climatique" in risques_demandes:
        score = calculer_ic(mean_lst=mean_lst, total_precip_mm=total_precip_mm, spi=spi)
        if score is not None:
            resultats["climatique"] = {
                "score": score, "niveau": classifier_niveau(score),
                **RISQUES_CONFIG["climatique"],
            }

    if "pollution" in risques_demandes:
        score, source = calculer_ip(european_aqi=european_aqi, no2_s5p_mol_m2=no2_s5p_mol_m2,
                                     no2_openmeteo_ugm3=no2_openmeteo_ugm3,
                                     distance_industrie_km=distance_industrie_km)
        if score is not None:
            resultats["pollution"] = {
                "score": score, "niveau": classifier_niveau(score), "source": source,
                **RISQUES_CONFIG["pollution"],
            }

    if "hydrologique" in risques_demandes:
        score = calculer_ih(mean_ndwi=mean_ndwi, total_precip_mm=total_precip_mm, spi=spi)
        if score is not None:
            resultats["hydrologique"] = {
                "score": score, "niveau": classifier_niveau(score),
                **RISQUES_CONFIG["hydrologique"],
            }

    if "biodiversite" in risques_demandes:
        score = calculer_ibio(mean_ndvi, mean_nbr, mean_evi=mean_evi, mean_savi=mean_savi,
                               aire_protegee_proche=aire_protegee_proche)
        if score is not None:
            resultats["biodiversite"] = {
                "score": score, "niveau": classifier_niveau(score),
                **RISQUES_CONFIG["biodiversite"],
            }

    return resultats


def calculer_ire(mean_ndvi, mean_nbr, mean_bsi, mean_ndbi,
                  mean_lst=None, mean_pop_density=None, distances_infra_km=None,
                  total_precip_mm=None, spi=None, european_aqi=None,
                  no2_s5p_mol_m2=None, no2_openmeteo_ugm3=None,
                  mean_evi=None, mean_savi=None, mean_si=None,
                  superficie_reboisee_ha=None):
    """Calcule l IRE et sa classification.

    :param mean_lst: LST moyenne (Celsius), active/complete IC si fourni.
    :param mean_pop_density: si fourni, active IV (densite population, +
        distances_infra_km si disponible).
    :param distances_infra_km: dict {groupe: distance_km ou None}, voir
        core/vulnerabilite.CATEGORIES_GROUPES (sante, education,
        securite_urgence, marche).
    :param total_precip_mm: cumul de precipitation annuel (mm, Open-Meteo),
        active/complete IC si fourni (voir calculer_ic).
    :param no2_s5p_mol_m2: colonne tropospherique NO2 (Sentinel-5P), active
        IP si fourni (prioritaire sur no2_openmeteo_ugm3 si les deux le sont).
    :param no2_openmeteo_ugm3: NO2 de surface (Open-Meteo/CAMS), active IP
        si fourni et no2_s5p_mol_m2 absent.
    :return: dict {idt_score, iu_score, ic_score (si dispo), iv_score (si
        dispo), ip_score (si dispo), ire_score, ire_niveau, ire_phase,
        ire_composantes, ire_composantes_manquantes}
    """
    idt = calculer_idt(mean_ndvi, mean_nbr, mean_bsi, mean_evi=mean_evi, mean_savi=mean_savi, mean_si=mean_si,
                        superficie_reboisee_ha=superficie_reboisee_ha)
    iu = calculer_iu(mean_ndbi)

    scores = {"IDT": idt, "IU": iu}
    indices_veg_utilises = ["NDVI"] + (["EVI"] if mean_evi is not None else []) + (["SAVI"] if mean_savi is not None else [])
    libelle_idt = "Degradation des terres (" + "/".join(indices_veg_utilises) + " + NBR/BSI"
    libelle_idt += " + SI salinite" if mean_si is not None else ""
    libelle_idt += ", attenue par effort de reboisement regional ANSD/DEFCCS" if superficie_reboisee_ha is not None else ""
    libelle_idt += ")"
    composantes = [libelle_idt, "Urbanisation (NDBI)"]
    manquantes = []

    ic = calculer_ic(mean_lst=mean_lst, total_precip_mm=total_precip_mm, spi=spi)
    if ic is not None:
        scores["IC"] = ic
        secheresse_libelle = "SPI (climatologie locale 30 ans)" if spi is not None else (
            "precipitation (bornes fixes)" if total_precip_mm is not None else None)
        if mean_lst is not None and secheresse_libelle:
            composantes.append("Climatique (LST + secheresse : " + secheresse_libelle + ")")
        elif mean_lst is not None:
            composantes.append("Climatique partiel (LST uniquement, secheresse indisponible)")
        else:
            composantes.append("Climatique partiel (secheresse : " + secheresse_libelle + ", LST indisponible)")
    else:
        manquantes.append("Climatique (LST + secheresse)")

    if mean_pop_density is not None:
        iv = calculer_iv(mean_pop_density, distances_infra_km)
        scores["IV"] = iv
        groupes_trouves = sorted(g for g, d in (distances_infra_km or {}).items() if d is not None)
        if groupes_trouves:
            composantes.append("Vulnerabilite (densite population + acces " + ", ".join(groupes_trouves) + ")")
        else:
            composantes.append("Vulnerabilite partielle (densite population uniquement, Overpass indisponible)")
    else:
        manquantes.append("Vulnerabilite (population/infrastructures)")

    ip, source_ip = calculer_ip(european_aqi=european_aqi, no2_s5p_mol_m2=no2_s5p_mol_m2, no2_openmeteo_ugm3=no2_openmeteo_ugm3)
    if ip is not None:
        scores["IP"] = ip
        composantes.append("Pollution NO2 (" + source_ip + ")")
    else:
        manquantes.append("Pollution (NO2)")

    phase_str = str(len(scores)) + "/5 sous-indices (" + " + ".join(scores.keys()) + ")"

    poids = _renormaliser_poids(list(scores.keys()))
    ire_score = sum(poids[k] * scores[k] for k in scores)

    resultat = {
        "idt_score": idt,
        "iu_score": iu,
        "ire_score": ire_score,
        "ire_niveau": classifier_niveau(ire_score),
        "ire_phase": phase_str,
        "ire_composantes": " + ".join(composantes),
        "ire_composantes_manquantes": ", ".join(manquantes),
    }
    if "IC" in scores:
        resultat["ic_score"] = scores["IC"]
    if "IV" in scores:
        resultat["iv_score"] = scores["IV"]
    if "IP" in scores:
        resultat["ip_score"] = scores["IP"]
    if superficie_reboisee_ha is not None:
        resultat["superficie_reboisee_ha"] = superficie_reboisee_ha
    return resultat
