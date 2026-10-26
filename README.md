# SN Analyser

Extension QGIS d'analyse de surface de l'environnement au Sénégal, par **ADIOUMA FALL-Géo**.

## Fonctionnalités
- **Zone d'étude** : région, commune ou zone dessinée librement (polygone, rectangle, sélection).
- **Analyse de risque environnemental** : climatique, pollution, dégradation des terres, hydrologique, urbanisation, biodiversité. Chaque risque est noté de 0 à 100, avec une carte de risque par pixel.
- **Imagerie** : Sentinel-2 L2A à 10 m (2017 et après) ou Landsat Collection 2 Level-2 (1984 et après).
- **Relief et hydrologie** : MNT Copernicus GLO-30, courbes de niveau, bassins versants et rivières MERIT-Hydro, types de sol SoilGrids.
- **Numérisation** : extraction d'entités OpenStreetMap sur une zone délimitée, vers un GeoPackage.
- **Rapports** : commentaire d'analyse automatique, export PDF et Word.

## Installation
Depuis QGIS : *Extensions > Installer/Gérer les extensions*, cocher « Afficher les extensions expérimentales », rechercher **SN Analyser**.

Les exports PDF et Word nécessitent : `python -m pip install reportlab python-docx` (OSGeo4W Shell sous Windows).

## Sources de données
Microsoft Planetary Computer (Sentinel-2, Landsat, ESA WorldCover, Copernicus DEM, Sentinel-5P), © contributeurs OpenStreetMap (ODbL), MERIT-Hydro / Global Watersheds (M. Heberger, mghydro.com), ISRIC SoilGrids, Open-Meteo, WorldPop, ANSD (RGPHAE 2023), geoBoundaries.

## Licence
GNU General Public License v3.0 — voir le fichier `LICENSE`.

## Contact
ADIOUMA FALL-Géo — mouridefalltouba@gmail.com
