# -*- coding: utf-8 -*-
"""
SN Analyser - outil_zone.py
---------------------------
Outil de carte pour delimiter manuellement une zone d etude :
  - mode "polygone"  : clic gauche = ajouter un sommet, clic droit / Entree =
                       terminer, Retour arriere = retirer le dernier sommet,
                       Echap = abandonner.
  - mode "rectangle" : cliquer-glisser sur la carte.
La geometrie finale est transmise (avec le SCR du canevas) au callback, de
facon differee (QTimer) pour ne pas detruire l outil pendant son propre
gestionnaire d evenement.
"""

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsGeometry, QgsRectangle, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand


class OutilDelimitationZone(QgsMapTool):

    def __init__(self, canvas, mode, callback):
        super().__init__(canvas)
        self.canvas = canvas
        self.mode = mode
        self.callback = callback
        self.points = []
        self.depart = None
        self.rb = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rb.setColor(QColor(214, 39, 40, 220))
        self.rb.setFillColor(QColor(214, 39, 40, 45))
        self.rb.setWidth(2)
        self.setCursor(Qt.CrossCursor)

    # -- evenements souris -------------------------------------------------
    def canvasPressEvent(self, e):
        pt = self.toMapCoordinates(e.pos())
        if self.mode == "rectangle":
            if e.button() == Qt.LeftButton:
                self.depart = pt
            return
        if e.button() == Qt.LeftButton:
            self.points.append(pt)
            self._maj(pt)
        elif e.button() == Qt.RightButton:
            self._terminer()

    def canvasMoveEvent(self, e):
        pt = self.toMapCoordinates(e.pos())
        if self.mode == "rectangle":
            if self.depart is not None:
                self.rb.setToGeometry(QgsGeometry.fromRect(QgsRectangle(self.depart, pt)), None)
        elif self.points:
            self._maj(pt)

    def canvasReleaseEvent(self, e):
        if self.mode != "rectangle" or e.button() != Qt.LeftButton or self.depart is None:
            return
        rect = QgsRectangle(self.depart, self.toMapCoordinates(e.pos()))
        self.depart = None
        if rect.width() > 0 and rect.height() > 0:
            self._emettre(QgsGeometry.fromRect(rect))
        else:
            self._reset()

    # -- clavier -----------------------------------------------------------
    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.points = []
            self.depart = None
            self._reset()
            self.canvas.unsetMapTool(self)
        elif e.key() in (Qt.Key_Backspace, Qt.Key_Delete) and self.points:
            self.points.pop()
            self._maj(None)
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._terminer()

    # -- interne -------------------------------------------------------------
    def _reset(self):
        self.rb.reset(QgsWkbTypes.PolygonGeometry)

    def _maj(self, curseur):
        pts = list(self.points) + ([curseur] if curseur is not None else [])
        if len(pts) >= 2:
            self.rb.setToGeometry(QgsGeometry.fromPolygonXY([pts]), None)
        else:
            self._reset()

    def _terminer(self):
        if len(self.points) < 3:
            return
        geom = QgsGeometry.fromPolygonXY([self.points])
        self.points = []
        self._emettre(geom)

    def _emettre(self, geom):
        self._reset()
        crs = self.canvas.mapSettings().destinationCrs()
        cb = self.callback
        QTimer.singleShot(0, lambda: cb(geom, crs))

    def deactivate(self):
        self._reset()
        super().deactivate()
