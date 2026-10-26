# -*- coding: utf-8 -*-
"""
core/export_pdf.py
--------------------
Generation du rapport PDF (synthese + tableau de resultats) a partir des
resultats d analyse.
"""

import datetime


def _verifier_dependance():
    """Message explicite si la bibliotheque d export n est pas installee
    (elle n est pas fournie par defaut avec QGIS)."""
    try:
        __import__("reportlab")
    except ImportError:
        raise RuntimeError(
            "La bibliotheque Python 'reportlab' n'est pas installee. Sous Windows, ouvrez l'OSGeo4W Shell "
            "et tapez : python -m pip install reportlab  (puis redemarrez QGIS)."
        )


def generer_rapport_pdf(nom, annee_debut, annee_fin, resultats, commentaire, chemin_sortie):
    """Assemble un rapport PDF a partir des resultats d analyse.

    :param nom: nom de la zone d etude
    :param annee_debut: annee de debut
    :param annee_fin: annee de fin
    :param resultats: dict des resultats (cles voir core.utils.INDICATOR_LABELS)
    :param commentaire: commentaire de synthese (core.commentaire_ia)
    :param chemin_sortie: chemin du fichier .pdf a produire
    """
    _verifier_dependance()
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from .utils import INDICATOR_LABELS

    styles = getSampleStyleSheet()
    style_titre = ParagraphStyle(
        "TitreSnAnalyser", parent=styles["Title"], textColor=colors.HexColor("#2E8B57")
    )

    doc = SimpleDocTemplate(
        chemin_sortie, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
    )

    elements = []
    elements.append(Paragraph("SN ANALYSER", style_titre))
    elements.append(Paragraph("Rapport d analyse territoriale", styles["Heading3"]))
    elements.append(Spacer(1, 12))

    elements.append(Paragraph("<b>Zone d etude :</b> " + nom, styles["Normal"]))
    elements.append(Paragraph("<b>Date analysee :</b> " + (str(annee_fin) if annee_debut == annee_fin else str(annee_debut) + " - " + str(annee_fin)), styles["Normal"]))
    elements.append(Paragraph(
        "<b>Date de generation :</b> " + datetime.datetime.now().strftime("%d/%m/%Y a %H:%M"),
        styles["Normal"],
    ))
    elements.append(Spacer(1, 16))

    elements.append(Paragraph("Synthese", styles["Heading2"]))
    elements.append(Paragraph(commentaire, styles["Normal"]))
    elements.append(Spacer(1, 16))

    elements.append(Paragraph("Resultats detailles", styles["Heading2"]))
    data = [["Indicateur", "Valeur"]]
    for cle, libelle in INDICATOR_LABELS.items():
        if cle in resultats:
            valeur = resultats[cle]
            data.append([libelle, ("%.2f" % valeur) if isinstance(valeur, float) else str(valeur)])

    if len(data) > 1:
        table = Table(data, colWidths=[10 * cm, 5 * cm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2E8B57")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F0F5F0")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        elements.append(table)
    else:
        elements.append(Paragraph("Aucun resultat detaille disponible.", styles["Normal"]))

    elements.append(Spacer(1, 20))
    elements.append(Paragraph(
        "<i>Genere automatiquement par le plugin QGIS SN Analyser (Geo Senegal 2026).</i>",
        styles["Normal"],
    ))

    doc.build(elements)
    return chemin_sortie
