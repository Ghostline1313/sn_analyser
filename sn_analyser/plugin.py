# -*- coding: utf-8 -*-
"""
SN Analyser - plugin.py
-------------------------
Classe principale du plugin QGIS. Gère l'intégration à l'interface QGIS
(icône, menu, panneau ancrable) et l'orchestration entre l'UI (ui/accueil.ui)
et les modules d'analyse (core/).

Logique de sélection de la zone d'étude : identique à Sen Couverture
Terrestre — l'utilisateur choisit un niveau administratif (Région /
Département / Commune) puis sélectionne le nom précis dans la liste
correspondante pour lancer l'analyse sur cette entité.

Squelette v1.0 — la logique des modules core/ sera branchée un par un.
"""

import os
import json

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QDockWidget
from qgis.PyQt import uic
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsWkbTypes, QgsFeature, QgsFillSymbol,
    QgsSimpleFillSymbolLayer, QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsMapLayer,
)

PLUGIN_DIR = os.path.dirname(__file__)


def _corriger_certificats_tls():
    """Neutralise les variables d environnement de certificats TLS qui
    pointent vers un fichier inexistant (ex: CURL_CA_BUNDLE laisse par
    l installateur PostgreSQL : C:\\Program Files\\PostgreSQL\\18\\ssl\\certs\\
    ca-bundle.crt). `requests` les utilise en priorite et echoue alors sur
    TOUS les telechargements HTTPS (GEE, Landsat, Sentinel...) avec
    "Could not find a suitable TLS CA certificate bundle". On les remplace
    par le bundle certifi fourni avec QGIS, pour cette session seulement."""
    try:
        import certifi
        bundle = certifi.where()
    except Exception:
        bundle = None
    for var in ("CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        valeur = os.environ.get(var)
        if valeur and not os.path.isfile(valeur):
            if bundle and os.path.isfile(bundle):
                os.environ[var] = bundle
            else:
                os.environ.pop(var, None)


_corriger_certificats_tls()

# GeoPackage de référence bundlé avec le plugin (régions / départements / communes du Sénégal)
REFERENCE_GPKG = os.path.join(PLUGIN_DIR, "data", "limites_administratives.gpkg")

# Configuration des niveaux administratifs proposés dans "Zone d'étude"
NIVEAU_CONFIG = {
    "Région": {
        "gpkg_layer": "regions",
        "project_name": "Régions du Sénégal",
        "name_field": "shapeName",
        "keywords": ["région", "region"],
    },
    "Commune": {
        "gpkg_layer": "communes",
        "project_name": "Limites communes",
        "name_field": "name",
        "keywords": ["commune"],
    },
}

# Option speciale de cbNiveau : zone delimitee par l utilisateur (dessin sur
# la carte ou entites selectionnees), voir _ajouter_controles_dessin.
NIVEAU_DESSIN = "✏️ Zone délimitée"
NOM_ZONE_DESSINEE_DEFAUT = "Zone_delimitee"

# Indices utilises pour deviner le champ "nom" d'une couche detectee
# dynamiquement dans le projet (meme heuristique que Sen Couverture
# Terrestre / qgis_layers_helper.guess_name_field)
NAME_FIELD_HINTS = ["ccrca", "nom", "name", "commune", "comm", "libelle", "label", "adm"]


def _guess_name_field(layer, hints=NAME_FIELD_HINTS):
    """Devine le champ contenant le nom de l'entite, par heuristique sur le nom du champ."""
    field_names = [f.name() for f in layer.fields()]
    lower_map = {f.lower(): f for f in field_names}
    for hint in hints:
        for lower_name, original in lower_map.items():
            if hint in lower_name:
                return original
    return field_names[0] if field_names else None

# Cache module-level pour data/zones_cotieres.json (liste des Régions/Communes
# qui touchent le littoral atlantique -- genere par intersection OSM
# natural=coastline, tampon 1.5 km, avec data/limites_administratives.gpkg).
# L erosion cotiere (NDWI) n a de sens que sur ces zones : ailleurs, le NDWI
# capte des plans d eau interieurs/saisonniers sans rapport avec un trait de
# cote reel.
_ZONES_COTIERES_CACHE = None
_REBOISEMENT_CACHE = None

# Cache module-level pour data/population_ansd_2023.json (recensement RGPHAE
# 2023, ANSD -- population reelle par Region/Commune, fourni par l utilisateur
# et agrege par nom normalise depuis Recensement_ANSD_2023.xlsx). Utilise en
# PRIORITE sur le raster WorldPop (estimation 2020, 1km) pour l IV quand la
# zone est couverte (410/428 communes, 14/14 regions) ; WorldPop reste le
# filet de secours pour les zones non couvertes.
_POPULATION_ANSD_CACHE = None


from qgis.core import QgsTask as _QgsTask


class _TacheNumerisation(_QgsTask):
    """Telechargement OSM + extraction + courbes de niveau, en tache de fond
    (barre de progression QGIS, annulable). Aucun acces a l interface ici :
    le chargement des couches se fait dans finished() (fil principal)."""

    def __init__(self, plugin, cles, bbox, geojson, prefixe, nom, aire_km2):
        super().__init__("SN Analyser - Numérisation " + nom, _QgsTask.CanCancel)
        self.plugin = plugin
        self.cles = cles
        self.bbox = bbox
        self.geojson = geojson
        self.gpkg = prefixe + "_osm.gpkg"
        self.courbes_gpkg = prefixe + "_courbes.gpkg"
        self.prefixe = prefixe
        self.nom = nom
        self.aire = aire_km2
        self.resultats = []
        self.avertissements = []
        self.erreur = None
        self.duree = 0.0
        self.annulee = False

    def run(self):
        import time
        import json
        import hashlib
        import threading
        from .core import numerisation as core_num
        t0 = time.time()
        self.annulee = False
        try:
            # Courbes de niveau (MNT) EN PARALLELE du telechargement OSM
            fil_courbes = None
            if "courbes_niveau" in self.cles:
                def _fil():
                    try:
                        self._courbes()
                    except Exception as exc:
                        self.avertissements.append("courbes de niveau indisponibles (" + str(exc)[:120] + ")")
                fil_courbes = threading.Thread(target=_fil, daemon=True)
                fil_courbes.start()

            cles_osm = [c for c in self.cles if c != "courbes_niveau"]
            if cles_osm:
                self.setProgress(5)
                delai = int(min(180, 45 + self.aire * 0.6))
                requete = core_num.construire_requete(cles_osm, self.bbox, timeout=delai)
                # Cache : meme zone + memes entites dans les 7 derniers jours
                dossier_cache = os.path.join(os.path.dirname(self.prefixe), "cache")
                os.makedirs(dossier_cache, exist_ok=True)
                cache = os.path.join(dossier_cache, hashlib.md5(requete.encode()).hexdigest() + ".json")
                data = None
                if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 7 * 86400:
                    try:
                        with open(cache, encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:
                        data = None
                if data is None:
                    data = core_num.telecharger_overpass(requete, delai=delai + 30, annule=self.isCanceled)
                    try:
                        with open(cache, "w", encoding="utf-8") as f:
                            json.dump(data, f)
                    except Exception:
                        pass
                if self.isCanceled():
                    raise core_num.Annulation()
                self.setProgress(70)
                self.resultats = core_num.extraire(data, cles_osm, self.geojson, self.gpkg)
            if fil_courbes is not None:
                self.setProgress(85)
                while fil_courbes.is_alive():
                    if self.isCanceled():
                        raise core_num.Annulation()
                    fil_courbes.join(0.3)
            self.setProgress(100)
            self.duree = time.time() - t0
            return True
        except core_num.Annulation:
            self.annulee = True
            return False
        except Exception as exc:
            if self.isCanceled():
                self.annulee = True
                return False
            self.erreur = str(exc)
            self.duree = time.time() - t0
            return False

    def _courbes(self):
        import numpy as np
        from osgeo import gdal
        from .core import relief_hydro as core_rh
        mnt_large = self.prefixe + "_mnt.tif"
        core_rh.telecharger_mnt(self.bbox, mnt_large, 30, 0.005)
        mnt = core_rh._decouper(mnt_large, self.geojson, self.prefixe + "_mnt_zone.tif", -9999)
        a = gdal.Open(mnt).ReadAsArray().astype(float)
        a = a[a != -9999]
        if a.size == 0:
            raise RuntimeError("MNT vide")
        pas = core_rh.intervalle_courbes(float(a.max() - a.min()))
        _gpkg, n = core_rh.courbes_de_niveau(mnt, self.courbes_gpkg, pas)
        self.resultats.append(("courbes_niveau", "lignes", "courbes", n))
        for f in (mnt_large,):
            try:
                os.remove(f)
            except OSError:
                pass

    def finished(self, result):
        self.plugin._charger_numerisation(self, bool(result))



class SnAnalyser:
    """Classe principale du plugin SN Analyser."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = PLUGIN_DIR
        self.actions = []
        self.menu = "&SN Analyser"
        self.toolbar = self.iface.addToolBar("SN Analyser")
        self.toolbar.setObjectName("SnAnalyserToolbar")
        self.dock_widget = None
        self.dernier_resultat = None  # {"nom", "annee_debut", "annee_fin", "resultats", "commentaire"}

    # ------------------------------------------------------------------
    # Cycle de vie du plugin
    # ------------------------------------------------------------------
    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icons", "icon.png")
        action = QAction(QIcon(icon_path), "SN Analyser", self.iface.mainWindow())
        action.triggered.connect(self.run)
        action.setEnabled(True)

        self.toolbar.addAction(action)
        self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)
        del self.toolbar
        self._desactiver_outil_dessin()
        # Arret propre des taches d arriere-plan (telechargements) avant le
        # dechargement : sinon un rechargement de l extension pendant qu elles
        # tournent peut faire planter QGIS (access violation, 26/10/2026).
        for pool in getattr(self, "_pools_actifs", []) or []:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        self._pools_actifs = []

        if self.dock_widget is not None:
            self.iface.removeDockWidget(self.dock_widget)
            self.dock_widget = None

    # ------------------------------------------------------------------
    # Interface principale
    # ------------------------------------------------------------------
    def run(self):
        """Ouvre (ou remet au premier plan) le panneau principal SN Analyser."""
        if self.dock_widget is None:
            ui_path = os.path.join(self.plugin_dir, "ui", "accueil.ui")
            self.dock_widget = QDockWidget("SN Analyser", self.iface.mainWindow())
            self.dock_widget.setObjectName("SnAnalyserDockWidget")

            widget = uic.loadUi(ui_path)
            self.dock_widget.setWidget(widget)

            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock_widget)
            self._connect_signals(widget)
            self._ajouter_controles_dessin(widget)
            self._ajouter_controle_numerisation(widget)
            self._populate_niveau_combo(widget)
        else:
            self.dock_widget.show()
            self.dock_widget.raise_()

    # ------------------------------------------------------------------
    # Connexions UI <-> core/
    # ------------------------------------------------------------------
    def _pulse(self):
        """Force le traitement des evenements Qt en attente (repeindre la
        fenetre, afficher les messages deja postes dans la barre). Sans cela,
        une longue sequence de telechargements/calculs synchrones bloque le
        thread principal et Windows affiche l application comme "ne repond
        plus", alors qu elle travaille simplement en silence. Appele entre
        chaque etape couteuse des pipelines d analyse."""
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.processEvents()

    def _log_critical(self, message):
        """Affiche un message d erreur dans la barre de messages (transitoire)
        ET le journalise dans le panneau persistant Vue > Panneaux > Journal
        des messages > onglet SN Analyser, pour pouvoir le relire apres coup
        (la barre de messages disparait au bout de quelques secondes).
        Garde anti-reentrance : evite une boucle infinie si l affichage du
        message (barre ou journal) redeclenche lui-meme un appel a
        _log_critical (observe empiriquement avec certaines connexions
        de signaux Qt actives en session)."""
        if getattr(self, "_log_critical_en_cours", False):
            return
        self._log_critical_en_cours = True
        try:
            from qgis.core import Qgis, QgsMessageLog
            self.iface.messageBar().pushCritical("SN Analyser", message)
            QgsMessageLog.logMessage(message, "SN Analyser", level=Qgis.Critical)
        finally:
            self._log_critical_en_cours = False

    def _connect_signals(self, widget):
        """Relie les widgets de accueil.ui aux modules core/.

        À ce stade (squelette), seul le bouton Analyser est connecté à un
        stub. Chaque module (urbanisation, erosion, environnement, exports)
        sera branché ici au fur et à mesure de son développement.
        """
        if hasattr(widget, "btnAnalyser"):
            widget.btnAnalyser.clicked.connect(lambda: self._on_analyser(widget))
        if hasattr(widget, "cbNiveau"):
            widget.cbNiveau.currentTextChanged.connect(
                lambda niveau: self._populate_nom_combo(widget, niveau)
            )
        if hasattr(widget, "cbNom"):
            widget.cbNom.currentTextChanged.connect(
                lambda nom: self._maj_disponibilite_erosion(widget)
            )
            widget.cbNom.currentTextChanged.connect(
                lambda nom: self._on_nom_selected(widget)
            )
        if hasattr(widget, "btnExportPDF"):
            widget.btnExportPDF.clicked.connect(lambda: self._on_export(widget, "pdf"))
        if hasattr(widget, "btnExportWord"):
            widget.btnExportWord.clicked.connect(lambda: self._on_export(widget, "word"))

        self._connect_sous_classes(widget)

    # Sous-classes affichees/masquees sous chaque risque principal, cochees a
    # l ouverture du panneau si le risque parent est deja coche. Voir
    # RISQUE_SOUS_CLASSES pour le detail de chaque groupe.
    RISQUE_SOUS_CLASSES = {
        "chkRisqueClimatique": ["chkClimTemperature", "chkClimSecheresse"],
        "chkRisquePollution": ["chkPolAQI", "chkPolIndustrie", "chkPollutionS5P"],
        "chkRisqueDegradationTerres": ["chkDegVegetation", "chkDegBrulis", "chkDegSolNu", "chkDegSalinisation"],
        "chkRisqueHydrologique": ["chkHydroZonesHumides", "chkHydroPrecipitations",
                                   "chkHydroReseauHydro", "chkHydroLacs", "chkHydroErosionCotiere",
                                   "chkHydroRelief", "chkHydroCourbes", "chkHydroBassins", "chkHydroSols"],
        "chkRisqueUrbanisation": ["chkUrbArtificialisation", "chkUrbDensitePop", "chkUrbInfrastructures",
                                   "chkUrbEvolutionBati"],
        "chkRisqueBiodiversite": ["chkBioVegetation", "chkBioFeux", "chkBioAiresProtegees",
                                   "chkBioEvolutionVegetation"],
    }

    def _connect_sous_classes(self, widget):
        """Affiche/masque les sous-classes de chaque risque selon l etat de
        sa case parente (cochee = sous-classes visibles pour affiner le
        calcul, decochee = sous-classes masquees). Etat initial applique
        immediatement (panneau tout juste ouvert).
        """
        for parent_attr, enfants_attrs in self.RISQUE_SOUS_CLASSES.items():
            if not hasattr(widget, parent_attr):
                continue
            parent = getattr(widget, parent_attr)
            enfants = [getattr(widget, a) for a in enfants_attrs if hasattr(widget, a)]

            def _maj_visibilite(checked, enfants=enfants):
                for e in enfants:
                    e.setVisible(checked)

            parent.toggled.connect(_maj_visibilite)
            _maj_visibilite(parent.isChecked())

    def _est_zone_cotiere(self, niveau, nom):
        """Indique si la zone (niveau + nom) touche le littoral atlantique.

        Référence précalculée dans data/zones_cotieres.json (voir commentaire
        près de _ZONES_COTIERES_CACHE plus haut dans ce fichier).
        """
        if niveau == NIVEAU_DESSIN:
            return bool(getattr(self, "_zone_dessinee_info", {}).get("cotiere"))
        global _ZONES_COTIERES_CACHE
        if _ZONES_COTIERES_CACHE is None:
            chemin = os.path.join(self.plugin_dir, "data", "zones_cotieres.json")
            try:
                with open(chemin, encoding="utf-8") as f:
                    brut = json.load(f)
            except Exception as e:
                self._log_critical("Reference zones cotieres illisible : " + str(e))
                brut = {}
            # Comparaison insensible a la casse/accents-espaces superflus :
            # la couche "Nom" utilisee peut desormais provenir d une couche
            # deja chargee dans le projet (detection dynamique, cf.
            # _get_reference_layer) avec une casse differente du GeoJSON
            # bundle de reference (ex: "NGOR" vs "Ngor").
            _ZONES_COTIERES_CACHE = {
                niv: {n.strip().lower() for n in noms} for niv, noms in brut.items()
            }
        return nom.strip().lower() in _ZONES_COTIERES_CACHE.get(niveau, set())

    def _population_ansd(self, niveau, nom):
        """Population reelle (recensement RGPHAE 2023, ANSD) pour la zone
        (niveau + nom), si couverte par data/population_ansd_2023.json.

        :return: population (int) ou None si la zone n est pas couverte
            (dans ce cas, l appelant doit se rabattre sur WorldPop).
        """
        global _POPULATION_ANSD_CACHE
        if _POPULATION_ANSD_CACHE is None:
            chemin = os.path.join(self.plugin_dir, "data", "population_ansd_2023.json")
            try:
                with open(chemin, encoding="utf-8") as f:
                    _POPULATION_ANSD_CACHE = json.load(f)
            except Exception as e:
                self._log_critical("Reference population ANSD illisible : " + str(e))
                _POPULATION_ANSD_CACHE = {}
        return _POPULATION_ANSD_CACHE.get(niveau, {}).get(nom)

    def _effort_reboisement_region(self, niveau, nom, feature):
        """Superficie reboisee 2023 (ha, ANSD/DEFCCS) pour la region de la
        zone analysee -- voir data/reboisement_regions.json. Donnee
        REGIONALE uniquement (le Ratio Reboisement/Deboisement complet
        n est disponible qu au niveau national dans la source) : si
        niveau == "Commune", on remonte a la region parente via le champ
        NAME_1 de la couche communes (deja present sur `feature`, pas besoin
        d une jointure separee).

        :return: superficie (ha, float) ou None si indisponible.
        """
        global _REBOISEMENT_CACHE
        if _REBOISEMENT_CACHE is None:
            chemin = os.path.join(self.plugin_dir, "data", "reboisement_regions.json")
            try:
                with open(chemin, encoding="utf-8") as f:
                    _REBOISEMENT_CACHE = json.load(f)
            except Exception as e:
                self._log_critical("Reference reboisement illisible : " + str(e))
                _REBOISEMENT_CACHE = {"regions": {}}

        if niveau == "Région":
            nom_region = nom
        elif niveau == "Commune":
            try:
                nom_region = feature["NAME_1"]
            except KeyError:
                nom_region = None
        elif niveau == NIVEAU_DESSIN:
            # Region majoritaire (en surface) calculee au moment du dessin
            nom_region = getattr(self, "_zone_dessinee_info", {}).get("region")
        else:
            nom_region = None

        if nom_region is None:
            return None
        return _REBOISEMENT_CACHE.get("regions", {}).get(nom_region)

    def _maj_disponibilite_erosion(self, widget):
        """Active/desactive la case Erosion cotiere selon que la zone
        selectionnee (niveau + nom) touche ou non le littoral. L analyse
        NDWI de trait de cote n a pas de sens hors zone cotiere (elle y
        capterait des plans d eau interieurs/saisonniers, pas une erosion
        reelle) -- d ou l incoherence des resultats observee sur des zones
        non cotieres avant ce correctif.
        """
        if not (hasattr(widget, "cbNiveau") and hasattr(widget, "cbNom") and hasattr(widget, "chkErosion")):
            return
        niveau = widget.cbNiveau.currentText()
        nom = self._nom_zone_courante(widget)
        if not nom:
            return
        if niveau == NIVEAU_DESSIN and getattr(self, "_zone_dessinee_info", None) is None:
            return
        cotiere = self._est_zone_cotiere(niveau, nom)
        widget.chkErosion.setEnabled(cotiere)
        if not cotiere:
            widget.chkErosion.setChecked(False)
            widget.chkErosion.setToolTip(
                niveau + " « " + nom + " » ne touche pas le littoral : "
                "l analyse d erosion cotiere n est pas applicable ici."
            )
        else:
            # Coche par defaut des qu une zone cotiere est selectionnee,
            # pour retrouver le comportement d origine du widget (coche a
            # l ouverture) plutot que de laisser la case bloquee decochee
            # apres un passage par une zone non cotiere.
            widget.chkErosion.setChecked(True)
            widget.chkErosion.setToolTip("")

    # ------------------------------------------------------------------
    # Zone d'étude : sélection Niveau (Région/Département/Commune) + Nom
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Zone delimitee par l utilisateur (dessin sur la carte / selection)
    # ------------------------------------------------------------------
    def _ajouter_controles_dessin(self, widget):
        """Ajoute sous la Zone d etude les controles du mode NIVEAU_DESSIN :
        nom de la zone + boutons Polygone / Rectangle / Selection. Construits
        en code (pas dans accueil.ui) et masques tant que ce mode n est pas
        choisi dans cbNiveau."""
        from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QLabel
        layout = getattr(widget, "zoneEtudeLayout", None)
        if layout is None:
            return
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 4, 0, 0)
        v.addWidget(QLabel("Nom de la zone :"))
        widget.leNomZone = QLineEdit()
        widget.leNomZone.setPlaceholderText(NOM_ZONE_DESSINEE_DEFAUT)
        widget.leNomZone.setToolTip("Utilise dans les noms de fichiers, le rapport et le commentaire")
        v.addWidget(widget.leNomZone)
        h = QHBoxLayout()
        widget.btnDessinPolygone = QPushButton("\u270f\ufe0f Polygone")
        widget.btnDessinPolygone.setToolTip(
            "Dessiner un polygone sur la carte :\n"
            "clic gauche = ajouter un sommet\n"
            "clic droit ou Entree = terminer\n"
            "Retour arriere = retirer le dernier sommet\n"
            "Echap = abandonner")
        widget.btnDessinRectangle = QPushButton("\u25ad Rectangle")
        widget.btnDessinRectangle.setToolTip("Cliquer-glisser sur la carte pour tracer un rectangle")
        widget.btnDessinSelection = QPushButton("\U0001F4D0 S\u00e9lection")
        widget.btnDessinSelection.setToolTip(
            "Utiliser les entites selectionnees de la couche polygone active\n"
            "(fusionnees en une seule zone)")
        for b in (widget.btnDessinPolygone, widget.btnDessinRectangle, widget.btnDessinSelection):
            h.addWidget(b)
        v.addLayout(h)
        widget.lblZoneDessinee = QLabel("Aucune zone d\u00e9limit\u00e9e.")
        widget.lblZoneDessinee.setWordWrap(True)
        v.addWidget(widget.lblZoneDessinee)
        layout.addWidget(box)
        widget.boxDessinZone = box
        box.setVisible(False)

        widget.btnDessinPolygone.clicked.connect(lambda: self._demarrer_dessin(widget, "polygone"))
        widget.btnDessinRectangle.clicked.connect(lambda: self._demarrer_dessin(widget, "rectangle"))
        widget.btnDessinSelection.clicked.connect(lambda: self._zone_depuis_selection(widget))

    def _basculer_mode_dessin(self, widget, actif):
        """Affiche les controles de dessin (et masque Nom) en mode
        NIVEAU_DESSIN, et inversement."""
        for attr in ("lblNom", "cbNom"):
            if hasattr(widget, attr):
                getattr(widget, attr).setVisible(not actif)
        if hasattr(widget, "boxDessinZone"):
            widget.boxDessinZone.setVisible(actif)
        if not actif:
            self._desactiver_outil_dessin()
            return
        layer, feature = self._zone_dessinee_layer_feature()
        if feature is not None and feature.hasGeometry():
            self._update_zone_highlight(feature.geometry(), layer.crs())
            self._zoom_to_geometry(feature.geometry(), layer.crs())
        self._maj_disponibilite_erosion(widget)

    def _nom_zone_courante(self, widget):
        """Nom de la zone d etude courante (texte cbNom, ou nom saisi pour une
        zone delimitee, nettoye pour servir de prefixe de fichier)."""
        import re
        if hasattr(widget, "cbNiveau") and widget.cbNiveau.currentText() == NIVEAU_DESSIN:
            txt = widget.leNomZone.text().strip() if hasattr(widget, "leNomZone") else ""
            txt = re.sub(r"[^\w\-]+", "_", txt).strip("_")
            return txt or NOM_ZONE_DESSINEE_DEFAUT
        return widget.cbNom.currentText() if hasattr(widget, "cbNom") else ""

    def _zone_dessinee_layer_feature(self):
        layer = getattr(self, "_zone_dessinee_layer", None)
        if layer is None or QgsProject.instance().mapLayer(layer.id()) is None:
            return None, None
        feature = next(layer.getFeatures(), None)
        if feature is None:
            return None, None
        return layer, feature

    def _demarrer_dessin(self, widget, mode):
        from .outil_zone import OutilDelimitationZone
        canvas = self.iface.mapCanvas()
        self._desactiver_outil_dessin()
        self._outil_precedent = canvas.mapTool()
        self._outil_dessin = OutilDelimitationZone(
            canvas, mode, lambda geom, crs: self._on_zone_dessinee(widget, geom, crs)
        )
        canvas.setMapTool(self._outil_dessin)
        if mode == "rectangle":
            consigne = "Cliquez-glissez sur la carte pour tracer le rectangle (Echap pour annuler)."
        else:
            consigne = ("Clic gauche pour ajouter des sommets, clic droit ou Entree pour terminer, "
                        "Retour arriere pour retirer le dernier sommet, Echap pour annuler.")
        self.iface.messageBar().pushInfo("SN Analyser", consigne)

    def _desactiver_outil_dessin(self):
        outil = getattr(self, "_outil_dessin", None)
        if outil is None:
            return
        try:
            canvas = self.iface.mapCanvas()
            if canvas.mapTool() is outil:
                precedent = getattr(self, "_outil_precedent", None)
                if precedent is not None:
                    canvas.setMapTool(precedent)
                else:
                    canvas.unsetMapTool(outil)
        except Exception:
            pass
        self._outil_dessin = None
        self._outil_precedent = None

    def _zone_depuis_selection(self, widget):
        from qgis.core import QgsGeometry
        layer = self.iface.activeLayer()
        highlight = getattr(self, "_zone_highlight_layer", None)
        if (not isinstance(layer, QgsVectorLayer)
                or QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PolygonGeometry
                or (highlight is not None and layer.id() == highlight.id())):
            self.iface.messageBar().pushWarning(
                "SN Analyser", "Activez une couche de polygones dans le panneau des couches et selectionnez-y une ou plusieurs entites."
            )
            return
        geoms = [f.geometry() for f in layer.selectedFeatures() if f.hasGeometry()]
        if not geoms:
            self.iface.messageBar().pushWarning(
                "SN Analyser", "Aucune entite selectionnee dans la couche \u00ab " + layer.name() + " \u00bb."
            )
            return
        geom = QgsGeometry.unaryUnion(geoms)
        if hasattr(widget, "leNomZone") and not widget.leNomZone.text().strip() and len(geoms) == 1:
            nf = _guess_name_field(layer)
            try:
                val = layer.selectedFeatures()[0][nf] if nf else None
            except Exception:
                val = None
            if val:
                widget.leNomZone.setText(str(val))
        self._on_zone_dessinee(widget, geom, layer.crs())

    def _on_zone_dessinee(self, widget, geom, crs):
        """Recoit la zone delimitee : validation, reprojection en EPSG:4326,
        surface, region majoritaire et caractere cotier, puis stockage dans
        une couche memoire (hors panneau des couches) utilisee comme zone
        d etude par toutes les analyses."""
        from qgis.core import QgsGeometry, QgsDistanceArea
        self._desactiver_outil_dessin()
        if geom is None or geom.isEmpty():
            return
        geom = QgsGeometry(geom)
        if not geom.isGeosValid():
            geom = geom.makeValid()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if crs != wgs84:
            geom.transform(QgsCoordinateTransform(crs, wgs84, QgsProject.instance()))
        if geom.isEmpty() or QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.PolygonGeometry:
            self._log_critical("Zone delimitee invalide (geometrie vide ou non surfacique).")
            return

        da = QgsDistanceArea()
        da.setSourceCrs(wgs84, QgsProject.instance().transformContext())
        da.setEllipsoid("WGS84")
        surface_km2 = da.measureArea(geom) / 1e6
        region, cotiere, n_communes = self._contexte_zone_dessinee(geom, wgs84)

        project = QgsProject.instance()
        layer = getattr(self, "_zone_dessinee_layer", None)
        if layer is None or project.mapLayer(layer.id()) is None:
            for existing in list(project.mapLayers().values()):
                if existing.name() == "Zone d\u00e9limit\u00e9e (SN Analyser)":
                    project.removeMapLayer(existing.id())
            layer = QgsVectorLayer(
                "Polygon?crs=EPSG:4326&field=NAME_1:string(100)&field=surface_km2:double",
                "Zone d\u00e9limit\u00e9e (SN Analyser)", "memory",
            )
            project.addMapLayer(layer, False)
            self._zone_dessinee_layer = layer
        provider = layer.dataProvider()
        provider.truncate()
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat["NAME_1"] = region
        feat["surface_km2"] = surface_km2
        provider.addFeatures([feat])
        layer.updateExtents()
        self._zone_dessinee_info = {"region": region, "cotiere": cotiere, "surface_km2": surface_km2}

        self._update_zone_highlight(geom, wgs84)
        self._zoom_to_geometry(geom, wgs84)

        if hasattr(widget, "lblZoneDessinee"):
            texte = "Zone d\u00e9limit\u00e9e : %s km\u00b2" % ("{:,.2f}".format(surface_km2).replace(",", " "))
            if region:
                texte += " \u2014 r\u00e9gion : " + str(region)
            if n_communes:
                texte += " \u2014 %d commune(s) touch\u00e9e(s)" % n_communes
            texte += " \u2014 littorale" if cotiere else " \u2014 non littorale"
            widget.lblZoneDessinee.setText(texte)
        self._maj_disponibilite_erosion(widget)

        if region is None:
            self.iface.messageBar().pushWarning(
                "SN Analyser", "La zone delimitee semble hors du Senegal : certaines donnees de reference ne seront pas disponibles."
            )
        elif surface_km2 > 5000:
            self.iface.messageBar().pushWarning(
                "SN Analyser", "Zone tres etendue (%.0f km\u00b2) : les telechargements et calculs seront longs." % surface_km2
            )
        else:
            self.iface.messageBar().pushSuccess(
                "SN Analyser", "Zone delimitee prete. Cochez les risques puis cliquez sur Analyser."
            )

    def _contexte_zone_dessinee(self, geom, crs):
        """Region majoritaire (en surface), caractere cotier (intersecte une
        commune listee dans data/zones_cotieres.json) et nombre de communes
        touchees par la zone delimitee."""
        from qgis.core import QgsGeometry, QgsFeatureRequest

        def _couche_et_champ(niveau):
            config = NIVEAU_CONFIG[niveau]
            lyr = self._get_reference_layer(config)
            if lyr is None or not lyr.isValid():
                return None, None, None
            champ = config["name_field"]
            if champ not in [f.name() for f in lyr.fields()]:
                champ = _guess_name_field(lyr)
            g = QgsGeometry(geom)
            if lyr.crs() != crs:
                g.transform(QgsCoordinateTransform(crs, lyr.crs(), QgsProject.instance()))
            return lyr, champ, g

        region, meilleure = None, 0.0
        lyr, champ, g = _couche_et_champ("R\u00e9gion")
        if lyr is not None:
            for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(g.boundingBox())):
                if not f.hasGeometry() or not f.geometry().intersects(g):
                    continue
                aire = f.geometry().intersection(g).area()
                if aire > meilleure:
                    meilleure, region = aire, f[champ]

        self._est_zone_cotiere("Commune", "")  # initialise le cache
        communes_cotieres = (_ZONES_COTIERES_CACHE or {}).get("Commune", set())
        cotiere, n_communes = False, 0
        lyr, champ, g = _couche_et_champ("Commune")
        if lyr is not None:
            for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(g.boundingBox())):
                if not f.hasGeometry() or not f.geometry().intersects(g):
                    continue
                n_communes += 1
                if str(f[champ]).strip().lower() in communes_cotieres:
                    cotiere = True
        return (str(region) if region else None), cotiere, n_communes

    # ------------------------------------------------------------------
    # Numerisation : extraction d entites OpenStreetMap sur une zone
    # delimitee (voir core/numerisation.py)
    # ------------------------------------------------------------------
    def _ajouter_controle_numerisation(self, widget):
        from qgis.PyQt.QtWidgets import QGroupBox, QVBoxLayout, QPushButton, QMenu, QLabel
        contenu = getattr(widget, "scrollAreaWidgetContents", None)
        layout = contenu.layout() if contenu is not None else None
        if layout is None:
            return
        gb = QGroupBox("Numérisation")
        v = QVBoxLayout(gb)
        btn = QPushButton("\U0001F5FA️ Numérisation")
        btn.setToolTip("Délimitez une zone puis choisissez les entités à extraire d'OpenStreetMap "
                       "(transport, hydrographie, occupation du sol, limites, réseaux, relief).")
        menu = QMenu(btn)
        menu.addAction("✏️ Dessiner un polygone", lambda: self._numeriser_par_dessin(widget, "polygone"))
        menu.addAction("▭ Tracer un rectangle", lambda: self._numeriser_par_dessin(widget, "rectangle"))
        menu.addAction("\U0001F4CD Utiliser la zone d'étude actuelle", lambda: self._numeriser_zone_etude(widget))
        menu.addAction("\U0001F4D0 Utiliser les entités sélectionnées", lambda: self._numeriser_selection(widget))
        btn.setMenu(menu)
        v.addWidget(btn)
        btn_annuler = QPushButton("\u26d4 Annuler le t\u00e9l\u00e9chargement")
        btn_annuler.setEnabled(False)
        btn_annuler.setToolTip("Interrompt imm\u00e9diatement le t\u00e9l\u00e9chargement en cours")
        btn_annuler.clicked.connect(self._annuler_numerisation)
        v.addWidget(btn_annuler)
        widget.btnAnnulerNumerisation = btn_annuler
        widget.lblEtatNumerisation = QLabel("")
        widget.lblEtatNumerisation.setWordWrap(True)
        v.addWidget(widget.lblEtatNumerisation)
        self._widget_num = widget
        lbl = QLabel("1. Délimitez la zone  2. Cochez les entités  3. Téléchargement automatique (OSM)")
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: gray; font-size: 8pt;")
        v.addWidget(lbl)
        widget.gbNumerisation = gb
        widget.btnNumerisation = btn
        idx = layout.indexOf(widget.gbIndices) if hasattr(widget, "gbIndices") else -1
        if idx >= 0:
            layout.insertWidget(idx + 1, gb)
        else:
            layout.addWidget(gb)

    def _numeriser_par_dessin(self, widget, mode):
        from .outil_zone import OutilDelimitationZone
        canvas = self.iface.mapCanvas()
        self._desactiver_outil_dessin()
        self._outil_precedent = canvas.mapTool()
        self._outil_dessin = OutilDelimitationZone(
            canvas, mode, lambda geom, crs: self._numerisation_zone(widget, geom, crs)
        )
        canvas.setMapTool(self._outil_dessin)
        self.iface.messageBar().pushInfo(
            "SN Analyser - Numérisation",
            "Cliquez-glissez pour tracer le rectangle (Echap pour annuler)." if mode == "rectangle" else
            "Clic gauche : sommets, clic droit ou Entree : terminer, Echap : annuler.",
        )

    def _numeriser_zone_etude(self, widget):
        layer, feature = self.get_selected_zone_feature(widget)
        if feature is None or not feature.hasGeometry():
            self.iface.messageBar().pushWarning("SN Analyser", "Aucune zone d etude selectionnee.")
            return
        nom = self._nom_zone_courante(widget)
        self._numerisation_zone(widget, feature.geometry(), layer.crs(), nom_defaut=nom)

    def _numeriser_selection(self, widget):
        from qgis.core import QgsGeometry
        lyr = self.iface.activeLayer()
        if not isinstance(lyr, QgsVectorLayer) or QgsWkbTypes.geometryType(lyr.wkbType()) != QgsWkbTypes.PolygonGeometry:
            self.iface.messageBar().pushWarning("SN Analyser", "Activez une couche de polygones et selectionnez-y des entites.")
            return
        geoms = [f.geometry() for f in lyr.selectedFeatures() if f.hasGeometry()]
        if not geoms:
            self.iface.messageBar().pushWarning("SN Analyser", "Aucune entite selectionnee dans " + lyr.name() + ".")
            return
        self._numerisation_zone(widget, QgsGeometry.unaryUnion(geoms), lyr.crs())

    def _numerisation_zone(self, widget, geom, crs, nom_defaut=""):
        """Zone delimitee : verification, choix des entites puis lancement
        du telechargement en tache de fond."""
        import re
        import datetime
        from qgis.core import QgsGeometry, QgsDistanceArea, QgsApplication
        self._desactiver_outil_dessin()
        if geom is None or geom.isEmpty():
            return
        geom = QgsGeometry(geom)
        if not geom.isGeosValid():
            geom = geom.makeValid()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if crs != wgs84:
            geom.transform(QgsCoordinateTransform(crs, wgs84, QgsProject.instance()))
        if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.PolygonGeometry:
            self.iface.messageBar().pushWarning("SN Analyser", "La zone de numerisation doit etre une surface.")
            return
        da = QgsDistanceArea()
        da.setSourceCrs(wgs84, QgsProject.instance().transformContext())
        da.setEllipsoid("WGS84")
        aire = da.measureArea(geom) / 1e6
        if aire > 2500:
            self.iface.messageBar().pushCritical(
                "SN Analyser",
                "Zone trop etendue (%.0f km2) : la numerisation OSM est limitee a 2 500 km2. "
                "Delimitez une zone plus petite." % aire)
            return

        choix = self._dialogue_numerisation(aire, nom_defaut)
        if not choix:
            return
        cles, nom = choix
        nom = re.sub(r"[^\w\-]+", "_", nom).strip("_") or "Zone"

        dossier = os.path.join(self.plugin_dir, "temp", "numerisation")
        os.makedirs(dossier, exist_ok=True)
        horodatage = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        prefixe = os.path.join(dossier, nom + "_" + horodatage)
        geojson = prefixe + "_zone.geojson"
        with open(geojson, "w", encoding="utf-8") as f:
            f.write('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},"geometry":'
                    + geom.asJson() + "}]}")
        r = geom.boundingBox()
        bbox = [r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum()]

        tache = _TacheNumerisation(self, cles, bbox, geojson, prefixe, nom, aire)
        if not hasattr(self, "_taches_numerisation"):
            self._taches_numerisation = []
        self._taches_numerisation.append(tache)
        QgsApplication.taskManager().addTask(tache)
        if hasattr(widget, "btnAnnulerNumerisation"):
            widget.btnAnnulerNumerisation.setEnabled(True)
            widget.lblEtatNumerisation.setText("\u23f3 T\u00e9l\u00e9chargement de %s en cours..." % nom)
            tache.progressChanged.connect(
                lambda p, n=nom, w=widget, tc=tache: w.lblEtatNumerisation.setText(
                    "\u23f3 %s : %d %%" % (n, int(p))) if not tc.isCanceled() else None)
        self.iface.messageBar().pushInfo(
            "SN Analyser - Numérisation",
            "Telechargement OpenStreetMap en cours pour %s (%.1f km2, %d type(s)) - suivi dans la barre de taches en bas."
            % (nom, aire, len(cles)))

    def _dialogue_numerisation(self, aire_km2, nom_defaut=""):
        """Fenetre de choix des entites. :return: (cles, nom) ou None."""
        from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
                                         QDialogButtonBox, QLabel, QLineEdit, QPushButton)
        from qgis.PyQt.QtCore import QSettings
        from .core import numerisation as core_num

        dlg = QDialog(self.iface.mainWindow())
        dlg.setWindowTitle("Numérisation")
        dlg.resize(470, 620)
        v = QVBoxLayout(dlg)
        info = QLabel("Zone délimitée : <b>%.2f km²</b>. Cochez les entités à extraire :" % aire_km2)
        v.addWidget(info)
        if aire_km2 > 300:
            alerte = QLabel("⚠️ Zone étendue : le téléchargement peut prendre plusieurs minutes.")
            alerte.setStyleSheet("color: #b35900;")
            v.addWidget(alerte)
        h = QHBoxLayout()
        h.addWidget(QLabel("Nom :"))
        le = QLineEdit(nom_defaut or "Zone_numerisee")
        h.addWidget(le)
        v.addLayout(h)

        reglages = QSettings()
        precedents = reglages.value("sn_analyser/numerisation/cles", "") or ""
        precedents = set(str(precedents).split(",")) if precedents else set()

        arbre = QTreeWidget()
        arbre.setHeaderHidden(True)
        for _cat, libcat, items in core_num.CATALOGUE:
            parent = QTreeWidgetItem(arbre, [libcat])
            parent.setFlags(parent.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            parent.setCheckState(0, Qt.Unchecked)
            for cle, lib, desc, _req, _f in items:
                it = QTreeWidgetItem(parent, [lib + " — " + desc])
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(0, Qt.Checked if cle in precedents else Qt.Unchecked)
                it.setData(0, Qt.UserRole, cle)
                it.setToolTip(0, desc)
            parent.setExpanded(True)
        v.addWidget(arbre)

        hb = QHBoxLayout()
        tout = QPushButton("Tout cocher")
        rien = QPushButton("Tout décocher")

        def _tous(etat):
            for i in range(arbre.topLevelItemCount()):
                arbre.topLevelItem(i).setCheckState(0, etat)
        tout.clicked.connect(lambda: _tous(Qt.Checked))
        rien.clicked.connect(lambda: _tous(Qt.Unchecked))
        hb.addWidget(tout)
        hb.addWidget(rien)
        hb.addStretch()
        v.addLayout(hb)
        note = QLabel("Données © contributeurs OpenStreetMap (ODbL). Courbes de niveau : MNT Copernicus 30 m.")
        note.setStyleSheet("color: gray; font-size: 8pt;")
        note.setWordWrap(True)
        v.addWidget(note)
        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.button(QDialogButtonBox.Ok).setText("Télécharger")
        boutons.accepted.connect(dlg.accept)
        boutons.rejected.connect(dlg.reject)
        v.addWidget(boutons)
        if dlg.exec_() != QDialog.Accepted:
            return None
        cles = []
        for i in range(arbre.topLevelItemCount()):
            p = arbre.topLevelItem(i)
            for j in range(p.childCount()):
                c = p.child(j)
                if c.checkState(0) == Qt.Checked:
                    cles.append(c.data(0, Qt.UserRole))
        if not cles:
            self.iface.messageBar().pushWarning("SN Analyser", "Aucune entite cochee : numerisation annulee.")
            return None
        reglages.setValue("sn_analyser/numerisation/cles", ",".join(cles))
        return cles, le.text().strip()

    def _annuler_numerisation(self):
        for tache in list(getattr(self, "_taches_numerisation", []) or []):
            try:
                tache.cancel()
            except Exception:
                pass
        w = getattr(self, "_widget_num", None)
        if w is not None and hasattr(w, "lblEtatNumerisation"):
            w.lblEtatNumerisation.setText("\u26d4 Annulation en cours...")
            w.btnAnnulerNumerisation.setEnabled(False)

    def _charger_numerisation(self, tache, succes):
        """Charge les couches produites par la tache (fil principal)."""
        from qgis.core import (QgsSymbol, QgsSingleSymbolRenderer, QgsPalLayerSettings, QgsTextFormat,
                               QgsVectorLayerSimpleLabeling)
        from qgis.PyQt.QtGui import QColor
        from .core import numerisation as core_num
        try:
            self._taches_numerisation.remove(tache)
        except Exception:
            pass
        w = getattr(self, "_widget_num", None)
        if w is not None and hasattr(w, "btnAnnulerNumerisation"):
            w.btnAnnulerNumerisation.setEnabled(bool(getattr(self, "_taches_numerisation", [])))
            w.lblEtatNumerisation.setText("")
        if getattr(tache, "annulee", False):
            self.iface.messageBar().pushInfo("SN Analyser - Num\u00e9risation",
                                             "T\u00e9l\u00e9chargement de %s annul\u00e9." % tache.nom)
            return
        if not succes:
            self._log_critical("Numerisation " + tache.nom + " echouee : " + (tache.erreur or "erreur inconnue"))
            return
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        groupe = root.insertGroup(0, "Numérisation - %s" % tache.nom)
        sous_groupes = {}
        libelles_cat = {cat: lib for cat, lib, _items in core_num.CATALOGUE}
        nb_total, vides = 0, []
        produits = {cle for cle, _n, _c, nb in tache.resultats if nb > 0}
        for cle in tache.cles:
            if cle not in produits and cle != "courbes_niveau":
                vides.append(core_num.ITEMS[cle][1])
        ordre = {"points": 0, "lignes": 1, "polygones": 2}
        for cle, nature, couche, nb in sorted(tache.resultats, key=lambda t: (t[0], ordre[t[1]])):
            if nb <= 0:
                continue
            cat, lib = core_num.ITEMS[cle][0], core_num.ITEMS[cle][1]
            if cat not in sous_groupes:
                sous_groupes[cat] = groupe.addGroup(libelles_cat[cat])
            source = "%s|layername=%s" % (tache.gpkg, couche) if couche != "courbes" else tache.courbes_gpkg
            lyr = QgsVectorLayer(source, "%s (%s, %d)" % (lib, nature, nb), "ogr")
            if not lyr.isValid():
                continue
            r, g, b = core_num.COULEURS.get(cle, (100, 100, 100))
            sym = QgsSymbol.defaultSymbol(lyr.geometryType())
            if sym is not None:
                if nature == "polygones":
                    sym.setColor(QColor(r, g, b, 110))
                    try:
                        sym.symbolLayer(0).setStrokeColor(QColor(r, g, b))
                        sym.symbolLayer(0).setStrokeWidth(0.3)
                    except Exception:
                        pass
                elif nature == "lignes":
                    sym.setColor(QColor(r, g, b))
                    sym.setWidth(0.7 if cle in ("cours_eau", "voies_ferrees", "energie") else 0.4)
                    if cle == "frontieres":
                        try:
                            sym.symbolLayer(0).setPenStyle(Qt.DashLine)
                            sym.setWidth(0.6)
                        except Exception:
                            pass
                else:
                    sym.setColor(QColor(r, g, b))
                    sym.setSize(2.4)
                lyr.setRenderer(QgsSingleSymbolRenderer(sym))
            if cle in ("courbes_niveau", "points_cotes"):
                pal = QgsPalLayerSettings()
                pal.fieldName = "altitude" if cle == "courbes_niveau" else 'coalesce("nom", \'\')'
                pal.isExpression = cle != "courbes_niveau"
                if cle == "courbes_niveau":
                    pal.placement = QgsPalLayerSettings.Line
                fmt = QgsTextFormat()
                fmt.setSize(7)
                fmt.setColor(QColor(r, g, b))
                pal.setFormat(fmt)
                lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
                lyr.setLabelsEnabled(True)
            project.addMapLayer(lyr, False)
            sous_groupes[cat].addLayer(lyr)
            nb_total += nb
        msg = "%d entité(s) chargée(s) pour %s (%.0f s). Fichier : %s" % (
            nb_total, tache.nom, tache.duree, os.path.basename(tache.gpkg))
        if vides:
            msg += " | Aucune donnée OSM ici pour : " + ", ".join(vides)
        if tache.avertissements:
            msg += " | " + " ; ".join(tache.avertissements)
        self.iface.messageBar().pushSuccess("SN Analyser - Numérisation", msg)
        try:
            from qgis.core import QgsMessageLog, Qgis
            QgsMessageLog.logMessage(msg, "SN Analyser", level=Qgis.Info)
        except Exception:
            pass
        if not sous_groupes:
            root.removeChildNode(groupe)
        else:
            self._remonter_highlight_au_sommet()

    def _populate_niveau_combo(self, widget):
        """Remplit cbNiveau avec les 3 niveaux administratifs disponibles
        et déclenche le premier remplissage de cbNom.
        """
        if not hasattr(widget, "cbNiveau"):
            return
        widget.cbNiveau.blockSignals(True)
        widget.cbNiveau.clear()
        widget.cbNiveau.addItems(list(NIVEAU_CONFIG.keys()) + [NIVEAU_DESSIN])
        widget.cbNiveau.blockSignals(False)
        self._populate_nom_combo(widget, widget.cbNiveau.currentText())

    def _populate_nom_combo(self, widget, niveau):
        """Remplit cbNom avec la liste triée des noms (régions ou communes)
        correspondant au niveau sélectionné.
        """
        self._basculer_mode_dessin(widget, niveau == NIVEAU_DESSIN)
        if niveau == NIVEAU_DESSIN:
            return
        if not hasattr(widget, "cbNom") or niveau not in NIVEAU_CONFIG:
            return

        config = NIVEAU_CONFIG[niveau]
        layer = self._get_reference_layer(config)
        combo = widget.cbNom
        combo.clear()

        if layer is None or not layer.isValid():
            self.iface.messageBar().pushWarning(
                "SN Analyser",
                f"Couche de référence introuvable pour le niveau « {niveau} ».",
            )
            return

        name_field = config["name_field"]
        if name_field not in [f.name() for f in layer.fields()]:
            # La couche detectee dans le projet n'a pas le champ attendu par
            # defaut (ex: couche personnelle de l'utilisateur) : on tente une
            # detection automatique du champ nom, meme heuristique que Sen
            # Couverture Terrestre (qgis_layers_helper.guess_name_field).
            guessed = _guess_name_field(layer)
            if guessed is None:
                self.iface.messageBar().pushWarning(
                    "SN Analyser",
                    f"Impossible de deviner le champ nom sur la couche '{layer.name()}'.",
                )
                return
            name_field = guessed

        entries = sorted(
            ((feat[name_field], feat.id()) for feat in layer.getFeatures() if feat[name_field]),
            key=lambda t: str(t[0]),
        )
        for nom, fid in entries:
            combo.addItem(str(nom), (layer.id(), fid))

    def _get_reference_layer(self, config):
        """Retourne la couche de référence pour un niveau donné.

        Même logique que Sen Couverture Terrestre : on privilégie une couche
        déjà présente dans le projet plutôt que de recharger un jeu de
        données bundlé, avec repli en cascade :
          1) couche déjà chargée portant exactement le nom attendu
          2) couche polygone déjà chargée dont le nom contient un des
             mots-clés du niveau (région/commune) - détection souple
          3) GeoPackage bundlé avec le plugin (data/limites_administratives.gpkg)
        """
        project = QgsProject.instance()

        exclude_ids = set()
        highlight = getattr(self, "_zone_highlight_layer", None)
        if highlight is not None:
            exclude_ids.add(highlight.id())

        # 1) nom exact configure
        for layer in project.mapLayers().values():
            if layer.id() in exclude_ids:
                continue
            if layer.name() == config["project_name"] and isinstance(layer, QgsVectorLayer):
                self._hide_from_layer_tree(layer)
                return layer

        # 2) detection souple par mot-cle dans le nom de la couche (polygone)
        for layer in project.mapLayers().values():
            if layer.id() in exclude_ids:
                continue
            if not isinstance(layer, QgsVectorLayer):
                continue
            if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
                continue
            lname = layer.name().lower()
            if any(kw in lname for kw in config.get("keywords", [])):
                self._hide_from_layer_tree(layer)
                return layer

        # 3) repli : GeoPackage bundle avec le plugin
        if not os.path.exists(REFERENCE_GPKG):
            return None

        uri = f"{REFERENCE_GPKG}|layername={config['gpkg_layer']}"
        layer = QgsVectorLayer(uri, config["project_name"], "ogr")
        if layer.isValid():
            # addToLegend=False : la couche reste accessible via
            # QgsProject.mapLayer() (utilise par get_selected_zone_feature)
            # mais n'apparait pas dans le panneau des couches. Seule la
            # limite de la zone selectionnee doit etre visible (cf.
            # _update_zone_highlight).
            project.addMapLayer(layer, False)
            return layer
        return None

    def _hide_from_layer_tree(self, layer):
        """Masque completement une couche du panneau des couches (deja
        presente dans le projet AVANT le lancement du plugin, ex: couche
        d'un autre plugin comme Sen Couverture Terrestre) : decochee ET
        marquee "Private" pour qu'elle n'apparaisse plus du tout dans la
        liste du panneau (pas juste grisee). Ne retire rien du projet ni de
        l'arbre : la couche reste utilisable par le plugin d'origine.
        """
        if layer is None:
            return
        layer.setFlags(layer.flags() | QgsMapLayer.Private)
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(layer.id())
        if node is not None:
            node.setItemVisibilityChecked(False)

    def get_selected_zone_feature(self, widget):
        """Retourne (QgsVectorLayer, QgsFeature) pour la zone d'étude
        actuellement sélectionnée (niveau + nom), ou (None, None).
        """
        if hasattr(widget, "cbNiveau") and widget.cbNiveau.currentText() == NIVEAU_DESSIN:
            return self._zone_dessinee_layer_feature()
        if not hasattr(widget, "cbNom"):
            return None, None
        data = widget.cbNom.currentData()
        if not data:
            return None, None
        layer_id, feat_id = data
        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is None:
            return None, None
        feature = layer.getFeature(feat_id)
        return layer, feature

    def _on_nom_selected(self, widget):
        """Zone (niveau + nom) selectionnee dans cbNom : affiche uniquement
        la limite de cette zone sur le canevas et zoome dessus. La couche de
        reference complete (regions/communes) ne s'affiche jamais dans le
        panneau des couches (cf. _get_reference_layer, addToLegend=False).
        """
        layer, feature = self.get_selected_zone_feature(widget)
        if layer is None or feature is None or not feature.hasGeometry():
            return
        self._update_zone_highlight(feature.geometry(), layer.crs())
        self._zoom_to_geometry(feature.geometry(), layer.crs())

    def _update_zone_highlight(self, geometry, crs):
        """Affiche la limite de la zone selectionnee (region/commune) sur le
        canevas : symbole unique, remplissage simple transparent, ligne en
        tirets, largeur de trait 0.26 mm. Reutilise la meme couche memoire
        d'un appel a l'autre (une seule entite a la fois).
        """
        project = QgsProject.instance()
        layer = getattr(self, "_zone_highlight_layer", None)
        if layer is None or project.mapLayer(layer.id()) is None:
            # Securite anti-doublon : si un rechargement du plugin (dev) a
            # laisse une ancienne couche de surbrillance orpheline dans le
            # projet, on la retire avant d'en recreer une.
            for existing in list(project.mapLayers().values()):
                if existing.name() == "Limite sélectionnée":
                    project.removeMapLayer(existing.id())
            layer = QgsVectorLayer(f"Polygon?crs={crs.authid()}", "Limite sélectionnée", "memory")

            symbol = QgsFillSymbol()
            fill_symbol_layer = QgsSimpleFillSymbolLayer()
            fill_symbol_layer.setBrushStyle(Qt.NoBrush)      # remplissage transparent
            fill_symbol_layer.setStrokeStyle(Qt.DashLine)    # ligne en tirets
            fill_symbol_layer.setStrokeWidth(0.26)           # largeur de trait 0.26 mm
            symbol.changeSymbolLayer(0, fill_symbol_layer)
            layer.renderer().setSymbol(symbol)

            project.addMapLayer(layer)
            # Private : la couche continue de s'afficher sur le canevas
            # (elle reste cochee dans l'arbre) mais disparait du panneau des
            # couches, comme demande.
            layer.setFlags(layer.flags() | QgsMapLayer.Private)
            self._zone_highlight_layer = layer

        # Reprojection si la geometrie n est pas dans le SCR de la couche de
        # surbrillance (ex: zone dessinee en EPSG:4326 vs couche communes).
        if crs.isValid() and layer.crs().isValid() and layer.crs() != crs:
            geometry = type(geometry)(geometry)
            geometry.transform(QgsCoordinateTransform(crs, layer.crs(), project))

        provider = layer.dataProvider()
        provider.truncate()
        feat = QgsFeature()
        feat.setGeometry(geometry)
        provider.addFeature(feat)
        layer.updateExtents()
        layer.triggerRepaint()

    def _zoom_to_geometry(self, geometry, crs, margin_fraction=0.15):
        """Zoome le canevas sur la geometrie donnee (reprojection si besoin)."""
        canvas = self.iface.mapCanvas()
        geom = geometry
        canvas_crs = canvas.mapSettings().destinationCrs()
        if crs != canvas_crs:
            geom = type(geometry)(geometry)  # copie
            transform = QgsCoordinateTransform(crs, canvas_crs, QgsProject.instance())
            geom.transform(transform)
        rect = geom.boundingBox()
        rect.scale(1 + margin_fraction)
        canvas.setExtent(rect)
        canvas.refresh()

    def _on_analyser(self, widget):
        # Garde anti-reentrance : les pipelines d analyse appellent _pulse()
        # (QApplication.processEvents()) a plusieurs reprises pendant les
        # telechargements (1-2 min chacun). Sans cette garde, un second clic
        # sur "Analyser" pendant ce delai (l utilisateur pense l interface
        # bloquee) relance une analyse COMPLETE en parallele (reentrance Qt),
        # qui ecrit sur les MEMES fichiers (meme zone/annee) que l analyse en
        # cours : la derniere ecriture gagne et peut laisser un resultat
        # corrompu/tronque pour un des modules (observe empiriquement sur la
        # vegetation, qui s execute au milieu de la sequence urbanisation ->
        # environnement -> erosion).
        if getattr(self, "_analyse_en_cours", False):
            self.iface.messageBar().pushWarning(
                "SN Analyser", "Une analyse est deja en cours, merci de patienter (1 a 2 minutes par indice)."
            )
            return

        layer, feature = self.get_selected_zone_feature(widget)
        if feature is None:
            self.iface.messageBar().pushWarning(
                "SN Analyser", "Veuillez selectionner une zone d etude (niveau + nom) ou delimiter une zone sur la carte."
            )
            return

        niveau = widget.cbNiveau.currentText() if hasattr(widget, "cbNiveau") else ""
        nom = self._nom_zone_courante(widget)

        from .core.ire import RISQUES_CONFIG
        RISQUE_CHECKBOX = {
            "climatique": "chkRisqueClimatique",
            "pollution": "chkRisquePollution",
            "degradation_terres": "chkRisqueDegradationTerres",
            "hydrologique": "chkRisqueHydrologique",
            "urbanisation": "chkRisqueUrbanisation",
            "biodiversite": "chkRisqueBiodiversite",
        }
        risques_demandes = [
            cle for cle, attr in RISQUE_CHECKBOX.items()
            if hasattr(widget, attr) and getattr(widget, attr).isChecked()
        ]

        if not risques_demandes:
            self.iface.messageBar().pushInfo(
                "SN Analyser",
                "Zone selectionnee = " + niveau + " " + nom + ". "
                "Cochez au moins un risque (Climatique / Pollution / Degradation des terres / "
                "Hydrologique / Urbanisation / Biodiversite) pour lancer l analyse.",
            )
            return

        self._analyse_en_cours = True
        if hasattr(widget, "btnAnalyser"):
            widget.btnAnalyser.setEnabled(False)
            texte_original_bouton = widget.btnAnalyser.text()
            widget.btnAnalyser.setText("Analyse en cours...")
        else:
            texte_original_bouton = None

        try:
            resultats_combines = self._run_risques_analysis(widget, layer, feature, niveau, nom, risques_demandes)
            if resultats_combines:
                self._afficher_commentaire(widget, niveau, nom, resultats_combines)
        finally:
            self._analyse_en_cours = False
            if hasattr(widget, "btnAnalyser"):
                widget.btnAnalyser.setEnabled(True)
                if texte_original_bouton is not None:
                    widget.btnAnalyser.setText(texte_original_bouton)

    def _afficher_commentaire(self, widget, niveau, nom, resultats_combines):
        """Genere le commentaire de synthese (core/commentaire_ia.py) a partir
        des resultats combines des modules lances, et l affiche a l utilisateur
        (panneau de log QGIS, non bloquant, persiste dans l historique).
        """
        from qgis.core import Qgis, QgsMessageLog
        from .core import commentaire_ia

        annee_fin = widget.spinAnneeFin.value() if hasattr(widget, "spinAnneeFin") else 2024
        annee_debut = widget.spinAnneeDebut.value() if hasattr(widget, "spinAnneeDebut") else annee_fin

        try:
            commentaire = commentaire_ia.generer_commentaire(nom, annee_debut, annee_fin, resultats_combines)
        except Exception as e:
            commentaire = "Commentaire non disponible (" + str(e) + ")."

        QgsMessageLog.logMessage(commentaire, "SN Analyser", level=Qgis.Info)
        self.iface.messageBar().pushInfo(
            "SN Analyser",
            "Synthese generee - voir le panneau Messages (View > Panels > Log Messages, onglet SN Analyser). "
            "Export PDF/Word disponible.",
        )

        self.dernier_resultat = {
            "nom": nom,
            "annee_debut": annee_debut,
            "annee_fin": annee_fin,
            "resultats": resultats_combines,
            "commentaire": commentaire,
        }

        return commentaire

    def _appliquer_symbologie_classification(self, raster_layer, couleur_hex, libelle_actif, libelle_inactif="Non classe"):
        """Applique une symbologie paletee lisible a un raster classe (0/1) :
        fond transparent pour la classe 0, couleur pleine pour la classe 1.
        Rend les cartes de resultats immediatement comprehensibles (bati en
        rouge, vegetation en vert, eau en bleu) au lieu du degrade de gris
        par defaut de QGIS.
        """
        from qgis.core import QgsPalettedRasterRenderer
        from qgis.PyQt.QtGui import QColor

        classes = [
            QgsPalettedRasterRenderer.Class(0, QColor(0, 0, 0, 0), libelle_inactif),
            QgsPalettedRasterRenderer.Class(1, QColor(couleur_hex), libelle_actif),
        ]
        renderer = QgsPalettedRasterRenderer(raster_layer.dataProvider(), 1, classes)
        raster_layer.setRenderer(renderer)
        raster_layer.setOpacity(0.85)
        raster_layer.triggerRepaint()
        self.iface.layerTreeView().refreshLayerSymbology(raster_layer.id())

    def _on_export(self, widget, format_export):
        """Exporte le dernier resultat d analyse au format demande (pdf/word)."""
        from qgis.PyQt.QtWidgets import QFileDialog

        if not self.dernier_resultat:
            self.iface.messageBar().pushWarning(
                "SN Analyser", "Aucune analyse a exporter. Lancez d abord une analyse (bouton Analyser)."
            )
            return

        ext_par_format = {"pdf": "pdf", "word": "docx"}
        filtre_par_format = {
            "pdf": "Fichiers PDF (*.pdf)",
            "word": "Documents Word (*.docx)",
        }
        ext = ext_par_format[format_export]
        nom_fichier_defaut = (
            "SnAnalyser_" + self.dernier_resultat["nom"].replace(" ", "_") + "_"
            + (str(self.dernier_resultat["annee_fin"]) if self.dernier_resultat["annee_debut"] == self.dernier_resultat["annee_fin"] else str(self.dernier_resultat["annee_debut"]) + "-" + str(self.dernier_resultat["annee_fin"]))
            + "." + ext
        )

        chemin, _ = QFileDialog.getSaveFileName(
            widget, "Exporter le rapport", nom_fichier_defaut, filtre_par_format[format_export]
        )
        if not chemin:
            return

        try:
            if format_export == "pdf":
                from .core import export_pdf
                export_pdf.generer_rapport_pdf(
                    self.dernier_resultat["nom"], self.dernier_resultat["annee_debut"],
                    self.dernier_resultat["annee_fin"], self.dernier_resultat["resultats"],
                    self.dernier_resultat["commentaire"], chemin,
                )
            elif format_export == "word":
                from .core import export_word
                export_word.generer_rapport_word(
                    self.dernier_resultat["nom"], self.dernier_resultat["annee_debut"],
                    self.dernier_resultat["annee_fin"], self.dernier_resultat["resultats"],
                    self.dernier_resultat["commentaire"], chemin,
                )
        except Exception as e:
            self._log_critical("Export " + format_export + " echoue : " + str(e))
            return

        self.iface.messageBar().pushSuccess("SN Analyser", "Rapport exporte : " + chemin)

    def _run_urbanisation_analysis(self, widget, layer, feature, niveau, nom, seuil_manuel=None):
        """Pipeline urbanisation : pour l annee de fin (et l annee de debut si
        differente), recherche + telechargement Landsat, calcul NDBI,
        classification bati avec un seuil Otsu UNIQUE partage entre les
        annees comparees (indispensable pour une comparaison multi-temporelle
        valide, voir _telecharger_ndbi_annee/_classifier_urbanisation_annee),
        decoupage a la zone d etude, calcul de surface. Calcule le taux de
        croissance annuel si deux annees.
        """
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsGeometry,
            QgsDistanceArea,
        )
        from .core import utils as core_utils
        from .core import urbanisation as core_urb

        self.iface.messageBar().pushInfo("SN Analyser", "Analyse Urbanisation lancee pour " + nom + "...")

        geom = QgsGeometry(feature.geometry())
        src_crs = layer.crs()
        dst_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        if src_crs != dst_crs:
            transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
            geom.transform(transform)

        bbox_rect = geom.boundingBox()
        bbox = [bbox_rect.xMinimum(), bbox_rect.yMinimum(), bbox_rect.xMaximum(), bbox_rect.yMaximum()]

        annee_fin = widget.spinAnneeFin.value() if hasattr(widget, "spinAnneeFin") else 2024
        annee_debut = widget.spinAnneeDebut.value() if hasattr(widget, "spinAnneeDebut") else annee_fin

        da = QgsDistanceArea()
        da.setEllipsoid("WGS84")
        surface_totale_ha = da.measureArea(geom) / 10000.0

        work_dir = core_utils.get_temp_dir(self.plugin_dir)

        annees_a_traiter = [annee_fin] if annee_debut == annee_fin else [annee_debut, annee_fin]

        # Etape 1 : recherche + telechargement + calcul NDBI pour chaque annee
        # (SANS classifier tout de suite : il faut le NDBI de toutes les
        # annees comparees pour calculer un seuil Otsu commun).
        # On commence par l annee de fin (archive Landsat generalement plus
        # dense recemment), puis on recherche l annee de debut en priorisant
        # le MEME mois : comparer une scene de saison des pluies a une scene
        # de saison seche confond la saisonnalite avec un veritable
        # changement inter-annuel (particulierement pour NDVI/NDWI, mais NDBI
        # peut aussi varier avec l humidite du sol).
        ordre = [annee_fin] + [a for a in annees_a_traiter if a != annee_fin]
        prepares = {}
        for annee in ordre:
            # path_row_cible=None : le composite annuel couvre lui-meme tous
            # les couloirs satellite necessaires (plus besoin de forcer un
            # path/row unique herite d une annee a l autre comme avec l
            # ancienne approche a une seule scene).
            prep = self._telecharger_ndbi_annee(bbox, nom, annee, work_dir)
            if prep is None:
                return None
            prepares[annee] = prep

        # Etape 2 : seuil Otsu UNIQUE, calcule sur l ensemble des NDBI des
        # annees comparees. Un seuil different par date (calcule
        # independamment sur chaque scene) ferait deriver la definition meme
        # de "bati" dans le temps et peut a lui seul produire une fausse
        # hausse/baisse de surface sans lien avec un changement reel.
        if seuil_manuel is not None:
            seuil_global = seuil_manuel
        elif len(annees_a_traiter) > 1:
            try:
                seuil_global = core_urb.calculer_seuil_otsu_multi(
                    [prepares[a]["index_path"] for a in annees_a_traiter]
                )
            except Exception as e:
                self._log_critical("Calcul du seuil NDBI commun echoue : " + str(e))
                return None
        else:
            seuil_global = None  # une seule date : seuil auto sur cette scene, pas de probleme de comparabilite

        # Etape 3 : classification (seuil commun) + decoupage + surface pour
        # chaque annee.
        resultats = {}
        for annee in annees_a_traiter:
            res = self._classifier_urbanisation_annee(prepares[annee], geom, nom, annee, work_dir, seuil_global)
            if res is None:
                return None
            resultats[annee] = res

        res_fin = resultats[annee_fin]
        pct_fin = (res_fin["surface_ha"] / surface_totale_ha * 100) if surface_totale_ha > 0 else 0

        message = (
            "Urbanisation " + nom + " (" + str(annee_fin) + ") : %.1f ha batis sur %.1f ha (%.1f pourcent, seuil NDBI=%.3f)."
            % (res_fin["surface_ha"], surface_totale_ha, pct_fin, res_fin.get("seuil_ndbi", 0.0))
        )

        if annee_debut != annee_fin:
            res_debut = resultats[annee_debut]
            nb_annees = annee_fin - annee_debut
            try:
                taux = core_urb.taux_croissance_annuel(
                    res_debut["surface_ha"], res_fin["surface_ha"], nb_annees
                )
                message += (
                    " Entre " + str(annee_debut) + " (%.1f ha) et " % res_debut["surface_ha"]
                    + str(annee_fin) + ", croissance annuelle moyenne : %.1f pourcent/an." % taux
                )
            except Exception as e:
                message += " (taux de croissance non calculable : " + str(e) + ")"

        self.iface.messageBar().pushSuccess("SN Analyser", message)

        resume = {
            "surface_urbanisee_debut": resultats[annee_debut]["surface_ha"],
            "surface_urbanisee_fin": res_fin["surface_ha"],
        }
        if annee_debut != annee_fin:
            try:
                resume["taux_croissance_urbaine"] = core_urb.taux_croissance_annuel(
                    resultats[annee_debut]["surface_ha"], res_fin["surface_ha"], annee_fin - annee_debut
                )
            except Exception:
                pass

        # Nettoyage des fichiers intermediaires volumineux (on garde les rasters classes decoupes)
        for annee, res in resultats.items():
            for key in ("extrait_path", "ndbi_path", "classe_path"):
                p = res.get(key)
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

        return resume

    def _telecharger_ndbi_annee(self, bbox, nom, annee, work_dir, path_row_cible=None):
        """Construit un composite annuel sans nuages (multi-scenes, voir
        core/landsat.py:telecharger_composite_annuel) et calcule le NDBI pour
        UNE annee donnee. Ne classifie PAS : le seuil Otsu commun doit d abord
        etre calcule sur l ensemble des annees comparees (voir
        _run_urbanisation_analysis).

        Remplace l ancienne approche a une seule scene : le composite couvre
        naturellement toute la zone meme si elle chevauche plusieurs couloirs
        satellite (path/row), et lisse le bruit d une scene isolee.

        :return: dict {composite, prefix, extrait_path, index_path} ou None si echec.
        """
        from .core import landsat as core_landsat
        from .core import ndbi as core_ndbi

        prefix = nom.replace(" ", "_") + "_" + str(annee)
        extrait_path = os.path.join(work_dir, prefix + "_nir_swir1.tif")

        try:
            composite = core_landsat.telecharger_composite_annuel(
                bbox, annee, ["nir", "swir1"], extrait_path,
                path_row_cible=path_row_cible, nuages_max=70, max_scenes=8,
            )
        except Exception as e:
            self._log_critical("Composite Landsat echoue (" + str(annee) + ") : " + str(e))
            return None

        if composite["nb_scenes_utilisees"] < composite["nb_scenes_demandees"]:
            self.iface.messageBar().pushWarning(
                "SN Analyser",
                "Composite " + str(annee) + " : " + str(composite["nb_scenes_utilisees"]) + "/"
                + str(composite["nb_scenes_demandees"]) + " scenes utilisees ("
                + "{:.0f}".format(composite["couverture_pct"]) + "% de couverture). "
                + "Voir la console Python QGIS pour le detail des scenes ignorees.",
            )
        self._pulse()

        ndbi_path = os.path.join(work_dir, prefix + "_ndbi.tif")
        try:
            core_ndbi.calculer_ndbi_depuis_extrait(extrait_path, ndbi_path, idx_nir=1, idx_swir=2)
        except Exception as e:
            self._log_critical("Calcul NDBI echoue (" + str(annee) + ") : " + str(e))
            return None

        return {
            "composite": composite,
            "prefix": prefix,
            "extrait_path": extrait_path,
            "index_path": ndbi_path,
        }

    def _classifier_urbanisation_annee(self, prep, geom, nom, annee, work_dir, seuil):
        """Classifie le NDBI prepare par _telecharger_ndbi_annee (seuil Otsu
        commun aux annees comparees, ou None pour un seuil auto sur cette
        seule scene), decoupe a la zone d etude, calcule la surface batie et
        ajoute la couche a la carte.

        :return: dict {surface_ha, scene_id, cloud_cover, seuil_ndbi, ...} ou
            None si une etape a echoue.
        """
        from qgis.core import QgsRasterLayer
        from .core import urbanisation as core_urb
        from .core import utils as core_utils

        composite = prep["composite"]
        prefix = prep["prefix"]
        ndbi_path = prep["index_path"]

        classe_path = os.path.join(work_dir, prefix + "_bati.tif")
        classe_clip_path = os.path.join(work_dir, prefix + "_bati_clip.tif")

        try:
            _, seuil_utilise = core_urb.classifier_bati(ndbi_path, classe_path, seuil=seuil)
        except Exception as e:
            self._log_critical("Classification echouee (" + str(annee) + ") : " + str(e))
            return None

        geojson_path = os.path.join(work_dir, prefix + "_zone.geojson")
        # Ecriture directe du GeoJSON par concatenation de chaine, SANS repasser
        # par json.loads(geom.asJson()) : ce round-trip declenchait un crash memoire
        # (access violation) lie au ramasse-miettes Python interferant avec des
        # objets C++ QGIS encore en cours d utilisation.
        geometry_json_str = geom.asJson()
        feature_collection_str = (
            '{"type": "FeatureCollection", "features": [{"type": "Feature", '
            '"properties": {}, "geometry": ' + geometry_json_str + "}]}"
        )
        with open(geojson_path, "w", encoding="utf-8") as f:
            f.write(feature_collection_str)

        try:
            core_utils.clip_raster_to_geometry(classe_path, geojson_path, classe_clip_path, nodata=255)
            surface_ha = core_urb.surface_urbanisee(classe_clip_path)
        except Exception as e:
            self._log_critical("Decoupage/calcul de surface echoue (" + str(annee) + ") : " + str(e))
            return None

        self._pulse()
        raster_layer = QgsRasterLayer(classe_clip_path, "Urbanisation " + nom + " " + str(annee))
        if raster_layer.isValid():
            QgsProject.instance().addMapLayer(raster_layer)
            self._appliquer_symbologie_classification(raster_layer, "#E31A1C", "Bati", "Non bati")

        return {
            "surface_ha": surface_ha,
            "nb_scenes": composite["nb_scenes_utilisees"],
            "couverture_pct": composite["couverture_pct"],
            "seuil_ndbi": seuil_utilise,
            "extrait_path": prep["extrait_path"],
            "ndbi_path": ndbi_path,
            "classe_path": classe_path,
            "classe_clip_path": classe_clip_path,
        }


    def _run_environnement_analysis(self, widget, layer, feature, niveau, nom, seuil_manuel=None):
        """Pipeline environnement (vegetation/NDVI) : pour l annee de fin (et
        l annee de debut si differente), calcule la surface vegetalisee et
        detecte une eventuelle degradation (perte de vegetation) entre les
        deux dates. Utilise un seuil Otsu UNIQUE partage entre les annees
        comparees (voir _telecharger_ndvi_annee/_classifier_environnement_annee) :
        indispensable pour une comparaison multi-temporelle valide, sinon un
        seuil different par date peut a lui seul produire une fausse
        deforestation/degradation qui ne reflete rien de reel.
        """
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsGeometry,
            QgsDistanceArea,
        )
        from .core import utils as core_utils
        from .core import environnement as core_env

        self.iface.messageBar().pushInfo("SN Analyser", "Analyse Environnement (NDVI) lancee pour " + nom + "...")

        geom = QgsGeometry(feature.geometry())
        src_crs = layer.crs()
        dst_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        if src_crs != dst_crs:
            transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
            geom.transform(transform)

        bbox_rect = geom.boundingBox()
        bbox = [bbox_rect.xMinimum(), bbox_rect.yMinimum(), bbox_rect.xMaximum(), bbox_rect.yMaximum()]

        annee_fin = widget.spinAnneeFin.value() if hasattr(widget, "spinAnneeFin") else 2024
        annee_debut = widget.spinAnneeDebut.value() if hasattr(widget, "spinAnneeDebut") else annee_fin

        da = QgsDistanceArea()
        da.setEllipsoid("WGS84")
        surface_totale_ha = da.measureArea(geom) / 10000.0

        work_dir = core_utils.get_temp_dir(self.plugin_dir)

        annees_a_traiter = [annee_fin] if annee_debut == annee_fin else [annee_debut, annee_fin]

        # Etape 1 : recherche + telechargement + calcul NDVI pour chaque annee
        # (SANS classifier tout de suite : il faut le NDVI de toutes les
        # annees comparees pour calculer un seuil Otsu commun).
        # Meme logique que pour l urbanisation : on cale l annee de debut sur
        # le mois de l annee de fin pour eviter de confondre saisonnalite
        # (vegetation tres sensible a la saison au Senegal) et changement reel.
        ordre = [annee_fin] + [a for a in annees_a_traiter if a != annee_fin]
        prepares = {}
        for annee in ordre:
            # path_row_cible=None : le composite annuel couvre lui-meme tous
            # les couloirs satellite necessaires pour chaque annee.
            prep = self._telecharger_ndvi_annee(bbox, nom, annee, work_dir)
            if prep is None:
                return None
            prepares[annee] = prep

        # Etape 2 : seuil Otsu UNIQUE, calcule sur l ensemble des NDVI des
        # annees comparees (meme raisonnement que pour l urbanisation : un
        # seuil independant par date fausserait la comparaison).
        if seuil_manuel is not None:
            seuil_global = seuil_manuel
        elif len(annees_a_traiter) > 1:
            try:
                seuil_global = core_env.calculer_seuil_otsu_multi(
                    [prepares[a]["index_path"] for a in annees_a_traiter]
                )
            except Exception as e:
                self._log_critical("Calcul du seuil NDVI commun echoue : " + str(e))
                return None
        else:
            seuil_global = None  # une seule date : seuil auto sur cette scene, pas de probleme de comparabilite

        # Etape 3 : classification (seuil commun) + decoupage + surface pour
        # chaque annee.
        resultats = {}
        for annee in annees_a_traiter:
            res = self._classifier_environnement_annee(prepares[annee], geom, nom, annee, work_dir, seuil_global)
            if res is None:
                return None
            resultats[annee] = res

        res_fin = resultats[annee_fin]
        pct_fin = (res_fin["surface_ha"] / surface_totale_ha * 100) if surface_totale_ha > 0 else 0

        message = (
            "Vegetation " + nom + " (" + str(annee_fin) + ") : %.1f ha vegetalisees sur %.1f ha (%.1f pourcent, seuil NDVI=%.3f)."
            % (res_fin["surface_ha"], surface_totale_ha, pct_fin, res_fin.get("seuil_ndvi", 0.0))
        )

        if annee_debut != annee_fin:
            res_debut = resultats[annee_debut]
            diag = core_env.detecter_deforestation(res_debut["surface_ha"], res_fin["surface_ha"])
            sens = "perte" if diag["degradation"] else "gain"
            message += (
                " Entre " + str(annee_debut) + " (%.1f ha) et " % res_debut["surface_ha"]
                + str(annee_fin) + " : %s de %.1f ha (%.1f pourcent)." % (sens, abs(diag["variation_ha"]), abs(diag["variation_pct"]))
            )

        self.iface.messageBar().pushSuccess("SN Analyser", message)

        resume = {
            "surface_vegetation_debut": resultats[annee_debut]["surface_ha"],
            "surface_vegetation_fin": res_fin["surface_ha"],
        }

        for annee, res in resultats.items():
            for key in ("extrait_path", "ndvi_path", "classe_path"):
                p = res.get(key)
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

        return resume

    def _telecharger_ndvi_annee(self, bbox, nom, annee, work_dir, path_row_cible=None):
        """Construit un composite annuel sans nuages (multi-scenes, voir
        core/landsat.py:telecharger_composite_annuel) et calcule le NDVI pour
        UNE annee donnee. Ne classifie PAS : le seuil Otsu commun doit d abord
        etre calcule sur l ensemble des annees comparees (voir
        _run_environnement_analysis).

        :return: dict {composite, prefix, extrait_path, index_path} ou None si echec.
        """
        from .core import landsat as core_landsat
        from .core import ndvi as core_ndvi

        prefix = nom.replace(" ", "_") + "_" + str(annee)
        extrait_path = os.path.join(work_dir, prefix + "_nir_red.tif")

        try:
            composite = core_landsat.telecharger_composite_annuel(
                bbox, annee, ["nir", "red"], extrait_path,
                path_row_cible=path_row_cible, nuages_max=70, max_scenes=8,
            )
        except Exception as e:
            self._log_critical("Composite Landsat echoue (" + str(annee) + ") : " + str(e))
            return None

        if composite["nb_scenes_utilisees"] < composite["nb_scenes_demandees"]:
            self.iface.messageBar().pushWarning(
                "SN Analyser",
                "Composite " + str(annee) + " : " + str(composite["nb_scenes_utilisees"]) + "/"
                + str(composite["nb_scenes_demandees"]) + " scenes utilisees ("
                + "{:.0f}".format(composite["couverture_pct"]) + "% de couverture). "
                + "Voir la console Python QGIS pour le detail des scenes ignorees.",
            )
        self._pulse()

        ndvi_path = os.path.join(work_dir, prefix + "_ndvi.tif")
        try:
            core_ndvi.calculer_ndvi_depuis_extrait(extrait_path, ndvi_path, idx_nir=1, idx_red=2)
        except Exception as e:
            self._log_critical("Calcul NDVI echoue (" + str(annee) + ") : " + str(e))
            return None

        return {
            "composite": composite,
            "prefix": prefix,
            "extrait_path": extrait_path,
            "index_path": ndvi_path,
        }

    def _classifier_environnement_annee(self, prep, geom, nom, annee, work_dir, seuil):
        """Classifie le NDVI prepare par _telecharger_ndvi_annee (seuil Otsu
        commun aux annees comparees, ou None pour un seuil auto sur cette
        seule scene), decoupe a la zone d etude, calcule la surface
        vegetalisee et ajoute la couche a la carte.

        :return: dict {surface_ha, scene_id, cloud_cover, seuil_ndvi, ...} ou
            None si une etape a echoue.
        """
        from qgis.core import QgsRasterLayer
        from .core import environnement as core_env
        from .core import utils as core_utils

        composite = prep["composite"]
        prefix = prep["prefix"]
        ndvi_path = prep["index_path"]

        classe_path = os.path.join(work_dir, prefix + "_veg.tif")
        classe_clip_path = os.path.join(work_dir, prefix + "_veg_clip.tif")

        try:
            _, seuil_utilise = core_env.classifier_vegetation(ndvi_path, classe_path, seuil=seuil)
        except Exception as e:
            self._log_critical("Classification echouee (" + str(annee) + ") : " + str(e))
            return None

        geojson_path = os.path.join(work_dir, prefix + "_zone_env.geojson")
        # Ecriture directe du GeoJSON par concatenation de chaine, SANS repasser
        # par json.loads(geom.asJson()) : ce round-trip declenchait un crash memoire
        # (access violation) lie au ramasse-miettes Python interferant avec des
        # objets C++ QGIS encore en cours d utilisation.
        geometry_json_str = geom.asJson()
        feature_collection_str = (
            '{"type": "FeatureCollection", "features": [{"type": "Feature", '
            '"properties": {}, "geometry": ' + geometry_json_str + "}]}"
        )
        with open(geojson_path, "w", encoding="utf-8") as f:
            f.write(feature_collection_str)

        try:
            core_utils.clip_raster_to_geometry(classe_path, geojson_path, classe_clip_path, nodata=255)
            surface_ha = core_env.surface_vegetation(classe_clip_path)
        except Exception as e:
            self._log_critical("Decoupage/calcul de surface echoue (" + str(annee) + ") : " + str(e))
            return None

        self._pulse()
        raster_layer = QgsRasterLayer(classe_clip_path, "Vegetation " + nom + " " + str(annee))
        if raster_layer.isValid():
            QgsProject.instance().addMapLayer(raster_layer)
            self._appliquer_symbologie_classification(raster_layer, "#33A02C", "Vegetation", "Non vegetalise")

        return {
            "surface_ha": surface_ha,
            "nb_scenes": composite["nb_scenes_utilisees"],
            "couverture_pct": composite["couverture_pct"],
            "seuil_ndvi": seuil_utilise,
            "extrait_path": prep["extrait_path"],
            "ndvi_path": ndvi_path,
            "classe_path": classe_path,
            "classe_clip_path": classe_clip_path,
        }


    def _run_erosion_analysis(self, widget, layer, feature, niveau, nom, seuil_manuel=None):
        """Pipeline erosion cotiere : classification eau/terre (NDWI) pour
        chaque annee avec un seuil Otsu UNIQUE partage entre les deux dates
        (voir _telecharger_ndwi_annee/_classifier_erosion_annee -- meme
        raisonnement que pour l urbanisation/l environnement : un seuil
        different par date fausserait la comparaison du trait de cote), puis
        comparaison de surfaces entre les deux dates (erosion = gain de
        surface en eau, accretion = perte de surface en eau).
        """
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsGeometry,
        )
        from .core import utils as core_utils
        from .core import erosion as core_ero

        self.iface.messageBar().pushInfo("SN Analyser", "Analyse Erosion cotiere lancee pour " + nom + "...")

        geom = QgsGeometry(feature.geometry())
        src_crs = layer.crs()
        dst_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        if src_crs != dst_crs:
            transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
            geom.transform(transform)

        bbox_rect = geom.boundingBox()
        bbox = [bbox_rect.xMinimum(), bbox_rect.yMinimum(), bbox_rect.xMaximum(), bbox_rect.yMaximum()]

        annee_fin = widget.spinAnneeFin.value() if hasattr(widget, "spinAnneeFin") else 2024
        annee_debut = widget.spinAnneeDebut.value() if hasattr(widget, "spinAnneeDebut") else annee_fin

        if annee_debut == annee_fin:
            self.iface.messageBar().pushWarning(
                "SN Analyser",
                "L analyse d erosion necessite deux annees differentes (Debut != Fin).",
            )
            return None

        work_dir = core_utils.get_temp_dir(self.plugin_dir)

        # Etape 1 : recherche + telechargement + calcul NDWI pour chaque
        # annee (SANS classifier tout de suite : il faut le NDWI des deux
        # dates pour calculer un seuil Otsu commun).
        # Meme logique : on cale l annee de debut sur le mois de l annee de
        # fin pour eviter de confondre variation saisonniere du trait de
        # cote/plans d eau interieurs et erosion/accretion reelle.
        # path_row_cible=None : le composite annuel couvre lui-meme tous les
        # couloirs satellite necessaires pour chaque annee independamment.
        prepares = {}
        prep_fin = self._telecharger_ndwi_annee(bbox, nom, annee_fin, work_dir)
        if prep_fin is None:
            return None
        prepares[annee_fin] = prep_fin

        prep_debut = self._telecharger_ndwi_annee(bbox, nom, annee_debut, work_dir)
        if prep_debut is None:
            return None
        prepares[annee_debut] = prep_debut

        # Etape 2 : seuil Otsu UNIQUE, calcule sur l ensemble des NDWI des
        # deux dates. Un seuil different par date ferait deriver la
        # definition meme de "eau" dans le temps et peut a lui seul produire
        # une fausse erosion/accretion sans lien avec un changement reel.
        if seuil_manuel is not None:
            seuil_global = seuil_manuel
        else:
            try:
                seuil_global = core_ero.calculer_seuil_otsu_multi(
                    [prepares[annee_debut]["index_path"], prepares[annee_fin]["index_path"]]
                )
            except Exception as e:
                self._log_critical("Calcul du seuil NDWI commun echoue : " + str(e))
                return None

        # Etape 3 : classification (seuil commun) + decoupage pour chaque
        # annee.
        resultats = {}
        for annee in (annee_debut, annee_fin):
            res = self._classifier_erosion_annee(prepares[annee], geom, nom, annee, work_dir, seuil_global)
            if res is None:
                return None
            resultats[annee] = res

        res_debut = resultats[annee_debut]
        res_fin = resultats[annee_fin]

        try:
            diag = core_ero.comparer_trait_cote(res_debut["classe_clip_path"], res_fin["classe_clip_path"])
        except Exception as e:
            self._log_critical("Comparaison du trait de cote echouee : " + str(e))
            return None

        sens = "recul net (erosion dominante)" if diag["recul_net"] else "avancee nette (accretion dominante)"
        message = (
            "Erosion cotiere " + nom + " (" + str(annee_debut) + "-" + str(annee_fin) + ") : "
            "%.2f ha gagnes par la mer, %.2f ha gagnes sur la mer -> %s (bilan %.2f ha, seuil NDWI=%.3f)."
            % (diag["erosion_ha"], diag["accretion_ha"], sens, diag["bilan_ha"], seuil_global)
        )
        self.iface.messageBar().pushSuccess("SN Analyser", message)

        resume = {
            "erosion_ha": diag["erosion_ha"],
            "accretion_ha": diag["accretion_ha"],
            "bilan_erosion_ha": diag["bilan_ha"],
            "recul_net": diag["recul_net"],
        }

        # Extraction vectorielle du trait de cote (une ligne par annee, symbologie
        # differente) + transects perpendiculaires etiquetes avec la distance
        # mesuree entre les deux traits (methode inspiree de DSAS).
        try:
            self._generer_traits_de_cote_et_transects(
                work_dir, nom, annee_debut, annee_fin, res_debut, res_fin
            )
        except Exception as e:
            self.iface.messageBar().pushWarning(
                "SN Analyser",
                "Extraction du trait de cote/transects echouee (l analyse en surface reste valide) : " + str(e),
            )

        for annee, res in resultats.items():
            for key in ("extrait_path", "ndwi_path", "classe_path"):
                p = res.get(key)
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

        return resume

    def _generer_traits_de_cote_et_transects(self, work_dir, nom, annee_debut, annee_fin, res_debut, res_fin):
        """Extrait le trait de cote (ligne) de chaque annee, les ajoute a la
        carte avec une symbologie differente par annee, genere des transects
        perpendiculaires le long du trait de cote de debut (baseline) et les
        ajoute comme couche etiquetee avec la distance mesuree (m) et
        coloree selon le sens (erosion/accretion/stable)."""
        from qgis.core import (
            QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY, QgsField,
            QgsLineSymbol, QgsSingleSymbolRenderer, QgsCategorizedSymbolRenderer,
            QgsRendererCategory, QgsPalLayerSettings, QgsVectorLayerSimpleLabeling,
            QgsTextFormat,
        )
        from qgis.PyQt.QtCore import QVariant
        from qgis.PyQt.QtGui import QColor
        from .core import erosion as core_ero

        prefix = nom.replace(" ", "_")
        gpkg_debut = os.path.join(work_dir, prefix + "_" + str(annee_debut) + "_trait_cote.gpkg")
        gpkg_fin = os.path.join(work_dir, prefix + "_" + str(annee_fin) + "_trait_cote.gpkg")

        _, couche_debut = core_ero.extraire_trait_cote(res_debut["classe_clip_path"], gpkg_debut, "trait_cote")
        _, couche_fin = core_ero.extraire_trait_cote(res_fin["classe_clip_path"], gpkg_fin, "trait_cote")

        # --- couches "trait de cote" (une par annee, couleurs distinctes) ---
        couleurs_annees = ["#E31A1C", "#1F78B4", "#33A02C", "#FF7F00", "#6A3D9A"]

        def _ajouter_trait_cote(gpkg_path, couche, annee, couleur):
            uri = gpkg_path + "|layername=" + couche
            lyr = QgsVectorLayer(uri, "Trait de cote " + nom + " " + str(annee), "ogr")
            if not lyr.isValid():
                return None
            symbole = QgsLineSymbol.createSimple({"line_color": couleur, "line_width": "0.9"})
            lyr.setRenderer(QgsSingleSymbolRenderer(symbole))
            QgsProject.instance().addMapLayer(lyr)
            return lyr

        _ajouter_trait_cote(gpkg_debut, couche_debut, annee_debut, couleurs_annees[0])
        _ajouter_trait_cote(gpkg_fin, couche_fin, annee_fin, couleurs_annees[1 % len(couleurs_annees)])

        # --- transects perpendiculaires + mesure de distance ---
        transects = core_ero.generer_transects(gpkg_debut, couche_debut, longueur_m=300, pas_m=100)
        if not transects:
            self.iface.messageBar().pushWarning(
                "SN Analyser", "Aucun transect genere (trait de cote trop court/absent sur cette zone)."
            )
            return

        mesures = core_ero.mesurer_transects(
            transects, gpkg_debut, couche_debut, gpkg_fin, couche_fin, res_debut["classe_clip_path"]
        )
        if not mesures:
            self.iface.messageBar().pushWarning(
                "SN Analyser",
                "Aucune mesure de distance possible (les deux traits de cote ne se croisent avec aucun transect)."
            )
            return

        crs_wkt = None
        ds_tmp = QgsVectorLayer(gpkg_debut + "|layername=" + couche_debut, "tmp", "ogr")
        if ds_tmp.isValid():
            crs_wkt = ds_tmp.crs().authid()

        lyr_transects = QgsVectorLayer(
            "LineString?crs=" + (crs_wkt or "EPSG:32628"),
            "Transects erosion " + nom + " " + str(annee_debut) + "-" + str(annee_fin),
            "memory",
        )
        prov = lyr_transects.dataProvider()
        prov.addAttributes([
            QgsField("distance_m", QVariant.Double),
            QgsField("sens", QVariant.String),
        ])
        lyr_transects.updateFields()

        feats = []
        for m in mesures:
            geom = QgsGeometry.fromPolylineXY([QgsPointXY(m["x1"], m["y1"]), QgsPointXY(m["x2"], m["y2"])])
            f = QgsFeature(lyr_transects.fields())
            f.setGeometry(geom)
            f.setAttribute("distance_m", m["distance_m"])
            f.setAttribute("sens", m["sens"])
            feats.append(f)
        prov.addFeatures(feats)
        lyr_transects.updateExtents()

        # symbologie par categorie (sens) : rouge=erosion, vert=accretion, gris=stable/indetermine
        couleurs_sens = {"erosion": "#E31A1C", "accretion": "#33A02C", "stable": "#969696", "indetermine": "#969696"}
        categories = []
        for valeur, couleur in couleurs_sens.items():
            sym = QgsLineSymbol.createSimple({"line_color": couleur, "line_width": "0.6"})
            categories.append(QgsRendererCategory(valeur, sym, valeur))
        renderer = QgsCategorizedSymbolRenderer("sens", categories)
        lyr_transects.setRenderer(renderer)

        # etiquetage : distance en metres, affichee le long de chaque transect
        pal = QgsPalLayerSettings()
        pal.fieldName = "concat(round(\"distance_m\"), ' m')"
        pal.isExpression = True
        pal.placement = QgsPalLayerSettings.Line
        texte = QgsTextFormat()
        texte.setSize(8)
        pal.setFormat(texte)
        lyr_transects.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        lyr_transects.setLabelsEnabled(True)

        QgsProject.instance().addMapLayer(lyr_transects)

        nb_erosion = sum(1 for m in mesures if m["sens"] == "erosion")
        nb_accretion = sum(1 for m in mesures if m["sens"] == "accretion")
        self.iface.messageBar().pushInfo(
            "SN Analyser",
            str(len(mesures)) + " transect(s) mesure(s) (" + str(nb_erosion) + " erosion, "
            + str(nb_accretion) + " accretion). Trait de cote et transects ajoutes a la carte.",
        )

    def _telecharger_ndwi_annee(self, bbox, nom, annee, work_dir, path_row_cible=None):
        """Construit un composite annuel sans nuages (multi-scenes, voir
        core/landsat.py:telecharger_composite_annuel) et calcule le NDWI pour
        UNE annee donnee. Ne classifie PAS : le seuil Otsu commun doit d abord
        etre calcule sur les deux dates comparees (voir _run_erosion_analysis).

        :return: dict {composite, prefix, extrait_path, index_path} ou None si echec.
        """
        from .core import landsat as core_landsat
        from .core import ndwi as core_ndwi

        prefix = nom.replace(" ", "_") + "_" + str(annee)
        extrait_path = os.path.join(work_dir, prefix + "_green_nir.tif")

        try:
            composite = core_landsat.telecharger_composite_annuel(
                bbox, annee, ["green", "nir"], extrait_path,
                path_row_cible=path_row_cible, nuages_max=70, max_scenes=8,
            )
        except Exception as e:
            self._log_critical("Composite Landsat echoue (" + str(annee) + ") : " + str(e))
            return None

        if composite["nb_scenes_utilisees"] < composite["nb_scenes_demandees"]:
            self.iface.messageBar().pushWarning(
                "SN Analyser",
                "Composite " + str(annee) + " : " + str(composite["nb_scenes_utilisees"]) + "/"
                + str(composite["nb_scenes_demandees"]) + " scenes utilisees ("
                + "{:.0f}".format(composite["couverture_pct"]) + "% de couverture). "
                + "Voir la console Python QGIS pour le detail des scenes ignorees.",
            )
        self._pulse()

        ndwi_path = os.path.join(work_dir, prefix + "_ndwi.tif")
        try:
            core_ndwi.calculer_ndwi_depuis_extrait(extrait_path, ndwi_path, idx_green=1, idx_nir=2)
        except Exception as e:
            self._log_critical("Calcul NDWI echoue (" + str(annee) + ") : " + str(e))
            return None

        return {
            "composite": composite,
            "prefix": prefix,
            "extrait_path": extrait_path,
            "index_path": ndwi_path,
        }

    def _classifier_erosion_annee(self, prep, geom, nom, annee, work_dir, seuil):
        """Classifie le NDWI prepare par _telecharger_ndwi_annee (seuil Otsu
        commun aux deux dates comparees), decoupe a la zone d etude et
        ajoute la couche a la carte.

        :return: dict {scene_id, cloud_cover, seuil_ndwi, classe_clip_path, ...}
            ou None si une etape a echoue.
        """
        from qgis.core import QgsRasterLayer
        from .core import erosion as core_ero
        from .core import utils as core_utils

        composite = prep["composite"]
        prefix = prep["prefix"]
        ndwi_path = prep["index_path"]

        classe_path = os.path.join(work_dir, prefix + "_eau.tif")
        classe_clip_path = os.path.join(work_dir, prefix + "_eau_clip.tif")

        try:
            _, seuil_utilise = core_ero.classifier_eau(ndwi_path, classe_path, seuil=seuil)
        except Exception as e:
            self._log_critical("Classification echouee (" + str(annee) + ") : " + str(e))
            return None

        geojson_path = os.path.join(work_dir, prefix + "_zone_ero.geojson")
        # Ecriture directe du GeoJSON par concatenation de chaine, SANS repasser
        # par json.loads(geom.asJson()) : ce round-trip declenchait un crash memoire
        # (access violation) lie au ramasse-miettes Python interferant avec des
        # objets C++ QGIS encore en cours d utilisation.
        geometry_json_str = geom.asJson()
        feature_collection_str = (
            '{"type": "FeatureCollection", "features": [{"type": "Feature", '
            '"properties": {}, "geometry": ' + geometry_json_str + "}]}"
        )
        with open(geojson_path, "w", encoding="utf-8") as f:
            f.write(feature_collection_str)

        try:
            core_utils.clip_raster_to_geometry(classe_path, geojson_path, classe_clip_path, nodata=255)
        except Exception as e:
            self._log_critical("Decoupage echoue (" + str(annee) + ") : " + str(e))
            return None

        # Nettoyage : ne conserve que les composantes d eau connexes assez
        # grandes pour etre la mer (le seuil par defaut, ~1.35 ha, exclut
        # mares/bassins de retenue/sols humides isoles a l interieur des
        # terres, sans rapport avec le littoral -- voir docstring de la
        # fonction). Applique APRES le decoupage a la zone d etude, pour que
        # la taille des composantes reflete ce qui est reellement visible
        # dans la zone analysee.
        try:
            core_ero.filtrer_petites_composantes_eau(classe_clip_path)
        except Exception as e:
            self._log_critical("Filtrage des petites composantes d eau non applique (" + str(annee) + ") : " + str(e))

        self._pulse()
        raster_layer = QgsRasterLayer(classe_clip_path, "Eau-Terre " + nom + " " + str(annee))
        if raster_layer.isValid():
            QgsProject.instance().addMapLayer(raster_layer)
            self._appliquer_symbologie_classification(raster_layer, "#1F78B4", "Eau", "Terre")

        return {
            "nb_scenes": composite["nb_scenes_utilisees"],
            "couverture_pct": composite["couverture_pct"],
            "seuil_ndwi": seuil_utilise,
            "extrait_path": prep["extrait_path"],
            "ndwi_path": ndwi_path,
            "classe_path": classe_path,
            "classe_clip_path": classe_clip_path,
        }

    def _generer_raster_risque(self, cle, score_array, base_mask, geotransform, projection,
                                work_dir, prefix, label, nom, annee):
        """Classe un raster de score (0-100, array numpy, meme grille qu un
        des indices deja decoupes) en 5 classes de risque (memes paliers que
        core.ire.CLASSES_RISQUE : Tres faible/Faible/Moyen/Eleve/Tres eleve)
        et le charge sur la carte avec une symbologie paletee vert->rouge.
        Les pixels hors de la zone d etude (base_mask False) restent
        transparents (NoData), pas comptes dans une classe.

        Le fichier GeoTIFF genere N EST PAS supprime (contrairement aux
        autres fichiers intermediaires du pipeline) : c est le fichier
        source de la couche chargee dans le projet.

        :return: QgsRasterLayer charge, ou None si la couche produite n est
            pas valide.
        """
        import numpy as np
        from osgeo import gdal
        from qgis.core import QgsRasterLayer, QgsPalettedRasterRenderer, QgsProject
        from qgis.PyQt.QtGui import QColor

        NODATA_CLASSE = 255
        bornes = [0, 20, 40, 60, 80, 101]
        classe_array = np.digitize(score_array, bornes[1:-1]).astype(np.uint8)  # 0..4
        classe_array = np.where(base_mask, classe_array, NODATA_CLASSE).astype(np.uint8)

        raster_path = os.path.join(work_dir, prefix + "_" + cle + "_classe.tif")
        hauteur, largeur = classe_array.shape
        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(raster_path, largeur, hauteur, 1, gdal.GDT_Byte)
        out_ds.SetGeoTransform(geotransform)
        out_ds.SetProjection(projection)
        band = out_ds.GetRasterBand(1)
        band.SetNoDataValue(NODATA_CLASSE)
        band.WriteArray(classe_array)
        out_ds.FlushCache()
        out_ds = None

        layer_name = label + " - " + nom + " (" + str(annee) + ")"
        raster_layer = QgsRasterLayer(raster_path, layer_name)
        if not raster_layer.isValid():
            self._log_critical("Raster de risque invalide (" + cle + ") : " + raster_path)
            return None

        palette = [
            (0, QColor(26, 152, 80), "Tres faible"),
            (1, QColor(166, 217, 106), "Faible"),
            (2, QColor(255, 255, 191), "Moyen"),
            (3, QColor(253, 174, 97), "Eleve"),
            (4, QColor(215, 48, 39), "Tres eleve"),
        ]
        classes = [QgsPalettedRasterRenderer.Class(val, color, lib) for val, color, lib in palette]
        renderer = QgsPalettedRasterRenderer(raster_layer.dataProvider(), 1, classes)
        raster_layer.setRenderer(renderer)
        raster_layer.setOpacity(0.75)

        QgsProject.instance().addMapLayer(raster_layer, True)
        # Les nouvelles couches sont inserees en tete d arbre par defaut :
        # on repasse la surbrillance de la zone selectionnee au sommet pour
        # qu elle reste visible par-dessus le raster de risque tout juste
        # ajoute (sinon le raster, oppacite 0.75, la recouvre visuellement).
        self._remonter_highlight_au_sommet()
        return raster_layer

    def _attendre(self, futur, fn=None, *args, delai=None):
        """Resultat d une tache lancee en arriere-plan, en gardant QGIS
        reactif ; si la tache n a pas ete lancee, appel direct de fn.

        :param delai: attente maximale (s). Au-dela, TimeoutError : la donnee
            est abandonnee (journalisee par l appelant) et l analyse continue
            (serveurs Overpass injoignables, Sentinel-5P trop lent...)."""
        if futur is None:
            return fn(*args)
        import time
        # delai compte depuis le LANCEMENT de la tache (attribut pose a la
        # soumission) : les sources lentes ne s additionnent plus.
        debut = getattr(futur, "_sn_t0", None) or time.time()
        while not futur.done():
            if delai is not None and time.time() - debut > delai:
                futur.cancel()
                raise TimeoutError("source trop lente, ignoree apres %d s" % delai)
            self._pulse()
            time.sleep(0.05)
        return futur.result()

    def _chemin_cache(self, work_dir, nature, bbox, annee, variante, ext=".tif"):
        import hashlib
        cle = "%s|%s|%s|%s" % (nature, ",".join("%.4f" % c for c in bbox), annee, variante)
        dossier = os.path.join(work_dir, "cache")
        os.makedirs(dossier, exist_ok=True)
        return os.path.join(dossier, nature + "_" + hashlib.md5(cle.encode()).hexdigest()[:16] + ext)

    def _mnt_en_cache(self, core_rh, bbox, resolution, marge, work_dir):
        """MNT Copernicus de la zone (+ marge), conserve en cache : il ne
        change pas d une analyse a l autre (thread-safe, sans Qt)."""
        cache = self._chemin_cache(work_dir, "mnt", bbox, 0, "%d_%.3f" % (resolution, marge))
        if os.path.exists(cache):
            return cache
        tmp = cache + ".part.tif"
        core_rh.telecharger_mnt(bbox, tmp, resolution, marge)
        os.replace(tmp, cache)
        return cache

    def _occupation_en_cache(self, core_occ, bbox, geojson_path, gt, proj, forme, work_dir, prefix):
        import pickle
        cache = self._chemin_cache(work_dir, "worldcover", bbox, 0,
                                   "%s|%s" % (",".join("%.3f" % v for v in gt), forme), ext=".pkl")
        if os.path.exists(cache):
            try:
                with open(cache, "rb") as f:
                    return pickle.load(f)
            except Exception:
                pass
        occ = core_occ.occupation_du_sol(bbox, geojson_path, gt, proj, forme, work_dir, prefix)
        try:
            with open(cache, "wb") as f:
                pickle.dump(occ, f)
        except Exception:
            pass
        return occ

    def _retirer_couches_fichiers(self, prefixe_chemin):
        """Retire du projet toute couche dont la source est un fichier
        commencant par prefixe_chemin (analyse precedente de la meme zone et
        de la meme annee), pour liberer les fichiers avant leur reecriture."""
        project = QgsProject.instance()
        cible = os.path.normcase(os.path.normpath(prefixe_chemin))
        a_retirer = []
        for lyr in project.mapLayers().values():
            src = lyr.source().split("|")[0]
            try:
                if os.path.normcase(os.path.normpath(src)).startswith(cible):
                    a_retirer.append(lyr.id())
            except Exception:
                continue
        if a_retirer:
            project.removeMapLayers(a_retirer)
        root = project.layerTreeRoot()
        for g in list(root.findGroups()):
            if not g.children():
                root.removeChildNode(g)
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.processEvents()

    def _charger_couches_relief(self, couches, nom, annee):
        """Charge relief, courbes, bassins, reseau et sols dans un groupe
        "Relief et hydrologie - <zone> (<annee>)" en tete de l arbre."""
        from qgis.core import (QgsRasterLayer, QgsSingleBandPseudoColorRenderer, QgsColorRampShader,
                               QgsRasterShader, QgsLineSymbol, QgsFillSymbol, QgsPalLayerSettings,
                               QgsVectorLayerSimpleLabeling, QgsTextFormat, QgsRasterBandStats)
        from qgis.PyQt.QtGui import QColor, QPainter

        project = QgsProject.instance()
        root = project.layerTreeRoot()
        nom_groupe = "Relief et hydrologie - %s (%s)" % (nom, annee)
        ancien = root.findGroup(nom_groupe)
        if ancien is not None:
            root.removeChildNode(ancien)
        groupe = root.insertGroup(0, nom_groupe)

        for entree in couches:
            type_couche, chemin, libelle = entree[:3]
            visible = entree[3] if len(entree) > 3 else True
            try:
                if type_couche in ("mnt", "ombrage", "sols"):
                    lyr = QgsRasterLayer(chemin, libelle)
                else:
                    lyr = QgsVectorLayer(chemin, libelle, "ogr")
                if not lyr.isValid():
                    self._log_critical("Couche invalide : " + libelle)
                    continue
                if type_couche == "mnt":
                    st = lyr.dataProvider().bandStatistics(1, QgsRasterBandStats.Min | QgsRasterBandStats.Max)
                    vmin, vmax = st.minimumValue, st.maximumValue
                    if vmax <= vmin:
                        vmax = vmin + 1
                    couleurs = [(0.0, "#1a9641"), (0.25, "#a6d96a"), (0.5, "#ffffbf"), (0.75, "#d7a35c"), (1.0, "#8c510a")]
                    items = [QgsColorRampShader.ColorRampItem(vmin + f * (vmax - vmin), QColor(c),
                                                              "%.0f m" % (vmin + f * (vmax - vmin)))
                             for f, c in couleurs]
                    crs_shader = QgsColorRampShader(vmin, vmax)
                    crs_shader.setColorRampType(QgsColorRampShader.Interpolated)
                    crs_shader.setColorRampItemList(items)
                    shader = QgsRasterShader()
                    shader.setRasterShaderFunction(crs_shader)
                    lyr.setRenderer(QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, shader))
                    lyr.setOpacity(0.85)
                elif type_couche == "ombrage":
                    lyr.setBlendMode(QPainter.CompositionMode_Multiply)
                    lyr.setOpacity(0.55)
                elif type_couche == "sols":
                    qml = os.path.join(self.plugin_dir, "data", "soilgrids_wrb.qml")
                    if os.path.exists(qml):
                        lyr.loadNamedStyle(qml)
                    lyr.setOpacity(0.7)
                elif type_couche == "courbes":
                    lyr.setRenderer(lyr.renderer().__class__(QgsLineSymbol.createSimple(
                        {"color": "139,90,43,200", "width": "0.2"})))
                    pal = QgsPalLayerSettings()
                    pal.fieldName = "altitude"
                    pal.placement = QgsPalLayerSettings.Line
                    fmt = QgsTextFormat()
                    fmt.setSize(7)
                    fmt.setColor(QColor(110, 70, 30))
                    pal.setFormat(fmt)
                    lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
                    lyr.setLabelsEnabled(True)
                elif type_couche == "reseau":
                    lyr.setRenderer(lyr.renderer().__class__(QgsLineSymbol.createSimple(
                        {"color": "31,120,180,255", "width": "0.6", "capstyle": "round"})))
                elif type_couche == "reseau_mg":
                    from qgis.core import QgsProperty, QgsSymbolLayer
                    sym = QgsLineSymbol.createSimple({"color": "21,101,192,255", "width": "0.5", "capstyle": "round"})
                    sym.symbolLayer(0).setDataDefinedProperty(
                        QgsSymbolLayer.PropertyStrokeWidth, QgsProperty.fromExpression('0.2 + 0.35 * "strahler"'))
                    lyr.setRenderer(lyr.renderer().__class__(sym))
                elif type_couche == "bassins_mg":
                    lyr.setRenderer(lyr.renderer().__class__(QgsFillSymbol.createSimple(
                        {"color": "33,113,181,25", "outline_color": "8,48,107,255", "outline_width": "0.8"})))
                elif type_couche == "bassins":
                    lyr.setRenderer(lyr.renderer().__class__(QgsFillSymbol.createSimple(
                        {"color": "0,0,0,0", "outline_color": "8,48,107,255", "outline_width": "0.5",
                         "outline_style": "solid"})))
                project.addMapLayer(lyr, False)
                noeud = groupe.addLayer(lyr)
                if not visible or (type_couche == "sols" and any(e[0] in ("mnt", "ombrage") for e in couches)):
                    noeud.setItemVisibilityChecked(False)
            except Exception as e:
                self._log_critical("Chargement de la couche '" + libelle + "' echoue : " + str(e))
        self._remonter_highlight_au_sommet()

    def _remonter_highlight_au_sommet(self):
        """Replace la couche de surbrillance (_zone_highlight_layer) tout en
        haut de l arbre des couches, pour qu elle reste visible par-dessus
        tout raster de risque ajoute apres elle (cf. _generer_raster_risque).
        """
        layer = getattr(self, "_zone_highlight_layer", None)
        if layer is None:
            return
        root = QgsProject.instance().layerTreeRoot()
        node = root.findLayer(layer.id())
        if node is None:
            return
        clone = node.clone()
        parent = node.parent()
        parent.insertChildNode(0, clone)
        parent.removeChildNode(node)

    def _sous_classe_cochee(self, widget, attr, defaut=True):
        """Etat d une sous-classe (case a cocher sous un risque principal),
        avec repli sur `defaut` si le widget n existe pas (compatibilite si
        l UI n a pas encore ete rechargee)."""
        if not hasattr(widget, attr):
            return defaut
        return getattr(widget, attr).isChecked()

    def _run_risques_analysis(self, widget, layer, feature, niveau, nom, risques_demandes):
        """Pipeline commun aux 6 risques environnementaux INDEPENDANTS du
        panneau "Analyse de Risque Environnemental" (climatique, pollution,
        degradation des terres, hydrologique, urbanisation, biodiversite --
        voir core/ire.RISQUES_CONFIG / calculer_risques_independants).

        Remplace l ancien pipeline IRE combine (_run_ire_analysis, conserve
        pour reference). Chaque risque coche recoit son PROPRE score/niveau,
        il n y a plus de score composite unique. Un seul composite Landsat
        multi-bandes est telecharge et partage entre tous les risques
        coches ; les appels reseau couteux (precipitation, pollution) ne
        sont faits que si un risque qui en a besoin est demande.

        :return: dict {risque_<cle>_score, risque_<cle>_niveau,
            risque_<cle>_label, risque_<cle>_emoji, ...} ou None si echec.
        """
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsGeometry,
        )
        from .core import landsat as core_landsat
        from .core import ndvi as core_ndvi
        from .core import ndbi as core_ndbi
        from .core import nbr as core_nbr
        from .core import bsi as core_bsi
        from .core import lst as core_lst
        from .core import ndwi as core_ndwi
        from .core import ire as core_ire
        from .core import utils as core_utils
        from .core import pollution as core_pollution
        from .core import precipitation as core_precip
        from .core import no2 as core_no2
        from .core import evi as core_evi
        from .core import savi as core_savi
        from .core import salinite as core_salinite
        from .core import aires_protegees as core_aires
        from .core import vulnerabilite as core_vuln

        self.iface.messageBar().pushInfo("SN Analyser", "Analyse des risques lancee pour " + nom + "...")

        geom = QgsGeometry(feature.geometry())
        src_crs = layer.crs()
        dst_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        if src_crs != dst_crs:
            transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
            geom.transform(transform)

        bbox_rect = geom.boundingBox()
        bbox = [bbox_rect.xMinimum(), bbox_rect.yMinimum(), bbox_rect.xMaximum(), bbox_rect.yMaximum()]
        centroide = geom.centroid().asPoint()

        annee = widget.spinAnneeFin.value() if hasattr(widget, "spinAnneeFin") else 2026
        work_dir = core_utils.get_temp_dir(self.plugin_dir)
        prefix = nom.replace(" ", "_") + "_" + str(annee) + "_risques"
        # Retire du projet les couches d une analyse precedente de la meme
        # zone/annee : elles verrouillaient leurs fichiers sous Windows et les
        # nouvelles cartes ne pouvaient pas etre ecrites ("Permission denied").
        self._retirer_couches_fichiers(os.path.join(work_dir, prefix))

        from qgis.core import QgsDistanceArea
        _da = QgsDistanceArea()
        _da.setSourceCrs(dst_crs, QgsProject.instance().transformContext())
        _da.setEllipsoid("WGS84")
        aire_zone_km2 = _da.measureArea(geom) / 1e6

        # Etat des sous-classes cochees (affinent chaque risque, voir
        # RISQUE_SOUS_CLASSES / ui/accueil.ui). Toutes par defaut a True si
        # le risque parent n est pas demande (n a pas d effet dans ce cas).
        veut_temperature = self._sous_classe_cochee(widget, "chkClimTemperature")
        veut_secheresse_clim = self._sous_classe_cochee(widget, "chkClimSecheresse")
        veut_aqi = self._sous_classe_cochee(widget, "chkPolAQI")
        veut_industrie = self._sous_classe_cochee(widget, "chkPolIndustrie")
        veut_vegetation_deg = self._sous_classe_cochee(widget, "chkDegVegetation")
        veut_brulis_deg = self._sous_classe_cochee(widget, "chkDegBrulis")
        veut_sol_nu = self._sous_classe_cochee(widget, "chkDegSolNu")
        veut_salinisation = self._sous_classe_cochee(widget, "chkDegSalinisation")
        veut_zones_humides = self._sous_classe_cochee(widget, "chkHydroZonesHumides")
        veut_precip_hydro = self._sous_classe_cochee(widget, "chkHydroPrecipitations")
        veut_artificialisation = self._sous_classe_cochee(widget, "chkUrbArtificialisation")
        veut_densite_pop = self._sous_classe_cochee(widget, "chkUrbDensitePop", defaut=False)
        veut_infra_urb = self._sous_classe_cochee(widget, "chkUrbInfrastructures", defaut=False)
        veut_vegetation_bio = self._sous_classe_cochee(widget, "chkBioVegetation")
        veut_feux_bio = self._sous_classe_cochee(widget, "chkBioFeux")
        veut_aires_protegees = self._sous_classe_cochee(widget, "chkBioAiresProtegees")
        veut_reseau_hydro = self._sous_classe_cochee(widget, "chkHydroReseauHydro")
        veut_lacs = self._sous_classe_cochee(widget, "chkHydroLacs")
        veut_evolution_bati = self._sous_classe_cochee(widget, "chkUrbEvolutionBati", defaut=False)
        veut_evolution_veg = self._sous_classe_cochee(widget, "chkBioEvolutionVegetation", defaut=False)
        veut_erosion_cotiere = self._sous_classe_cochee(widget, "chkHydroErosionCotiere", defaut=False)
        veut_relief = self._sous_classe_cochee(widget, "chkHydroRelief")
        veut_courbes = self._sous_classe_cochee(widget, "chkHydroCourbes")
        veut_bassins = self._sous_classe_cochee(widget, "chkHydroBassins")
        veut_sols = self._sous_classe_cochee(widget, "chkHydroSols")
        annee_debut = widget.spinAnneeDebut.value() if hasattr(widget, "spinAnneeDebut") else annee
        veut_secheresse_besoin = (
            ("climatique" in risques_demandes and veut_secheresse_clim)
            or ("hydrologique" in risques_demandes and veut_precip_hydro)
        )

        geojson_path = os.path.join(work_dir, prefix + "_zone.geojson")
        geometry_json_str = geom.asJson()
        feature_collection_str = (
            '{"type": "FeatureCollection", "features": [{"type": "Feature", '
            '"properties": {}, "geometry": ' + geometry_json_str + "}]}"
        )
        with open(geojson_path, "w", encoding="utf-8") as f:
            f.write(feature_collection_str)

        # Taches reseau lancees EN PARALLELE des le debut (Open-Meteo,
        # OpenStreetMap/Overpass, MNT Copernicus, SoilGrids) : elles tournent
        # pendant le telechargement des images au lieu de s enchainer apres.
        # Overpass limite a 2 requetes simultanees (sinon erreurs 429).
        from concurrent.futures import ThreadPoolExecutor
        pool_api = ThreadPoolExecutor(max_workers=6)
        pool_osm = ThreadPoolExecutor(max_workers=2)
        self._pools_actifs = [pool_api, pool_osm]
        lat_c, lon_c = centroide.y(), centroide.x()

        class _Futurs(dict):
            """Dictionnaire de taches qui memorise l heure de lancement."""
            def __setitem__(self, cle, fut):
                import time as _t
                try:
                    fut._sn_t0 = _t.time()
                except Exception:
                    pass
                super().__setitem__(cle, fut)
        futurs = _Futurs()
        if veut_secheresse_besoin:
            futurs["precip"] = pool_api.submit(core_precip.recuperer_precipitation_annuelle, lat_c, lon_c, annee)
            futurs["spi"] = pool_api.submit(core_precip.calculer_spi, lat_c, lon_c, annee)
        if "climatique" in risques_demandes:
            futurs["climat"] = pool_api.submit(core_precip.recuperer_climat_complementaire_annuel, lat_c, lon_c, annee)
        if "pollution" in risques_demandes and veut_aqi:
            futurs["no2"] = pool_api.submit(core_pollution.recuperer_no2_open_meteo, lat_c, lon_c)
        if "pollution" in risques_demandes and veut_industrie:
            futurs["industrie"] = pool_osm.submit(core_vuln.distance_industrie_proche, lat_c, lon_c)
        futurs["infra"] = pool_osm.submit(core_vuln.distances_infrastructures_par_categorie, lat_c, lon_c)
        if ("pollution" in risques_demandes and hasattr(widget, "chkPollutionS5P")
                and widget.chkPollutionS5P.isChecked()):
            # Sentinel-5P : fichiers lourds, telecharges EN ARRIERE-PLAN avec
            # un budget de temps (3 passages, ~3 min max) au lieu de bloquer
            # QGIS (15 min mesurees le 26/10/2026 avec l ancien code).
            futurs["s5p"] = pool_api.submit(core_no2.calculer_no2_moyen_zone, bbox, annee, work_dir, 45, 3, None, 180)
        if "biodiversite" in risques_demandes and veut_aires_protegees:
            futurs["aires"] = pool_osm.submit(core_aires.zone_protegee_proche, lat_c, lon_c)
        if "hydrologique" in risques_demandes and veut_reseau_hydro:
            futurs["reseau_osm"] = pool_osm.submit(core_vuln.distance_reseau_hydrographique_proche, lat_c, lon_c)
        if "hydrologique" in risques_demandes and veut_lacs:
            futurs["lacs"] = pool_osm.submit(core_vuln.distance_lac_proche, lat_c, lon_c)
        if "hydrologique" in risques_demandes:
            from .core import relief_hydro as core_rh
            _res_mnt = core_rh.resolution_adaptee(aire_zone_km2)
            _marge = min(0.15, max(0.03, (aire_zone_km2 ** 0.5) / 111.0 * 0.25))
            if veut_relief or veut_courbes or veut_bassins or veut_reseau_hydro:
                futurs["mnt"] = pool_api.submit(self._mnt_en_cache, core_rh, bbox, _res_mnt, _marge, work_dir)
            if veut_sols:
                futurs["sols"] = pool_api.submit(core_rh.analyser_sols, bbox, geojson_path, work_dir, prefix,
                                                 os.path.join(self.plugin_dir, "data", "soilgrids_wrb.qml"))

        # Composite multi-bandes partage : green=1, blue=2, red=3, nir=4,
        # swir1=5, swir2=6, thermal1=7 (green ajoute vs l ancien pipeline
        # IRE, necessaire pour le NDWI du risque hydrologique).
        extrait_path = os.path.join(work_dir, prefix + "_extrait.tif")
        source_imagerie = "Landsat Collection 2 Level-2 (30 m)"
        composite_ok = False
        if annee >= 2017 and hasattr(widget, "chkHauteResolution") and widget.chkHauteResolution.isChecked():
            from .core import sentinel2 as core_s2
            res_s2 = core_s2.resolution_adaptee(aire_zone_km2)
            if res_s2:
                try:
                    cache = self._chemin_cache(work_dir, "composite", bbox, annee, "s2_%d" % res_s2)
                    if os.path.exists(cache) and os.path.exists(cache + ".txt"):
                        import shutil
                        shutil.copyfile(cache, extrait_path)
                        source_imagerie = open(cache + ".txt", encoding="utf-8").read() + " (cache)"
                    else:
                        self.iface.messageBar().pushInfo(
                            "SN Analyser", "Composite Sentinel-2 %d m en cours (%.0f km2)..." % (res_s2, aire_zone_km2)
                        )
                        self._pulse()
                        info_s2 = core_s2.telecharger_composite_s2(bbox, annee, extrait_path, resolution=res_s2,
                                                                   max_dates=6, pulse=self._pulse)
                        source_imagerie = info_s2["source"]
                        if info_s2.get("thermique") and (info_s2.get("couverture_pct") or 0) > 90:
                            import shutil
                            shutil.copyfile(extrait_path, cache)
                            open(cache + ".txt", "w", encoding="utf-8").write(source_imagerie)
                    composite_ok = True
                except Exception as e:
                    self._log_critical("Sentinel-2 indisponible, repli sur Landsat 30 m : " + str(e))
            else:
                self.iface.messageBar().pushInfo(
                    "SN Analyser", "Zone tres etendue (%.0f km2) : Landsat 30 m utilise." % aire_zone_km2
                )
        if not composite_ok:
            try:
                import shutil
                cache = self._chemin_cache(work_dir, "composite", bbox, annee, "landsat")
                if os.path.exists(cache):
                    shutil.copyfile(cache, extrait_path)
                    source_imagerie += " (cache)"
                else:
                    info_ls = core_landsat.telecharger_composite_annuel(
                        bbox, annee, ["green", "blue", "red", "nir", "swir1", "swir2", "thermal1"], extrait_path,
                        nuages_max=70, max_scenes=8, pulse=self._pulse,
                    )
                    if (info_ls.get("couverture_pct") or 0) > 90 and info_ls.get("nb_scenes_utilisees", 0) >= 3:
                        shutil.copyfile(extrait_path, cache)
            except Exception as e:
                self._log_critical("Composite Landsat (risques) echoue (" + str(annee) + ") : " + str(e))
                return None
        self._pulse()


        fichiers_intermediaires = [extrait_path]
        try:
            ndvi_path = os.path.join(work_dir, prefix + "_ndvi.tif")
            core_ndvi.calculer_ndvi_depuis_extrait(extrait_path, ndvi_path, idx_nir=4, idx_red=3)
            ndbi_path = os.path.join(work_dir, prefix + "_ndbi.tif")
            core_ndbi.calculer_ndbi_depuis_extrait(extrait_path, ndbi_path, idx_nir=4, idx_swir=5)
            nbr_path = os.path.join(work_dir, prefix + "_nbr.tif")
            core_nbr.calculer_nbr_depuis_extrait(extrait_path, nbr_path, idx_nir=4, idx_swir2=6)
            bsi_path = os.path.join(work_dir, prefix + "_bsi.tif")
            core_bsi.calculer_bsi_depuis_extrait(extrait_path, bsi_path, idx_blue=2, idx_red=3, idx_nir=4, idx_swir1=5)
            lst_path = os.path.join(work_dir, prefix + "_lst.tif")
            core_lst.calculer_lst_depuis_extrait(extrait_path, lst_path, idx_thermal=7)
            evi_path = os.path.join(work_dir, prefix + "_evi.tif")
            core_evi.calculer_evi_depuis_extrait(extrait_path, evi_path, idx_nir=4, idx_red=3, idx_blue=2)
            savi_path = os.path.join(work_dir, prefix + "_savi.tif")
            core_savi.calculer_savi_depuis_extrait(extrait_path, savi_path, idx_nir=4, idx_red=3)
            si_path = os.path.join(work_dir, prefix + "_si.tif")
            core_salinite.calculer_si_depuis_extrait(extrait_path, si_path, idx_blue=2, idx_red=3)
            ndwi_path = os.path.join(work_dir, prefix + "_ndwi.tif")
            core_ndwi.calculer_ndwi_depuis_extrait(extrait_path, ndwi_path, idx_green=1, idx_nir=4)

            moyennes = {}
            rasters = {}  # cle -> (array, geotransform, projection, masque_valide)
            for cle, chemin in (("ndvi", ndvi_path), ("ndbi", ndbi_path), ("nbr", nbr_path),
                                 ("bsi", bsi_path), ("lst", lst_path), ("evi", evi_path),
                                 ("savi", savi_path), ("si", si_path), ("ndwi", ndwi_path)):
                clip_path = os.path.join(work_dir, prefix + "_" + cle + "_clip.tif")
                core_utils.clip_raster_to_geometry(chemin, geojson_path, clip_path, nodata=0.0)
                arr, gt, proj, masque = core_utils.lire_raster(clip_path)
                rasters[cle] = (arr, gt, proj, masque)
                valeurs_valides = arr[masque]
                moyennes[cle] = float(valeurs_valides.mean()) if valeurs_valides.size else None
                fichiers_intermediaires += [chemin, clip_path]
        except Exception as e:
            self._log_critical("Calcul des indices (risques) echoue (" + str(annee) + ") : " + str(e))
            return None

        self._pulse()

        # Occupation du sol ESA WorldCover 10 m (Planetary Computer) : separe
        # bati / eau / milieux naturels. Les indices de vegetation des risques
        # Degradation des terres et Biodiversite sont recalcules sur les SEULS
        # pixels naturels ou agricoles (un toit a un NDVI faible qui etait lu
        # comme une perte de vegetation), et la part de bati remplace le NDBI
        # pour l Urbanisation. Repli silencieux sur l ancien calcul si la
        # source est indisponible.
        import numpy as np
        occupation = None
        try:
            from .core import occupation_sol as core_occ
            _arr0, gt0, proj0, _m0 = rasters["ndvi"]
            occupation = self._occupation_en_cache(core_occ, bbox, geojson_path, gt0, proj0, _arr0.shape, work_dir, prefix)
        except Exception as e:
            self._log_critical("Occupation du sol ESA WorldCover indisponible (calcul sur tous les pixels) : " + str(e))
        self._pulse()

        moyennes_nat = dict(moyennes)
        masque_naturel = None
        classes_occ = None
        part_bati = None
        part_bati_terrestre = None
        part_naturelle = None
        if occupation is not None:
            classes_occ = occupation["classes"]
            fr = occupation["fractions"]
            part_bati = fr.get("bati", 0.0)
            terres = 1.0 - fr.get("eau", 0.0)
            part_bati_terrestre = (part_bati / terres) if terres > 0 else 0.0
            part_naturelle = sum(fr.get(core_occ.CLASSES[c], 0.0) for c in core_occ.CLASSES_NATURELLES)
            masque_naturel = np.isin(classes_occ, core_occ.CLASSES_NATURELLES)
            for cle in ("ndvi", "evi", "savi", "nbr", "bsi", "si"):
                arr, _gt, _proj, masque = rasters[cle]
                if arr.shape != masque_naturel.shape:
                    continue
                m = masque & masque_naturel
                if int(m.sum()) >= 5:
                    moyennes_nat[cle] = float(arr[m].mean())
            if part_naturelle < 0.05:
                self.iface.messageBar().pushInfo(
                    "SN Analyser",
                    nom + " est presque entierement batie ou en eau (%.0f %% de milieux naturels) : "
                    "les risques Degradation des terres et Biodiversite portent sur tres peu de surface." % (part_naturelle * 100),
                )

        # Precipitation/SPI : necessaire pour climatique (secheresse) et/ou
        # hydrologique (exces de pluie) uniquement.
        total_precip_mm = None
        spi = None
        if veut_secheresse_besoin:
            try:
                total_precip_mm, _d1, _d2 = self._attendre(futurs.get("precip"), core_precip.recuperer_precipitation_annuelle, centroide.y(), centroide.x(), annee)
            except Exception as e:
                self._log_critical("Precipitation non calculee (Open-Meteo) : " + str(e))
            try:
                spi = self._attendre(futurs.get("spi"), core_precip.calculer_spi, centroide.y(), centroide.x(), annee)
            except Exception as e:
                self._log_critical("SPI non calcule (Open-Meteo climatologie) : " + str(e))
            self._pulse()

        # Climat complementaire (vent/humidite/evapotranspiration) :
        # information CONTEXTUELLE seulement pour le risque climatique (pas
        # encore integree au score, voir core/precipitation.
        # recuperer_climat_complementaire_annuel).
        climat_complementaire = {}
        if "climatique" in risques_demandes:
            try:
                climat_complementaire = self._attendre(futurs.get("climat"), core_precip.recuperer_climat_complementaire_annuel, centroide.y(), centroide.x(), annee)
            except Exception as e:
                self._log_critical("Climat complementaire non calcule (vent/humidite/ETP) : " + str(e))
            self._pulse()

        # Pollution NO2/AQI + proximite industrielle : necessaire seulement
        # pour le risque pollution.
        no2_openmeteo_ugm3 = None
        european_aqi = None
        no2_s5p_mol_m2 = None
        distance_industrie_km = None
        if "pollution" in risques_demandes:
            if veut_aqi:
                try:
                    no2_openmeteo_ugm3, details_qualite_air = self._attendre(futurs.get("no2"), core_pollution.recuperer_no2_open_meteo, centroide.y(), centroide.x())
                    if details_qualite_air:
                        european_aqi = details_qualite_air.get("european_aqi")
                except Exception as e:
                    self._log_critical("Pollution NO2 non calculee (Open-Meteo) : " + str(e))
                self._pulse()

            if hasattr(widget, "chkPollutionS5P") and widget.chkPollutionS5P.isChecked():
                self.iface.messageBar().pushInfo(
                    "SN Analyser", "Sentinel-5P (NO2) : fin du telechargement en arriere-plan..."
                )
                try:
                    no2_s5p_mol_m2, _nb_pixels = self._attendre(
                        futurs.get("s5p"), core_no2.calculer_no2_moyen_zone, bbox, annee, work_dir, 45, 3, None, 120,
                        delai=150,
                    )
                except Exception as e:
                    self._log_critical("Pollution NO2 non calculee (Sentinel-5P) : " + str(e))
            self._pulse()

            if veut_industrie:
                try:
                    distance_industrie_km = self._attendre(futurs.get("industrie"), core_vuln.distance_industrie_proche, centroide.y(), centroide.x(), delai=90)
                except Exception as e:
                    self._log_critical("Distance industrielle non calculee (Overpass) : " + str(e))
                self._pulse()

        # Contexte Vulnerabilite (population/infrastructures sensibles) :
        # information CONTEXTUELLE affichee a cote des risques (pas un
        # risque note en soi, cf. discussion classification), calculee des
        # qu au moins un risque est demande.
        contexte_population_densite = None
        contexte_source_population = None
        contexte_distances_infra = None
        try:
            population_ansd = self._population_ansd(niveau, nom)
            if population_ansd is not None:
                from qgis.core import QgsDistanceArea
                d = QgsDistanceArea()
                d.setEllipsoid("WGS84")
                aire_km2 = d.measureArea(geom) / 1_000_000.0
                if aire_km2 > 0:
                    contexte_population_densite = population_ansd / aire_km2
                    contexte_source_population = "ANSD RGPHAE 2023 (recensement reel, " + str(population_ansd) + " hab.)"
            if contexte_population_densite is None:
                raster_population = os.path.join(self.plugin_dir, "data", "sen_population_2020_1km.tif")
                if os.path.exists(raster_population):
                    pop_clip_path = os.path.join(work_dir, prefix + "_population_clip.tif")
                    contexte_population_densite = core_vuln.moyenne_densite_population(
                        raster_population, geojson_path, pop_clip_path
                    )
                    contexte_source_population = "WorldPop 2020 (estimation raster 1km, ANSD non disponible pour cette zone)"
                    fichiers_intermediaires.append(pop_clip_path)
        except Exception as e:
            self._log_critical("Contexte population non calcule : " + str(e))
        try:
            contexte_distances_infra = self._attendre(futurs.get("infra"), core_vuln.distances_infrastructures_par_categorie, centroide.y(), centroide.x(), delai=90)
        except Exception as e:
            self._log_critical("Contexte infrastructures non calcule (Overpass) : " + str(e))
        self._pulse()

        # Aire protegee a proximite : attenue legerement le risque
        # biodiversite (voir core/ire.calculer_ibio). Uniquement recherchee
        # si le risque biodiversite est demande.
        aire_protegee_proche = False
        nom_aire = None
        distance_aire_km = None
        if "biodiversite" in risques_demandes and veut_aires_protegees:
            try:
                trouvee, nom_aire, distance_aire_km = self._attendre(futurs.get("aires"), core_aires.zone_protegee_proche, centroide.y(), centroide.x(), delai=90)
                aire_protegee_proche = bool(trouvee)
            except Exception as e:
                self._log_critical("Recherche aire protegee non effectuee : " + str(e))

        # Reseau hydrographique / lacs (OSM) : sous-classes du risque
        # hydrologique, distinctes du NDWI (cartographie independante de la
        # date de l image satellite).
        distance_reseau_hydro_km = None
        distance_lac_km = None
        if "hydrologique" in risques_demandes:
            if veut_reseau_hydro:
                try:
                    distance_reseau_hydro_km = self._attendre(futurs.get("reseau_osm"), core_vuln.distance_reseau_hydrographique_proche, centroide.y(), centroide.x(), delai=90)
                except Exception as e:
                    self._log_critical("Distance reseau hydrographique non calculee (Overpass) : " + str(e))
                self._pulse()
            if veut_lacs:
                try:
                    distance_lac_km = self._attendre(futurs.get("lacs"), core_vuln.distance_lac_proche, centroide.y(), centroide.x(), delai=90)
                except Exception as e:
                    self._log_critical("Distance lac non calculee (Overpass) : " + str(e))
                self._pulse()

        # Relief, courbes de niveau, bassins versants, reseau hydrographique
        # derive du MNT et types de sol : sous-classes du risque hydrologique
        # (voir core/relief_hydro.py). Les couches sont chargees dans un
        # groupe dedie et alimentent le score hydrologique.
        relief = None
        bassins = None
        sols = None
        aire_drainee_ref = None
        densite_drainage_ref = None
        risque_topo_zone = None
        risque_topo_arr = None
        intervalle_courbes = None
        nb_courbes = None
        couches_relief = []
        if "hydrologique" in risques_demandes and (veut_relief or veut_courbes or veut_bassins
                                                    or veut_reseau_hydro or veut_sols):
            from .core import relief_hydro as core_rh
            res_mnt = core_rh.resolution_adaptee(aire_zone_km2)
            marge = min(0.15, max(0.03, (aire_zone_km2 ** 0.5) / 111.0 * 0.25))
            mnt_large = None
            if veut_relief or veut_courbes or veut_bassins or veut_reseau_hydro:
                try:
                    self.iface.messageBar().pushInfo("SN Analyser", "Relief : MNT Copernicus %d m..." % res_mnt)
                    self._pulse()
                    mnt_large = self._attendre(futurs.get("mnt"), self._mnt_en_cache, core_rh, bbox, res_mnt, marge, work_dir)
                except Exception as e:
                    self._log_critical("MNT Copernicus indisponible : " + str(e))
                self._pulse()
            if mnt_large:
                try:
                    relief = core_rh.analyser_relief(mnt_large, geojson_path, work_dir, prefix)
                    fichiers_intermediaires.append(relief["pente"])
                    if veut_relief:
                        couches_relief.append(("ombrage", relief["ombrage"], "Relief (ombrage)"))
                        couches_relief.append(("mnt", relief["mnt"], "Elevation (m)"))
                    elif not veut_courbes:
                        fichiers_intermediaires += [relief["mnt"], relief["ombrage"]]
                except Exception as e:
                    self._log_critical("Analyse du relief echouee : " + str(e))
                self._pulse()
            if relief is not None and veut_courbes:
                try:
                    intervalle_courbes = core_rh.intervalle_courbes(relief["denivele"])
                    gpkg, nb_courbes = core_rh.courbes_de_niveau(
                        relief["mnt"], os.path.join(work_dir, prefix + "_courbes_niveau.gpkg"), intervalle_courbes
                    )
                    couches_relief.insert(0, ("courbes", gpkg, "Courbes de niveau (%s m)" % intervalle_courbes))
                except Exception as e:
                    self._log_critical("Courbes de niveau non generees : " + str(e))
            if mnt_large and (veut_bassins or veut_reseau_hydro):
                try:
                    self.iface.messageBar().pushInfo("SN Analyser", "Bassins versants et ecoulements (GRASS)...")
                    self._pulse()
                    aire_seuil = max(0.25, aire_zone_km2 / 40.0)
                    bassins = core_rh.bassins_et_reseau(mnt_large, geojson_path, work_dir, prefix, res_mnt, aire_seuil)
                    # Bassins et rivieres MERIT-Hydro (mghydro.com) depuis les
                    # exutoires de la zone (plus forte accumulation GRASS).
                    mg = None
                    try:
                        self.iface.messageBar().pushInfo("SN Analyser", "Bassins et rivieres MERIT-Hydro (mghydro.com)...")
                        self._pulse()
                        exutoires = core_rh.exutoires_principaux(bassins["accumulation"], geojson_path, work_dir,
                                                                  prefix, res_mnt)
                        mg = core_rh.bassins_mghydro(exutoires, geojson_path, work_dir, prefix)
                        bassins["mghydro"] = mg
                    except Exception as e:
                        self._log_critical("MERIT-Hydro (mghydro.com) indisponible, bassins et reseau GRASS conserves : " + str(e))
                    nouvelles = []
                    if veut_reseau_hydro:
                        if mg and mg.get("rivieres"):
                            nouvelles.append(("reseau_mg", mg["rivieres"], "Reseau hydrographique (MERIT-Hydro)", True))
                            nouvelles.append(("reseau", bassins["reseau"], "Ecoulements locaux (MNT 30 m)", False))
                        else:
                            nouvelles.append(("reseau", bassins["reseau"],
                                              "Ecoulements locaux (MNT 30 m)" if mg else "Reseau hydrographique (MNT 30 m)", True))
                    if veut_bassins:
                        if mg and mg.get("bassins"):
                            nouvelles.append(("bassins_mg", mg["bassins"], "Bassins versants (MERIT-Hydro)", True))
                            nouvelles.append(("bassins", bassins["bassins"], "Sous-bassins locaux (MNT 30 m)", False))
                        else:
                            nouvelles.append(("bassins", bassins["bassins"], "Bassins versants (MNT 30 m)", True))
                    couches_relief[0:0] = nouvelles
                    aire_drainee_ref = (mg or {}).get("aire_amont_max_km2") or bassins.get("aire_drainee_max_km2")
                    densite_drainage_ref = (mg["densite_drainage_km_km2"] if mg
                                            else bassins.get("densite_drainage_km_km2"))
                except Exception as e:
                    self._log_critical("Bassins versants non calcules (GRASS) : " + str(e))
                self._pulse()
            if veut_sols:
                try:
                    sols = self._attendre(futurs.get("sols"), core_rh.analyser_sols, bbox, geojson_path, work_dir, prefix,
                                          os.path.join(self.plugin_dir, "data", "soilgrids_wrb.qml"))
                    couches_relief.append(("sols", sols["types_sol"], "Types de sol (WRB, SoilGrids)"))
                except Exception as e:
                    self._log_critical("Types de sol non recuperes (SoilGrids) : " + str(e))
                self._pulse()

            # Susceptibilite topographique pixel par pixel, sur la grille des
            # indices satellites (pour le score ET la carte de risque)
            if relief is not None and (veut_relief or veut_bassins):
                try:
                    _a, gt_g, proj_g, masque_g = rasters["ndwi"]
                    alt_g = core_rh.aligner_sur_grille(relief["mnt"], gt_g, proj_g, _a.shape, -9999) if veut_relief else None
                    pente_g = core_rh.aligner_sur_grille(relief["pente"], gt_g, proj_g, _a.shape, -9999) if veut_relief else None
                    tci_g = None
                    if veut_bassins and bassins is not None:
                        tci_g = core_rh.aligner_sur_grille(bassins["tci"], gt_g, proj_g, _a.shape, -9999)
                    risque_topo_arr = core_rh.risque_topographique(alt_g, pente_g, tci_g)
                    if risque_topo_arr is not None:
                        vals = risque_topo_arr[masque_g & np.isfinite(risque_topo_arr)]
                        risque_topo_zone = float(vals.mean()) if vals.size else None
                        risque_topo_arr = np.nan_to_num(risque_topo_arr, nan=0.0)
                except Exception as e:
                    self._log_critical("Susceptibilite topographique non calculee : " + str(e))
            if bassins is not None:
                fichiers_intermediaires += [bassins["tci"], bassins["accumulation"]]
            if couches_relief:
                self._charger_couches_relief(couches_relief, nom, annee)

        # Evolution du bati (multi-dates, reutilise le pipeline urbanisation
        # historique) : sous-classe du risque Urbanisation, seulement si deux
        # annees distinctes sont selectionnees.
        taux_croissance_bati_pct_an = None
        if "urbanisation" in risques_demandes and veut_evolution_bati and annee_debut != annee:
            try:
                res_bati = self._run_urbanisation_analysis(widget, layer, feature, niveau, nom)
                if res_bati:
                    taux_croissance_bati_pct_an = res_bati.get("taux_croissance_urbaine")
            except Exception as e:
                self._log_critical("Evolution du bati non calculee : " + str(e))
        elif "urbanisation" in risques_demandes and veut_evolution_bati and annee_debut == annee:
            self.iface.messageBar().pushInfo(
                "SN Analyser",
                "Evolution du bati ignoree : annee de debut = annee de fin (choisissez deux annees distinctes).",
            )

        # Evolution/perte de vegetation (multi-dates, reutilise le pipeline
        # environnement historique) : sous-classe du risque Biodiversite,
        # seulement si deux annees distinctes sont selectionnees.
        variation_vegetation_pct = None
        if "biodiversite" in risques_demandes and veut_evolution_veg and annee_debut != annee:
            try:
                res_veg = self._run_environnement_analysis(widget, layer, feature, niveau, nom)
                if res_veg:
                    debut = res_veg.get("surface_vegetation_debut")
                    fin = res_veg.get("surface_vegetation_fin")
                    if debut is not None and fin is not None and debut > 0:
                        variation_vegetation_pct = (fin - debut) / debut * 100.0
            except Exception as e:
                self._log_critical("Evolution de la vegetation non calculee : " + str(e))
        elif "biodiversite" in risques_demandes and veut_evolution_veg and annee_debut == annee:
            self.iface.messageBar().pushInfo(
                "SN Analyser",
                "Evolution de la vegetation ignoree : annee de debut = annee de fin (choisissez deux annees distinctes).",
            )

        # Erosion cotiere (multi-dates, reutilise le pipeline erosion
        # historique + genere trait de cote/transects sur la carte) : sous-
        # classe du risque Hydrologique, uniquement pour une zone cotiere et
        # deux annees distinctes (meme garde-fou que l ancienne case
        # chkErosion, voir _est_zone_cotiere).
        bilan_erosion_ha_an = None
        if "hydrologique" in risques_demandes and veut_erosion_cotiere:
            if not self._est_zone_cotiere(niveau, nom):
                self.iface.messageBar().pushInfo(
                    "SN Analyser",
                    niveau + " « " + nom + " » ne touche pas le littoral : "
                    "erosion cotiere ignoree.",
                )
            elif annee_debut == annee:
                self.iface.messageBar().pushInfo(
                    "SN Analyser",
                    "Erosion cotiere ignoree : annee de debut = annee de fin (choisissez deux annees distinctes).",
                )
            else:
                try:
                    res_erosion = self._run_erosion_analysis(widget, layer, feature, niveau, nom)
                    if res_erosion:
                        bilan_ha = res_erosion.get("bilan_erosion_ha")
                        nb_annees = annee - annee_debut
                        if bilan_ha is not None and nb_annees > 0:
                            bilan_erosion_ha_an = bilan_ha / nb_annees
                except Exception as e:
                    self._log_critical("Erosion cotiere non calculee : " + str(e))

        # Appels INDIVIDUELS par risque (pas le dispatcher partage) : chaque
        # risque applique ses PROPRES sous-classes cochees, et deux risques
        # qui partagent un meme indicateur brut (ex : NDVI utilise par
        # Degradation des terres ET Biodiversite) peuvent l activer/desactiver
        # independamment l un de l autre.
        scores = {}

        if "degradation_terres" in risques_demandes:
            score = core_ire.calculer_idt(
                mean_ndvi=moyennes_nat["ndvi"] if veut_vegetation_deg else None,
                mean_nbr=moyennes_nat["nbr"] if veut_brulis_deg else None,
                mean_bsi=moyennes_nat["bsi"] if veut_sol_nu else None,
                mean_evi=moyennes_nat["evi"] if veut_vegetation_deg else None,
                mean_savi=moyennes_nat["savi"] if veut_vegetation_deg else None,
                mean_si=moyennes_nat["si"] if veut_salinisation else None,
                superficie_reboisee_ha=self._effort_reboisement_region(niveau, nom, feature),
            )
            if score is not None:
                scores["degradation_terres"] = {
                    "score": score, "niveau": core_ire.classifier_niveau(score),
                    **core_ire.RISQUES_CONFIG["degradation_terres"],
                }

        if "urbanisation" in risques_demandes:
            score = core_ire.calculer_iu(
                mean_ndbi=moyennes["ndbi"] if veut_artificialisation else None,
                mean_pop_density=contexte_population_densite if veut_densite_pop else None,
                distances_infra_km=contexte_distances_infra if veut_infra_urb else None,
                taux_croissance_pct_an=taux_croissance_bati_pct_an if veut_evolution_bati else None,
                part_bati=part_bati if veut_artificialisation else None,
            )
            if score is not None:
                scores["urbanisation"] = {
                    "score": score, "niveau": core_ire.classifier_niveau(score),
                    **core_ire.RISQUES_CONFIG["urbanisation"],
                }

        if "climatique" in risques_demandes:
            score = core_ire.calculer_ic(
                mean_lst=moyennes["lst"] if veut_temperature else None,
                total_precip_mm=total_precip_mm if veut_secheresse_clim else None,
                spi=spi if veut_secheresse_clim else None,
            )
            if score is not None:
                scores["climatique"] = {
                    "score": score, "niveau": core_ire.classifier_niveau(score),
                    **core_ire.RISQUES_CONFIG["climatique"],
                }

        if "pollution" in risques_demandes:
            score, source = core_ire.calculer_ip(
                european_aqi=european_aqi if veut_aqi else None,
                no2_s5p_mol_m2=no2_s5p_mol_m2,
                no2_openmeteo_ugm3=no2_openmeteo_ugm3 if veut_aqi else None,
                distance_industrie_km=distance_industrie_km if veut_industrie else None,
            )
            if score is not None:
                scores["pollution"] = {
                    "score": score, "niveau": core_ire.classifier_niveau(score), "source": source,
                    **core_ire.RISQUES_CONFIG["pollution"],
                }

        if "hydrologique" in risques_demandes:
            score = core_ire.calculer_ih(
                mean_ndwi=moyennes["ndwi"] if veut_zones_humides else None,
                total_precip_mm=total_precip_mm if veut_precip_hydro else None,
                spi=spi if veut_precip_hydro else None,
                distance_reseau_hydro_km=distance_reseau_hydro_km if veut_reseau_hydro else None,
                distance_lac_km=distance_lac_km if veut_lacs else None,
                bilan_erosion_ha_an=bilan_erosion_ha_an if veut_erosion_cotiere else None,
                risque_topo=risque_topo_zone,
                aire_drainee_max_km2=aire_drainee_ref if veut_bassins else None,
                densite_drainage_km_km2=densite_drainage_ref if veut_reseau_hydro else None,
                argile_moy_pct=sols["argile_moy_pct"] if sols else None,
                impermeabilite_sol=sols["impermeabilite"] if sols else None,
            )
            if score is not None:
                scores["hydrologique"] = {
                    "score": score, "niveau": core_ire.classifier_niveau(score),
                    **core_ire.RISQUES_CONFIG["hydrologique"],
                }

        if "biodiversite" in risques_demandes:
            score = core_ire.calculer_ibio(
                mean_ndvi=moyennes_nat["ndvi"] if veut_vegetation_bio else None,
                mean_nbr=moyennes_nat["nbr"] if veut_feux_bio else None,
                mean_evi=moyennes_nat["evi"] if veut_vegetation_bio else None,
                mean_savi=moyennes_nat["savi"] if veut_vegetation_bio else None,
                aire_protegee_proche=aire_protegee_proche if veut_aires_protegees else False,
                variation_vegetation_pct=variation_vegetation_pct if veut_evolution_veg else None,
                part_bati_terrestre=part_bati_terrestre if veut_vegetation_bio else None,
            )
            if score is not None:
                scores["biodiversite"] = {
                    "score": score, "niveau": core_ire.classifier_niveau(score),
                    **core_ire.RISQUES_CONFIG["biodiversite"],
                }

        if not scores:
            self.iface.messageBar().pushWarning(
                "SN Analyser", "Aucun risque n a pu etre calcule (donnees indisponibles pour " + nom + ")."
            )
            return None

        # Chargement + categorisation du raster de resultat pour chaque
        # risque calcule : meme formule que le score zonal (moyenne), mais
        # appliquee PIXEL PAR PIXEL (core_ire.normaliser accepte les arrays
        # numpy), puis classee en 5 classes de risque (memes paliers que
        # core.ire.CLASSES_RISQUE) et affichee sur la carte (vert -> rouge).
        import numpy as np

        constructeurs_raster = {
            "degradation_terres": lambda: core_ire.calculer_idt(
                mean_ndvi=rasters["ndvi"][0] if veut_vegetation_deg else None,
                mean_nbr=rasters["nbr"][0] if veut_brulis_deg else None,
                mean_bsi=rasters["bsi"][0] if veut_sol_nu else None,
                mean_evi=rasters["evi"][0] if veut_vegetation_deg else None,
                mean_savi=rasters["savi"][0] if veut_vegetation_deg else None,
                mean_si=rasters["si"][0] if veut_salinisation else None,
                superficie_reboisee_ha=self._effort_reboisement_region(niveau, nom, feature),
            ),
            "urbanisation": lambda: core_ire.calculer_iu(
                mean_ndbi=rasters["ndbi"][0] if veut_artificialisation else None,
                mean_pop_density=contexte_population_densite if veut_densite_pop else None,
                taux_croissance_pct_an=taux_croissance_bati_pct_an if veut_evolution_bati else None,
                part_bati=(core_occ.densite_locale(classes_occ == core_occ.CLASSE_BATI, rayon=1)
                           if (classes_occ is not None and veut_artificialisation) else None),
            ),
            "biodiversite": lambda: core_ire.calculer_ibio(
                mean_ndvi=rasters["ndvi"][0] if veut_vegetation_bio else None,
                mean_nbr=rasters["nbr"][0] if veut_feux_bio else None,
                mean_evi=rasters["evi"][0] if veut_vegetation_bio else None,
                mean_savi=rasters["savi"][0] if veut_vegetation_bio else None,
                aire_protegee_proche=aire_protegee_proche if veut_aires_protegees else False,
                variation_vegetation_pct=variation_vegetation_pct if veut_evolution_veg else None,
                part_bati_terrestre=(core_occ.densite_locale(classes_occ == core_occ.CLASSE_BATI, rayon=3)
                                     if (classes_occ is not None and veut_vegetation_bio) else None),
            ),
            "climatique": lambda: core_ire.calculer_ic(
                mean_lst=rasters["lst"][0] if (veut_temperature and moyennes.get("lst") is not None) else None,
                total_precip_mm=total_precip_mm if veut_secheresse_clim else None,
                spi=spi if veut_secheresse_clim else None,
            ),
            "hydrologique": lambda: core_ire.calculer_ih(
                mean_ndwi=rasters["ndwi"][0] if veut_zones_humides else None,
                total_precip_mm=total_precip_mm if veut_precip_hydro else None,
                spi=spi if veut_precip_hydro else None,
                distance_reseau_hydro_km=distance_reseau_hydro_km if veut_reseau_hydro else None,
                distance_lac_km=distance_lac_km if veut_lacs else None,
                bilan_erosion_ha_an=bilan_erosion_ha_an if veut_erosion_cotiere else None,
                risque_topo=risque_topo_arr,
                aire_drainee_max_km2=aire_drainee_ref if veut_bassins else None,
                densite_drainage_km_km2=densite_drainage_ref if veut_reseau_hydro else None,
                argile_moy_pct=sols["argile_moy_pct"] if sols else None,
                impermeabilite_sol=sols["impermeabilite"] if sols else None,
            ),
        }
        base_par_risque = {
            "degradation_terres": "ndvi", "urbanisation": "ndbi", "biodiversite": "ndvi",
            "climatique": "lst" if moyennes.get("lst") is not None else "ndvi", "hydrologique": "ndwi",
        }

        for cle, infos in scores.items():
            try:
                if cle == "pollution":
                    # Pas de donnee spatiale (AQI/NO2 = mesure ponctuelle au
                    # centroide) : raster uniforme sur l emprise de la zone,
                    # valeur unique = score calcule, pour rester coherent
                    # avec la demande "charger et categoriser le resultat"
                    # meme pour un risque sans variation spatiale mesuree.
                    base_arr, gt, proj, base_mask = rasters["ndvi"]
                    score_array = np.where(base_mask, infos["score"], 0.0)
                elif cle in constructeurs_raster:
                    score_array = constructeurs_raster[cle]()
                    _, gt, proj, base_mask = rasters[base_par_risque[cle]]
                    if (cle in ("degradation_terres", "biodiversite") and masque_naturel is not None
                            and masque_naturel.shape == base_mask.shape):
                        # carte limitee aux milieux naturels/agricoles
                        base_mask = base_mask & masque_naturel
                else:
                    continue
                self._generer_raster_risque(
                    cle, score_array, base_mask, gt, proj, work_dir, prefix,
                    infos["emoji"] + " " + infos["label"], nom, annee,
                )
            except Exception as e:
                self._log_critical("Raster de risque non genere (" + cle + ") : " + str(e))

        resultat = {}
        resume_parts = []
        for cle, infos in scores.items():
            resultat["risque_" + cle + "_score"] = infos["score"]
            resultat["risque_" + cle + "_niveau"] = infos["niveau"]
            resultat["risque_" + cle + "_label"] = infos["label"]
            resultat["risque_" + cle + "_emoji"] = infos["emoji"]
            if "source" in infos:
                resultat["risque_" + cle + "_source"] = infos["source"]
            resume_parts.append(
                infos["emoji"] + " " + infos["label"] + " : %.1f/100 (%s)" % (infos["score"], infos["niveau"])
            )

        resultat["contexte_source_imagerie"] = source_imagerie
        if relief is not None:
            resultat["contexte_relief"] = (
                "altitude %.0f a %.0f m (moyenne %.0f m), pente moyenne %.1f %%, %.0f %% de zones basses (< 5 m), "
                "%.0f %% de zones plates (< 2 %%)" % (
                    relief["alt_min"], relief["alt_max"], relief["alt_moy"], relief["pente_moy_pct"],
                    relief["part_basse_altitude"] * 100, relief["part_plate"] * 100)
            )
        if nb_courbes is not None:
            resultat["contexte_courbes_niveau"] = "%d courbes, equidistance %s m" % (nb_courbes, intervalle_courbes)
        if bassins is not None:
            morceaux = []
            mg = bassins.get("mghydro")
            if mg:
                txt_mg = "MERIT-Hydro : %d bassin(s) versant(s)" % mg["nb_bassins"]
                if mg.get("aire_amont_max_km2"):
                    txt_mg += ", aire amont jusqu a %.0f km2" % mg["aire_amont_max_km2"]
                if mg.get("nb_troncons"):
                    txt_mg += ", %d troncons de riviere (ordre de Strahler max %s)" % (mg["nb_troncons"], mg["ordre_max"])
                    txt_mg += ", %.1f km de cours d eau dans la zone" % mg["longueur_zone_km"]
                else:
                    txt_mg += ", aucun cours d eau permanent en amont"
                morceaux.append(txt_mg + " ; detail local (MNT 30 m)")
            morceaux.append("%d sous-bassin(s)" % bassins["nb_bassins"])
            if bassins.get("longueur_reseau_km") is not None:
                morceaux.append("reseau de %.1f km" % bassins["longueur_reseau_km"])
            if bassins.get("densite_drainage_km_km2") is not None:
                morceaux.append("densite de drainage %.2f km/km2" % bassins["densite_drainage_km_km2"])
            if bassins.get("aire_drainee_max_km2") is not None:
                morceaux.append("aire drainee maximale %.1f km2" % bassins["aire_drainee_max_km2"])
            if bassins.get("tci_moy") is not None:
                morceaux.append("indice d humidite topographique moyen %.1f" % bassins["tci_moy"])
            resultat["contexte_bassins"] = ", ".join(morceaux)
        if sols is not None and sols.get("repartition"):
            texte = ", ".join("%s %.0f %%" % (n, p * 100) for n, p in sols["repartition"] if p >= 0.03)
            if sols.get("argile_moy_pct") is not None:
                texte += " ; argile (0-5 cm) %.0f %%" % sols["argile_moy_pct"]
            if sols.get("impermeabilite") is not None:
                imp = sols["impermeabilite"]
                texte += " ; permeabilite " + ("bonne" if imp < 0.35 else ("moyenne" if imp < 0.65 else "faible"))
            resultat["contexte_sols"] = texte

        if occupation is not None and occupation.get("fractions"):
            resultat["contexte_occupation_sol"] = core_occ.resume_fractions(occupation["fractions"])
            resultat["contexte_source_occupation_sol"] = occupation["source"]

        if nom_aire:
            resultat["aire_protegee_proche"] = nom_aire
            resultat["aire_protegee_distance_km"] = round(distance_aire_km, 1) if distance_aire_km is not None else None

        # Contexte (informations, pas des risques notes) : population,
        # infrastructures sensibles, climat complementaire.
        if contexte_population_densite is not None:
            resultat["contexte_population_densite"] = contexte_population_densite
            resultat["contexte_source_population"] = contexte_source_population
        if contexte_distances_infra:
            groupes_trouves = {g: d for g, d in contexte_distances_infra.items() if d is not None}
            if groupes_trouves:
                resultat["contexte_distances_infra_km"] = groupes_trouves
        if climat_complementaire:
            for cle_ctx in ("vent_moyen_kmh", "humidite_moyenne_pct", "evapotranspiration_cumul_mm"):
                if climat_complementaire.get(cle_ctx) is not None:
                    resultat["contexte_" + cle_ctx] = climat_complementaire[cle_ctx]

        # Contexte multi-dates (evolution du bati / de la vegetation), si
        # les sous-classes correspondantes ont ete calculees.
        if taux_croissance_bati_pct_an is not None:
            resultat["contexte_taux_croissance_bati_pct_an"] = taux_croissance_bati_pct_an
            resultat["contexte_periode_bati"] = str(annee_debut) + "-" + str(annee)
        if variation_vegetation_pct is not None:
            resultat["contexte_variation_vegetation_pct"] = variation_vegetation_pct
            resultat["contexte_periode_vegetation"] = str(annee_debut) + "-" + str(annee)
        if distance_reseau_hydro_km is not None:
            resultat["contexte_distance_reseau_hydro_km"] = round(distance_reseau_hydro_km, 1)
        if distance_lac_km is not None:
            resultat["contexte_distance_lac_km"] = round(distance_lac_km, 1)
        if bilan_erosion_ha_an is not None:
            resultat["contexte_bilan_erosion_ha_an"] = round(bilan_erosion_ha_an, 2)
            resultat["contexte_periode_erosion"] = str(annee_debut) + "-" + str(annee)

        self.iface.messageBar().pushSuccess(
            "SN Analyser", nom + " (" + str(annee) + ") - " + " | ".join(resume_parts),
        )

        for p in fichiers_intermediaires:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

        pool_api.shutdown(wait=False)
        pool_osm.shutdown(wait=False)
        return resultat

    def _run_ire_analysis(self, widget, layer, feature, niveau, nom):
        """Pipeline IRE (Indice de Risque Environnemental) - PHASE 1.

        Score instantane (une seule annee = spinAnneeFin, contrairement aux
        3 autres indices qui comparent deux dates) combinant :
          - IDT (Degradation des Terres) : moyennes NDVI, NBR, BSI sur la zone
          - IU  (Urbanisation)           : moyenne NDBI sur la zone
        Un seul composite annuel multi-bandes (blue, red, nir, swir1, swir2)
        est telecharge, evitant 4 telechargements separes.

        IMPORTANT : Phase 1 uniquement (2 sous-indices sur 5 a terme). Le
        score ne reflete PAS encore le climat, la vulnerabilite ni la
        pollution - voir core/ire.py pour le detail.

        :return: dict (cles core.ire.calculer_ire + core.utils.INDICATOR_LABELS) ou None si echec.
        """
        from qgis.core import (
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsGeometry,
        )
        from .core import landsat as core_landsat
        from .core import ndvi as core_ndvi
        from .core import ndbi as core_ndbi
        from .core import nbr as core_nbr
        from .core import bsi as core_bsi
        from .core import lst as core_lst
        from .core import vulnerabilite as core_vuln
        from .core import ire as core_ire
        from .core import utils as core_utils
        from .core import pollution as core_pollution
        from .core import precipitation as core_precip
        from .core import no2 as core_no2
        from .core import evi as core_evi
        from .core import savi as core_savi
        from .core import salinite as core_salinite
        from .core import aires_protegees as core_aires

        self.iface.messageBar().pushInfo("SN Analyser", "Analyse IRE lancee pour " + nom + "...")

        geom = QgsGeometry(feature.geometry())
        src_crs = layer.crs()
        dst_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        if src_crs != dst_crs:
            transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
            geom.transform(transform)

        bbox_rect = geom.boundingBox()
        bbox = [bbox_rect.xMinimum(), bbox_rect.yMinimum(), bbox_rect.xMaximum(), bbox_rect.yMaximum()]
        centroide = geom.centroid().asPoint()

        annee = widget.spinAnneeFin.value() if hasattr(widget, "spinAnneeFin") else 2026
        work_dir = core_utils.get_temp_dir(self.plugin_dir)
        prefix = nom.replace(" ", "_") + "_" + str(annee) + "_ire"

        # Un seul composite multi-bandes pour les 5 indices
        # (blue=1, red=2, nir=3, swir1=4, swir2=5, thermal1=6)
        extrait_path = os.path.join(work_dir, prefix + "_extrait.tif")
        try:
            composite = core_landsat.telecharger_composite_annuel(
                bbox, annee, ["blue", "red", "nir", "swir1", "swir2", "thermal1"], extrait_path,
                nuages_max=70, max_scenes=8,
            )
        except Exception as e:
            self._log_critical("Composite Landsat IRE echoue (" + str(annee) + ") : " + str(e))
            return None
        self._pulse()

        # Ecriture de la geometrie de decoupe (meme pattern que les autres pipelines)
        geojson_path = os.path.join(work_dir, prefix + "_zone.geojson")
        geometry_json_str = geom.asJson()
        feature_collection_str = (
            '{"type": "FeatureCollection", "features": [{"type": "Feature", '
            '"properties": {}, "geometry": ' + geometry_json_str + "}]}"
        )
        with open(geojson_path, "w", encoding="utf-8") as f:
            f.write(feature_collection_str)

        fichiers_intermediaires = [extrait_path]
        try:
            ndvi_path = os.path.join(work_dir, prefix + "_ndvi.tif")
            core_ndvi.calculer_ndvi_depuis_extrait(extrait_path, ndvi_path, idx_nir=3, idx_red=2)
            ndbi_path = os.path.join(work_dir, prefix + "_ndbi.tif")
            core_ndbi.calculer_ndbi_depuis_extrait(extrait_path, ndbi_path, idx_nir=3, idx_swir=4)
            nbr_path = os.path.join(work_dir, prefix + "_nbr.tif")
            core_nbr.calculer_nbr_depuis_extrait(extrait_path, nbr_path, idx_nir=3, idx_swir2=5)
            bsi_path = os.path.join(work_dir, prefix + "_bsi.tif")
            core_bsi.calculer_bsi_depuis_extrait(extrait_path, bsi_path, idx_blue=1, idx_red=2, idx_nir=3, idx_swir1=4)
            lst_path = os.path.join(work_dir, prefix + "_lst.tif")
            core_lst.calculer_lst_depuis_extrait(extrait_path, lst_path, idx_thermal=6)
            evi_path = os.path.join(work_dir, prefix + "_evi.tif")
            core_evi.calculer_evi_depuis_extrait(extrait_path, evi_path, idx_nir=3, idx_red=2, idx_blue=1)
            savi_path = os.path.join(work_dir, prefix + "_savi.tif")
            core_savi.calculer_savi_depuis_extrait(extrait_path, savi_path, idx_nir=3, idx_red=2)
            si_path = os.path.join(work_dir, prefix + "_si.tif")
            core_salinite.calculer_si_depuis_extrait(extrait_path, si_path, idx_blue=1, idx_red=2)

            moyennes = {}
            for cle, chemin in (("ndvi", ndvi_path), ("ndbi", ndbi_path), ("nbr", nbr_path),
                                 ("bsi", bsi_path), ("lst", lst_path), ("evi", evi_path),
                                 ("savi", savi_path), ("si", si_path)):
                clip_path = os.path.join(work_dir, prefix + "_" + cle + "_clip.tif")
                core_utils.clip_raster_to_geometry(chemin, geojson_path, clip_path, nodata=0.0)
                moyennes[cle] = core_utils.moyenne_raster(clip_path)
                fichiers_intermediaires += [chemin, clip_path]
        except Exception as e:
            self._log_critical("Calcul des indices IRE echoue (" + str(annee) + ") : " + str(e))
            return None

        self._pulse()

        # Vulnerabilite (IV) : densite population (raster WorldPop bundle) +
        # distance a l infrastructure sensible la plus proche (Overpass).
        # Degrade proprement (IV base sur la densite seule) si le raster
        # bundle est absent ou si Overpass echoue/timeout.
        mean_pop_density = None
        distances_infra_km = None
        source_population = None

        # PRIORITE 1 : recensement reel ANSD (RGPHAE 2023), si la zone est
        # couverte -- bien plus precis qu une estimation satellite (WorldPop).
        population_ansd = self._population_ansd(niveau, nom)
        if population_ansd is not None:
            try:
                from qgis.core import QgsDistanceArea
                d = QgsDistanceArea()
                d.setEllipsoid("WGS84")
                aire_km2 = d.measureArea(geom) / 1_000_000.0
                if aire_km2 > 0:
                    mean_pop_density = population_ansd / aire_km2
                    source_population = "ANSD RGPHAE 2023 (recensement reel, " + str(population_ansd) + " hab.)"
            except Exception as e:
                self._log_critical("Densite population IRE (ANSD) non calculee : " + str(e))

        # PRIORITE 2 (repli) : raster WorldPop bundle, si ANSD ne couvre pas
        # cette zone (niveau/nom absent de data/population_ansd_2023.json).
        raster_population = os.path.join(self.plugin_dir, "data", "sen_population_2020_1km.tif")
        if mean_pop_density is None and os.path.exists(raster_population):
            try:
                pop_clip_path = os.path.join(work_dir, prefix + "_population_clip.tif")
                mean_pop_density = core_vuln.moyenne_densite_population(
                    raster_population, geojson_path, pop_clip_path
                )
                source_population = "WorldPop 2020 (estimation raster 1km, ANSD non disponible pour cette zone)"
                fichiers_intermediaires.append(pop_clip_path)
            except Exception as e:
                self._log_critical("Densite population IRE (WorldPop) non calculee : " + str(e))

        distances_infra_km = None
        if mean_pop_density is not None:
            try:
                distances_infra_km = core_vuln.distances_infrastructures_par_categorie(
                    centroide.y(), centroide.x()
                )
            except Exception as e:
                self._log_critical("Distances infrastructures IRE non calculees (Overpass) : " + str(e))

        self._pulse()

        # Precipitation (complete IC) : Open-Meteo Historical Weather,
        # rapide (1 requete HTTP), ne bloque jamais le pipeline en cas
        # d echec (IC se degrade proprement a la LST seule, cf core/ire.py).
        total_precip_mm = None
        spi = None
        try:
            total_precip_mm, _d1, _d2 = core_precip.recuperer_precipitation_annuelle(
                centroide.y(), centroide.x(), annee
            )
        except Exception as e:
            self._log_critical("Precipitation IRE non calculee (Open-Meteo) : " + str(e))
        try:
            spi = core_precip.calculer_spi(centroide.y(), centroide.x(), annee)
        except Exception as e:
            self._log_critical("SPI IRE non calcule (Open-Meteo climatologie) : " + str(e))
        self._pulse()

        # Pollution NO2 (IP) : Open-Meteo par defaut (rapide, ~1s). Si
        # chkPollutionS5P est coche, on tente EN PLUS Sentinel-5P (lent,
        # plusieurs minutes) qui prend le pas sur Open-Meteo s il reussit
        # (mesure satellite directe, plus precise -- voir core/ire.calculer_ip).
        no2_openmeteo_ugm3 = None
        european_aqi = None
        try:
            no2_openmeteo_ugm3, details_qualite_air = core_pollution.recuperer_no2_open_meteo(
                centroide.y(), centroide.x()
            )
            if details_qualite_air:
                european_aqi = details_qualite_air.get("european_aqi")
        except Exception as e:
            self._log_critical("Pollution NO2 IRE non calculee (Open-Meteo) : " + str(e))
        self._pulse()

        no2_s5p_mol_m2 = None
        if hasattr(widget, "chkPollutionS5P") and widget.chkPollutionS5P.isChecked():
            self.iface.messageBar().pushInfo(
                "SN Analyser", "Telechargement Sentinel-5P (NO2) en cours, cela peut prendre plusieurs minutes..."
            )
            try:
                no2_s5p_mol_m2, _nb_pixels = core_no2.calculer_no2_moyen_zone(
                    bbox, annee, work_dir, pulse_callback=self._pulse
                )
            except Exception as e:
                self._log_critical("Pollution NO2 IRE non calculee (Sentinel-5P) : " + str(e))
        self._pulse()

        resultat = core_ire.calculer_ire(
            mean_ndvi=moyennes["ndvi"], mean_nbr=moyennes["nbr"],
            mean_bsi=moyennes["bsi"], mean_ndbi=moyennes["ndbi"],
            mean_lst=moyennes["lst"],
            mean_pop_density=mean_pop_density, distances_infra_km=distances_infra_km,
            total_precip_mm=total_precip_mm, spi=spi,
            european_aqi=european_aqi,
            no2_s5p_mol_m2=no2_s5p_mol_m2, no2_openmeteo_ugm3=no2_openmeteo_ugm3,
            mean_evi=moyennes["evi"], mean_savi=moyennes["savi"], mean_si=moyennes["si"],
            superficie_reboisee_ha=self._effort_reboisement_region(niveau, nom, feature),
        )

        # Aire protegee a proximite : information contextuelle (voir
        # core/aires_protegees.py), n influence pas le score IRE.
        try:
            trouvee, nom_aire, distance_aire_km = core_aires.zone_protegee_proche(
                centroide.y(), centroide.x()
            )
            if trouvee:
                resultat["aire_protegee_proche"] = nom_aire
                resultat["aire_protegee_distance_km"] = round(distance_aire_km, 1)
        except Exception as e:
            self._log_critical("Recherche aire protegee IRE non effectuee : " + str(e))
        if source_population is not None:
            resultat["iv_source_population"] = source_population

        self.iface.messageBar().pushSuccess(
            "SN Analyser",
            "IRE " + nom + " (" + str(annee) + ") : %.1f/100 (%s) - Phase %s."
            % (resultat["ire_score"], resultat["ire_niveau"], resultat["ire_phase"]),
        )

        # Nettoyage des fichiers intermediaires (composite + indices non decoupes + clips)
        for p in fichiers_intermediaires:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

        return resultat
