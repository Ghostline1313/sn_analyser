# -*- coding: utf-8 -*-
"""
core/commentaire_ia.py
------------------------
Generation d un commentaire automatique de synthese a partir des resultats
d analyse (ex : "Entre 1984 et 2026, la commune de Rufisque presente une
augmentation de 68% des surfaces urbanisees...").

v1.0 : generation par gabarit (template), a partir des resultats reels des
modules Urbanisation et Environnement. Une integration a un modele de
langage (API Anthropic) pourra etre branchee ulterieurement pour enrichir
la formulation (voir docs.claude.com).
"""


# Seuil au-dela duquel un sous-indice IRE est considere comme une cause
# significative du niveau de risque global (voir generer_causes_et_recommandations).
SEUIL_CAUSE = 50.0

LIBELLES_CAUSES = {
    "idt_score": "Degradation des terres (vegetation/sols)",
    "iu_score": "Urbanisation/artificialisation",
    "ic_score": "Stress climatique (chaleur/secheresse)",
    "iv_score": "Vulnerabilite sociale (densite de population/acces aux services)",
    "ip_score": "Pollution de l air (NO2)",
    # 6 risques independants (panneau "Analyse de Risque Environnemental",
    # voir core/ire.RISQUES_CONFIG) -- memes libelles/recommandations que
    # les sous-indices IRE historiques quand ils se recoupent (degradation
    # des terres, urbanisation, climatique, pollution), + 2 nouveaux
    # (hydrologique, biodiversite).
    "risque_climatique_score": "Stress climatique (chaleur/secheresse ou exces de pluie)",
    "risque_pollution_score": "Pollution de l air (NO2/AQI)",
    "risque_degradation_terres_score": "Degradation des terres (vegetation/sols)",
    "risque_hydrologique_score": "Risque hydrologique (crue/exces d eau)",
    "risque_urbanisation_score": "Urbanisation/artificialisation",
    "risque_biodiversite_score": "Pression sur la biodiversite (perte de vegetation/habitat)",
}

RECOMMANDATIONS_PAR_CAUSE = {
    "idt_score": ["Reboisement / restauration vegetale", "Lutte contre le surpaturage et le defrichement"],
    "iu_score": ["Planification et maitrise de l etalement urbain", "Preservation des espaces verts et permeables"],
    "ic_score": ["Amenagements bioclimatiques (ombrage, vegetalisation urbaine)", "Gestion de la ressource en eau en periode de secheresse"],
    "iv_score": ["Renforcement des infrastructures de sante/education de proximite", "Amelioration de l acces aux services essentiels"],
    "ip_score": ["Surveillance renforcee de la qualite de l air", "Reduction des emissions (trafic, combustion)"],
    "risque_climatique_score": ["Amenagements bioclimatiques (ombrage, vegetalisation urbaine)", "Gestion de la ressource en eau (secheresse et exces de pluie)"],
    "risque_pollution_score": ["Surveillance renforcee de la qualite de l air", "Reduction des emissions (trafic, combustion)"],
    "risque_degradation_terres_score": ["Reboisement / restauration vegetale", "Lutte contre le surpaturage et le defrichement"],
    "risque_hydrologique_score": ["Gestion des eaux pluviales et des zones inondables", "Protection/restauration des zones humides tampons"],
    "risque_urbanisation_score": ["Planification et maitrise de l etalement urbain", "Preservation des espaces verts et permeables"],
    "risque_biodiversite_score": ["Protection des habitats naturels et corridors ecologiques", "Lutte contre les feux de brousse et le defrichement"],
}


def generer_causes_et_recommandations(resultats, seuil=SEUIL_CAUSE, max_causes=3):
    """A partir des sous-indices IRE disponibles (idt_score, iu_score,
    ic_score, iv_score, ip_score), identifie les causes principales du
    niveau de risque (sous-indices >= seuil, du plus eleve au moins eleve,
    max_causes retenues) et les recommandations associees.

    Si aucun sous-indice n atteint le seuil (risque global faible/homogene),
    la cause dominante est quand meme retenue pour eviter un resultat vide.

    :return: (causes: list[str], recommandations: list[str]), listes vides
        si resultats ne contient aucun sous-indice IRE.
    """
    sous_indices = [
        (cle, resultats[cle]) for cle in LIBELLES_CAUSES
        if cle in resultats and resultats[cle] is not None
    ]
    if not sous_indices:
        return [], []

    sous_indices.sort(key=lambda t: t[1], reverse=True)
    causes_retenues = [cle for cle, score in sous_indices if score >= seuil][:max_causes]
    if not causes_retenues:
        causes_retenues = [sous_indices[0][0]]

    causes = [LIBELLES_CAUSES[c] for c in causes_retenues]
    recommandations = []
    for c in causes_retenues:
        for r in RECOMMANDATIONS_PAR_CAUSE[c]:
            if r not in recommandations:
                recommandations.append(r)

    return causes, recommandations


def generer_commentaire(commune, annee_debut, annee_fin, resultats):
    """Genere un commentaire de synthese en francais a partir des resultats d analyse.

    :param commune: nom de la zone d etude (ex: "Rufisque Centre (Nord)")
    :param annee_debut: annee de debut de la periode analysee
    :param annee_fin: annee de fin de la periode analysee
    :param resultats: dict pouvant contenir les cles suivantes (toutes optionnelles) :
        - 'surface_urbanisee_debut', 'surface_urbanisee_fin' (ha)
        - 'taux_croissance_urbaine' (%/an)
        - 'surface_vegetation_debut', 'surface_vegetation_fin' (ha)
        - 'variation_vegetation_pct' (%)
        - 'recul_cotier_max_m' (m, optionnel)
    :return: commentaire (str), un paragraphe unique
    """
    phrases = []
    periode = str(annee_debut) + " et " + str(annee_fin)

    surf_urb_debut = resultats.get("surface_urbanisee_debut")
    surf_urb_fin = resultats.get("surface_urbanisee_fin")
    taux_urb = resultats.get("taux_croissance_urbaine")

    if surf_urb_debut is not None and surf_urb_fin is not None:
        variation_urb_pct = (
            ((surf_urb_fin - surf_urb_debut) / surf_urb_debut * 100)
            if surf_urb_debut > 0 else 0.0
        )
        sens_urb = "une augmentation" if variation_urb_pct >= 0 else "une diminution"
        phrase = (
            "Entre " + periode + ", la zone de " + commune + " presente " + sens_urb +
            " de " + "%.0f" % abs(variation_urb_pct) + " % des surfaces urbanisees" +
            " (de " + "%.0f" % surf_urb_debut + " a " + "%.0f" % surf_urb_fin + " hectares)"
        )
        if taux_urb is not None:
            phrase += ", soit un taux de croissance annuel moyen de " + "%.1f" % taux_urb + " %/an"
        phrases.append(phrase + ".")

    surf_veg_debut = resultats.get("surface_vegetation_debut")
    surf_veg_fin = resultats.get("surface_vegetation_fin")
    if surf_veg_debut is not None and surf_veg_fin is not None:
        variation_veg_pct = (
            ((surf_veg_fin - surf_veg_debut) / surf_veg_debut * 100)
            if surf_veg_debut > 0 else 0.0
        )
        if variation_veg_pct < 0:
            phrases.append(
                "La vegetation diminue de " + "%.0f" % abs(variation_veg_pct) +
                " % sur la meme periode (" + "%.0f" % surf_veg_debut + " a " +
                "%.0f" % surf_veg_fin + " hectares)."
            )
        else:
            phrases.append(
                "La couverture vegetale se maintient, voire progresse legerement (+" +
                "%.0f" % variation_veg_pct + " %, de " + "%.0f" % surf_veg_debut +
                " a " + "%.0f" % surf_veg_fin + " hectares)."
            )

    recul_cotier = resultats.get("recul_cotier_max_m")
    if recul_cotier is not None:
        phrases.append(
            "Le littoral recule localement jusqu a " + "%.0f" % recul_cotier + " m."
        )

    # 6 risques environnementaux INDEPENDANTS (voir core/ire.RISQUES_CONFIG /
    # calculer_risques_independants) - remplace l ancien score IRE combine.
    # Une phrase par risque coche et effectivement calcule.
    RISQUES_CLES = ["climatique", "pollution", "degradation_terres", "hydrologique",
                     "urbanisation", "biodiversite"]
    risques_presents = [c for c in RISQUES_CLES if resultats.get("risque_" + c + "_score") is not None]
    if risques_presents:
        for cle in risques_presents:
            score = resultats.get("risque_" + cle + "_score")
            niveau = resultats.get("risque_" + cle + "_niveau")
            label = resultats.get("risque_" + cle + "_label", cle)
            phrase = (
                label + " de " + commune + " en " + str(annee_fin) + " est de " +
                "%.1f" % score + "/100, soit un niveau " + (niveau.lower() if niveau else "indetermine") + "."
            )
            source = resultats.get("risque_" + cle + "_source")
            if source:
                phrase += " (source : " + source + ".)"
            phrases.append(phrase)

        causes, recommandations = generer_causes_et_recommandations(resultats)
        if causes:
            phrases.append("Causes principales : " + ", ".join(causes) + ".")

        superficie_reboisee = resultats.get("superficie_reboisee_ha")
        if superficie_reboisee is not None:
            phrases.append(
                "Effort de reboisement regional (ANSD/DEFCCS, 2023) : " +
                "%.0f" % superficie_reboisee + " ha."
            )
            if superficie_reboisee > 1000.0 and "Reboisement / restauration vegetale" in recommandations:
                recommandations.remove("Reboisement / restauration vegetale")

        aire_protegee = resultats.get("aire_protegee_proche")
        distance_aire = resultats.get("aire_protegee_distance_km")
        if aire_protegee:
            designation = (
                (" (" + aire_protegee + ")")
                if "non renseigne" not in aire_protegee else ""
            )
            phrases.append(
                "A proximite" + (" (" + "%.1f" % distance_aire + " km)" if distance_aire is not None else "") +
                " d une aire protegee" + designation +
                " : vigilance particuliere recommandee sur la degradation/urbanisation en peripherie."
            )
            if "Protection des aires naturelles proches" not in recommandations:
                recommandations.append("Protection des aires naturelles proches")

        if recommandations:
            phrases.append("Recommandations : " + ", ".join(recommandations) + ".")

        # Contexte (informations, pas des risques notes) : population,
        # infrastructures sensibles, climat complementaire (vent/humidite/ETP).
        for cle_ctx, intitule in (
            ("contexte_relief", "Relief (MNT Copernicus 30 m)"),
            ("contexte_courbes_niveau", "Courbes de niveau"),
            ("contexte_bassins", "Bassins versants et ecoulements"),
            ("contexte_sols", "Types de sol (SoilGrids)"),
        ):
            if resultats.get(cle_ctx):
                phrases.append(intitule + " : " + resultats[cle_ctx] + ".")
        if resultats.get("contexte_source_imagerie"):
            phrases.append("Imagerie satellite : " + resultats["contexte_source_imagerie"] + ".")
        occupation_sol = resultats.get("contexte_occupation_sol")
        if occupation_sol:
            phrases.append(
                "Occupation du sol (" + resultats.get("contexte_source_occupation_sol", "ESA WorldCover") + ") : "
                + occupation_sol + "."
            )
        densite = resultats.get("contexte_population_densite")
        if densite is not None:
            source_pop = resultats.get("contexte_source_population", "")
            phrases.append(
                "Contexte demographique : densite moyenne de " + "%.0f" % densite +
                " hab/km2" + (" (" + source_pop + ")" if source_pop else "") + "."
            )
        distances_infra = resultats.get("contexte_distances_infra_km")
        if distances_infra:
            LIBELLES_INFRA = {
                "sante": "sante", "education": "education",
                "securite_urgence": "securite/urgence", "marche": "marche",
            }
            parts = [
                LIBELLES_INFRA.get(g, g) + " a " + "%.1f" % d + " km"
                for g, d in sorted(distances_infra.items())
            ]
            phrases.append("Acces aux infrastructures sensibles : " + ", ".join(parts) + ".")
        vent = resultats.get("contexte_vent_moyen_kmh")
        humidite = resultats.get("contexte_humidite_moyenne_pct")
        eto = resultats.get("contexte_evapotranspiration_cumul_mm")
        contexte_climat = []
        if vent is not None:
            contexte_climat.append("vent moyen " + "%.0f" % vent + " km/h")
        if humidite is not None:
            contexte_climat.append("humidite moyenne " + "%.0f" % humidite + " %")
        if eto is not None:
            contexte_climat.append("evapotranspiration cumulee " + "%.0f" % eto + " mm/an")
        if contexte_climat:
            phrases.append("Contexte climatique complementaire : " + ", ".join(contexte_climat) + ".")

    ire_score = resultats.get("ire_score")
    ire_niveau = resultats.get("ire_niveau")
    ire_phase = resultats.get("ire_phase")
    ire_manquantes = resultats.get("ire_composantes_manquantes")
    if ire_score is not None:
        phrase_ire = (
            "L Indice de Risque Environnemental (IRE) de " + commune + " en " + str(annee_fin) +
            " est de " + "%.1f" % ire_score + "/100, soit un niveau de risque " +
            (ire_niveau.lower() if ire_niveau else "indetermine") + "."
        )
        if ire_manquantes:
            phrase_ire += (
                " (Score partiel - phase " + (ire_phase or "?") +
                " : composante(s) manquante(s) : " + ire_manquantes + ".)"
            )
        elif ire_phase:
            phrase_ire += " (Score complet - " + ire_phase + ".)"
        phrases.append(phrase_ire)

        causes, recommandations = generer_causes_et_recommandations(resultats)
        if causes:
            phrases.append("Causes principales : " + ", ".join(causes) + ".")

        superficie_reboisee = resultats.get("superficie_reboisee_ha")
        if superficie_reboisee is not None:
            phrases.append(
                "Effort de reboisement regional (ANSD/DEFCCS, 2023) : " +
                "%.0f" % superficie_reboisee + " ha."
            )
            # Programme deja substantiel (>1000 ha/an au niveau regional) :
            # la recommandation generique de reboisement est redondante,
            # on la retire pour eviter de suggerer une action deja en cours.
            if superficie_reboisee > 1000.0 and "Reboisement / restauration vegetale" in recommandations:
                recommandations.remove("Reboisement / restauration vegetale")

        aire_protegee = resultats.get("aire_protegee_proche")
        distance_aire = resultats.get("aire_protegee_distance_km")
        if aire_protegee:
            designation = (
                (" (" + aire_protegee + ")")
                if "non renseigne" not in aire_protegee else ""
            )
            phrases.append(
                "A proximite (" + "%.1f" % distance_aire + " km) d une aire protegee" + designation +
                " : vigilance particuliere recommandee sur la degradation/urbanisation en peripherie."
            )
            if "Protection des aires naturelles proches" not in recommandations:
                recommandations.append("Protection des aires naturelles proches")

        if recommandations:
            phrases.append("Recommandations : " + ", ".join(recommandations) + ".")

    if not phrases:
        return (
            "Donnees insuffisantes pour generer un commentaire de synthese sur " +
            commune + " (" + periode + ")."
        )

    conclusion_elements = []
    if surf_urb_debut is not None and surf_urb_fin is not None and surf_urb_fin > surf_urb_debut:
        conclusion_elements.append("une forte pression anthropique")
    if recul_cotier is not None:
        conclusion_elements.append("cotiere")
    if surf_veg_debut is not None and surf_veg_fin is not None and surf_veg_fin < surf_veg_debut:
        conclusion_elements.append("une pression sur les milieux naturels")

    if conclusion_elements:
        phrases.append(
            "Ces evolutions traduisent " + ", ".join(conclusion_elements) + "."
        )

    return " ".join(phrases)
