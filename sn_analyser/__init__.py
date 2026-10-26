# -*- coding: utf-8 -*-
"""
SN Analyser
------------
Observatoire multi-thématique du territoire sénégalais (urbanisation,
érosion côtière, dégradation environnementale).

Point d'entrée requis par QGIS. Ne pas ajouter de logique métier ici.
"""


def classFactory(iface):
    """Charge la classe SnAnalyser.

    :param iface: interface QGIS
    :type iface: QgsInterface
    """
    from .plugin import SnAnalyser
    return SnAnalyser(iface)
