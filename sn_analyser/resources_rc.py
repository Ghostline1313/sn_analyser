# -*- coding: utf-8 -*-
"""
resources_rc.py
-----------------
Fichier à GÉNÉRER automatiquement à partir de resources.qrc, via :

    pyrcc5 -o resources_rc.py resources.qrc

(depuis l'invite OSGeo4W Shell, avec l'environnement QGIS actif)

Non requis pour le fonctionnement actuel du plugin : plugin.py charge
actuellement les icônes directement par chemin de fichier
(icons/icon.png, etc.), pas via le système de ressources Qt compilé.
Ce fichier est fourni en placeholder pour respecter l'arborescence
standard des plugins QGIS (pb_tool) et pourra être activé plus tard
si l'on souhaite embarquer les icônes dans les .ui via Qt Designer.
"""
