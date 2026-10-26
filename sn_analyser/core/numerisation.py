# -*- coding: utf-8 -*-
"""
core/numerisation.py
--------------------
Fonction "Numerisation" : extraction automatique d entites OpenStreetMap
(API Overpass) sur une zone delimitee par l utilisateur, decoupees a la zone
et enregistrees dans un GeoPackage (une couche par type d entite et par
geometrie : points / lignes / polygones).

Exceptions :
  - courbes de niveau : OSM n en contient quasiment pas -> generees a partir
    du MNT Copernicus GLO-30 (core.relief_hydro) ;
  - parcelles cadastrales : tres rarement saisies dans OSM au Senegal (la
    couche est souvent vide, ce qui est signale a l utilisateur).

Donnees (c) contributeurs OpenStreetMap, licence ODbL.
"""

import os
import json
import time
import requests

SERVEURS_OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


def _v(tags, cle):
    return (tags.get(cle) or "").lower()


def _oui(tags, cle):
    v = _v(tags, cle)
    return v not in ("", "no", "0", "false")


# ---------------------------------------------------------------------------
# Catalogue : categorie -> [(cle, libelle, description, requetes Overpass,
#                            filtre(tags) -> bool)]
# Les requetes sont ecrites sans emprise : "{b}" est remplace par la bbox.
# ---------------------------------------------------------------------------
CATALOGUE = [
    ("transport", "\U0001F6E4️ Réseaux de transport et infrastructures", [
        ("voies_ferrees", "Voies ferrées", "Lignes de train, tramway, métro",
         ['way["railway"~"^(rail|tram|subway|light_rail|narrow_gauge|monorail|funicular)$"]({b})'],
         lambda t: _v(t, "railway") in ("rail", "tram", "subway", "light_rail", "narrow_gauge", "monorail", "funicular")),
        ("pistes_sentiers", "Pistes et sentiers", "Chemins piétons, pistes cyclables, voies de randonnée",
         ['way["highway"~"^(path|footway|cycleway|track|bridleway|steps|pedestrian)$"]({b})',
          'relation["route"~"^(hiking|bicycle|foot)$"]({b})'],
         lambda t: _v(t, "highway") in ("path", "footway", "cycleway", "track", "bridleway", "steps", "pedestrian")
                   or _v(t, "route") in ("hiking", "bicycle", "foot")),
        ("ouvrages_art", "Ouvrages d'art", "Ponts, tunnels, barrages, viaducs",
         ['way["bridge"]["bridge"!="no"]({b})', 'nwr["man_made"="bridge"]({b})',
          'way["tunnel"]["tunnel"!="no"]({b})', 'nwr["waterway"~"^(dam|weir)$"]({b})'],
         lambda t: _oui(t, "bridge") or _v(t, "man_made") == "bridge" or _oui(t, "tunnel")
                   or _v(t, "waterway") in ("dam", "weir")),
    ]),
    ("hydro", "\U0001F4A7 Hydrographie (réseau hydraulique)", [
        ("cours_eau", "Cours d'eau", "Rivières, fleuves, ruisseaux (lignes et surfaces)",
         ['way["waterway"~"^(river|stream|brook|rapids|tidal_channel|riverbank)$"]({b})',
          'nwr["natural"="water"]["water"~"^(river|stream|oxbow|canal)$"]({b})'],
         lambda t: _v(t, "waterway") in ("river", "stream", "brook", "rapids", "tidal_channel", "riverbank")
                   or (_v(t, "natural") == "water" and _v(t, "water") in ("river", "stream", "oxbow"))),
        ("plans_eau", "Plans d'eau", "Lacs, étangs, mers, océans, bassins, trait de côte",
         ['nwr["natural"="water"]({b})', 'nwr["landuse"~"^(reservoir|basin|salt_pond)$"]({b})',
          'way["natural"="coastline"]({b})', 'nwr["natural"~"^(bay|strait)$"]({b})'],
         lambda t: (_v(t, "natural") == "water" and _v(t, "water") not in ("river", "stream", "oxbow", "canal", "ditch", "drain"))
                   or _v(t, "landuse") in ("reservoir", "basin", "salt_pond")
                   or _v(t, "natural") in ("coastline", "bay", "strait")),
        ("reseaux_artificiels", "Réseaux artificiels", "Canaux, fossés, conduites d'eau",
         ['way["waterway"~"^(canal|ditch|drain)$"]({b})',
          'way["man_made"="pipeline"]["substance"~"^(water|irrigation)$"]({b})',
          'nwr["man_made"~"^(water_works|water_tower|reservoir_covered|pumping_station)$"]({b})'],
         lambda t: _v(t, "waterway") in ("canal", "ditch", "drain")
                   or (_v(t, "man_made") == "pipeline" and _v(t, "substance") in ("water", "irrigation"))
                   or _v(t, "man_made") in ("water_works", "water_tower", "reservoir_covered", "pumping_station")),
    ]),
    ("occupation", "\U0001F333 Occupation du sol et végétation", [
        ("zones_boisees", "Zones boisées", "Forêts, bois, bosquets, broussailles",
         ['nwr["landuse"="forest"]({b})', 'nwr["natural"~"^(wood|scrub|tree_row)$"]({b})'],
         lambda t: _v(t, "landuse") == "forest" or _v(t, "natural") in ("wood", "scrub", "tree_row")),
        ("zones_agricoles", "Zones agricoles", "Champs, cultures, vergers, vignes, prairies",
         ['nwr["landuse"~"^(farmland|orchard|vineyard|meadow|farmyard|plant_nursery|greenhouse_horticulture|allotments)$"]({b})'],
         lambda t: _v(t, "landuse") in ("farmland", "orchard", "vineyard", "meadow", "farmyard", "plant_nursery",
                                         "greenhouse_horticulture", "allotments")),
        ("espaces_verts", "Espaces verts", "Parcs urbains, jardins publics, pelouses",
         ['nwr["leisure"~"^(park|garden|playground)$"]({b})',
          'nwr["landuse"~"^(grass|recreation_ground|village_green)$"]({b})'],
         lambda t: _v(t, "leisure") in ("park", "garden", "playground")
                   or _v(t, "landuse") in ("grass", "recreation_ground", "village_green")),
        ("surfaces_minerales", "Surfaces minérales", "Zones rocheuses, plages, sable, carrières",
         ['nwr["natural"~"^(bare_rock|rock|scree|sand|beach|shingle|cliff|dune)$"]({b})',
          'nwr["landuse"="quarry"]({b})'],
         lambda t: _v(t, "natural") in ("bare_rock", "rock", "scree", "sand", "beach", "shingle", "cliff", "dune")
                   or _v(t, "landuse") == "quarry"),
    ]),
    ("limites", "\U0001F5FA️ Limites administratives et cadastre", [
        ("frontieres", "Frontières", "Limites d'États, régions, départements, communes",
         ['rel["boundary"="administrative"]["admin_level"~"^[2-8]$"]({b})->.r; way(r.r)({b})',
          'way["boundary"="administrative"]({b})'],
         lambda t: _v(t, "boundary") == "administrative" or t.get("_membre_limite") == "1"),
        ("parcelles", "Parcelles", "Limites cadastrales (rarement saisies dans OSM au Sénégal)",
         ['nwr["boundary"~"^(lot|cadastral|plot)$"]({b})', 'nwr["place"="plot"]({b})'],
         lambda t: _v(t, "boundary") in ("lot", "cadastral", "plot") or _v(t, "place") == "plot"),
        ("zones_reglementaires", "Zones réglementaires", "Parcs nationaux, aires protégées, réserves",
         ['nwr["boundary"~"^(national_park|protected_area)$"]({b})', 'nwr["leisure"="nature_reserve"]({b})'],
         lambda t: _v(t, "boundary") in ("national_park", "protected_area") or _v(t, "leisure") == "nature_reserve"),
    ]),
    ("reseaux", "⚡ Réseaux techniques (utilités)", [
        ("energie", "Énergie", "Lignes électriques, pylônes, postes, centrales, pipelines gaz/pétrole",
         ['nwr["power"~"^(line|minor_line|cable|tower|pole|substation|plant|generator|transformer)$"]({b})',
          'way["man_made"="pipeline"]["substance"!~"^(water|irrigation|sewage|wastewater)$"]({b})'],
         lambda t: _v(t, "power") in ("line", "minor_line", "cable", "tower", "pole", "substation", "plant", "generator", "transformer")
                   or (_v(t, "man_made") == "pipeline" and _v(t, "substance") not in ("water", "irrigation", "sewage", "wastewater"))),
        ("telecoms", "Télécoms", "Fibres optiques, antennes-relais, pylônes de communication",
         ['nwr["telecom"]({b})', 'nwr["communication:mobile_phone"]({b})',
          'nwr["man_made"~"^(mast|antenna)$"]({b})', 'nwr["man_made"="tower"]["tower:type"="communication"]({b})'],
         lambda t: bool(t.get("telecom")) or bool(t.get("communication:mobile_phone"))
                   or _v(t, "man_made") in ("mast", "antenna")
                   or (_v(t, "man_made") == "tower" and _v(t, "tower:type") == "communication")),
        ("assainissement", "Assainissement", "Canalisations d'eaux usées, stations d'épuration, regards",
         ['nwr["man_made"~"^(wastewater_plant|manhole)$"]({b})',
          'way["man_made"="pipeline"]["substance"~"^(sewage|wastewater)$"]({b})',
          'way["waterway"="drain"]["usage"~"^(sewage|wastewater)$"]({b})'],
         lambda t: _v(t, "man_made") in ("wastewater_plant", "manhole")
                   or (_v(t, "man_made") == "pipeline" and _v(t, "substance") in ("sewage", "wastewater"))),
    ]),
    ("relief", "⛰️ Altimétrie et relief", [
        ("courbes_niveau", "Courbes de niveau", "Générées depuis le MNT Copernicus 30 m (absentes d'OSM)",
         [], None),
        ("points_cotes", "Points cotés", "Sommets, collines, repères géodésiques, points d'altitude",
         ['node["natural"~"^(peak|hill|volcano|saddle)$"]({b})', 'node["man_made"="survey_point"]({b})',
          'node["ele"]({b})'],
         lambda t: _v(t, "natural") in ("peak", "hill", "volcano", "saddle") or _v(t, "man_made") == "survey_point"
                   or bool(t.get("ele"))),
    ]),
]

ITEMS = {cle: (cat, lib, desc, req, filtre)
         for cat, _libcat, items in CATALOGUE for cle, lib, desc, req, filtre in items}

# Couleurs (R,G,B) par type d entite, pour le chargement dans QGIS
COULEURS = {
    "voies_ferrees": (60, 60, 60), "pistes_sentiers": (160, 110, 60), "ouvrages_art": (200, 30, 30),
    "cours_eau": (30, 110, 220), "plans_eau": (90, 170, 240), "reseaux_artificiels": (0, 150, 170),
    "zones_boisees": (30, 120, 40), "zones_agricoles": (230, 200, 90), "espaces_verts": (120, 200, 90),
    "surfaces_minerales": (190, 170, 140), "frontieres": (130, 30, 130), "parcelles": (240, 120, 30),
    "zones_reglementaires": (0, 130, 90), "energie": (240, 180, 0), "telecoms": (150, 60, 200),
    "assainissement": (120, 80, 40), "courbes_niveau": (140, 90, 40), "points_cotes": (90, 50, 20),
}

# Cles OSM qui font d une voie fermee une SURFACE (sinon ligne)
_CLES_SURFACE = ("landuse", "natural", "leisure", "building", "amenity", "water", "area:highway", "place",
                 "boundary", "tourism", "aeroway")
_NATURAL_LIGNE = ("coastline", "tree_row", "cliff", "ridge", "arete")


def _est_surface(tags, ferme):
    if not ferme or _v(tags, "area") == "no":
        return False
    if _v(tags, "area") == "yes":
        return True
    if _v(tags, "natural") in _NATURAL_LIGNE:
        return False
    if _v(tags, "boundary") == "administrative":
        return False
    if _v(tags, "waterway") in ("riverbank", "dock", "dam", "boatyard"):
        return True
    if _v(tags, "man_made") in ("wastewater_plant", "water_works", "bridge", "reservoir_covered"):
        return True
    if _v(tags, "power") in ("substation", "plant", "generator"):
        return True
    return any(tags.get(k) for k in _CLES_SURFACE)


class Annulation(Exception):
    """Telechargement annule par l utilisateur."""


def construire_requete(cles, bbox, timeout=180):
    """Requete Overpass unique pour tous les types demandes (bbox WGS84)."""
    s, w, n, e = bbox[1], bbox[0], bbox[3], bbox[2]
    b = "%.6f,%.6f,%.6f,%.6f" % (s, w, n, e)
    morceaux = []
    for cle in cles:
        for r in ITEMS[cle][3]:
            morceaux.append(r.replace("{b}", b) + ";")
    if not morceaux:
        return None
    # timeout et maxsize declares au plus juste : les serveurs Overpass
    # servent en priorite les requetes qui annoncent peu de ressources.
    return "[out:json][timeout:%d][maxsize:268435456];(\n%s\n);out geom qt;" % (timeout, "\n".join(morceaux))


def _un_serveur(url, requete, delai):
    r = requests.post(url, data={"data": requete}, timeout=(15, delai),
                      headers={"User-Agent": "SN-Analyser-QGIS/1.0"})
    if r.status_code != 200:
        raise RuntimeError("%s : HTTP %d" % (url.split("/")[2], r.status_code))
    data = r.json()
    if data.get("remark") and not data.get("elements") and "error" in data["remark"].lower():
        raise RuntimeError("%s : %s" % (url.split("/")[2], data["remark"][:150]))
    return data


def telecharger_overpass(requete, delai=240, journal=None, annule=None):
    """Interroge TOUS les serveurs Overpass en parallele et garde la premiere
    reponse valide. annule() -> True interrompt immediatement l attente
    (les requetes en vol sont abandonnees)."""
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    erreurs = []
    for tour in range(2):
        pool = ThreadPoolExecutor(max_workers=len(SERVEURS_OVERPASS))
        futurs = {pool.submit(_un_serveur, url, requete, delai): url for url in SERVEURS_OVERPASS}
        restants = set(futurs)
        try:
            while restants:
                if annule and annule():
                    raise Annulation("telechargement annule")
                faits, restants = wait(restants, timeout=0.3, return_when=FIRST_COMPLETED)
                for fut in faits:
                    try:
                        data = fut.result()
                        if journal:
                            journal("Overpass : reponse de " + futurs[fut].split("/")[2])
                        return data
                    except Exception as exc:
                        erreurs.append(str(exc)[:120])
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        for _ in range(10):
            if annule and annule():
                raise Annulation("telechargement annule")
            time.sleep(0.5)
    raise RuntimeError("Serveurs Overpass injoignables ou surcharges : " + " | ".join(erreurs[-4:]))

def _geom_way(el, ogr):
    pts = el.get("geometry") or []
    pts = [p for p in pts if p]
    if len(pts) < 2:
        return None, None
    ferme = len(pts) >= 4 and pts[0]["lat"] == pts[-1]["lat"] and pts[0]["lon"] == pts[-1]["lon"]
    if _est_surface(el.get("tags", {}), ferme):
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for p in pts:
            ring.AddPoint_2D(p["lon"], p["lat"])
        g = ogr.Geometry(ogr.wkbPolygon)
        g.AddGeometry(ring)
        return g, "polygones"
    g = ogr.Geometry(ogr.wkbLineString)
    for p in pts:
        g.AddPoint_2D(p["lon"], p["lat"])
    return g, "lignes"


def _geom_relation(el, ogr):
    tags = el.get("tags", {})
    membres = [m for m in el.get("members", []) if m.get("type") == "way" and m.get("geometry")]
    if not membres:
        return None, None
    if _v(tags, "type") in ("multipolygon", "boundary") and _v(tags, "boundary") != "administrative":
        exterieurs = ogr.Geometry(ogr.wkbGeometryCollection)
        for m in membres:
            if m.get("role", "outer") in ("outer", ""):
                ls = ogr.Geometry(ogr.wkbLineString)
                for p in m["geometry"]:
                    if p:
                        ls.AddPoint_2D(p["lon"], p["lat"])
                if ls.GetPointCount() >= 2:
                    exterieurs.AddGeometry(ls)
        try:
            poly = ogr.BuildPolygonFromEdges(exterieurs, 0, 1, 1e-7)
            if poly is not None and not poly.IsEmpty():
                return poly, "polygones"
        except Exception:  # nosec B110 - erreur non bloquante, ignoree volontairement
            pass  # nosec B110
    multi = ogr.Geometry(ogr.wkbMultiLineString)
    for m in membres:
        ls = ogr.Geometry(ogr.wkbLineString)
        for p in m["geometry"]:
            if p:
                ls.AddPoint_2D(p["lon"], p["lat"])
        if ls.GetPointCount() >= 2:
            multi.AddGeometry(ls)
    return (multi, "lignes") if multi.GetGeometryCount() else (None, None)


def _type_principal(tags):
    for k in ("railway", "highway", "waterway", "natural", "landuse", "leisure", "power", "man_made",
              "boundary", "telecom", "bridge", "tunnel", "route", "place", "water"):
        if tags.get(k):
            return k + "=" + tags[k]
    return ""


def extraire(data, cles, geojson_zone, sortie_gpkg, journal=None):
    """Repartit les elements Overpass par type demande, decoupe a la zone et
    ecrit le GeoPackage.

    :return: liste [(cle, geometrie, nom_couche, nb_entites)]
    """
    from osgeo import ogr, osr
    ogr.UseExceptions()
    zone_ds = ogr.Open(geojson_zone)
    zone = None
    for f in zone_ds.GetLayer():
        g = f.GetGeometryRef().Clone()
        zone = g if zone is None else zone.Union(g)
    zone_ds = None
    if zone is None:
        raise RuntimeError("zone vide")
    if not zone.IsValid():
        zone = zone.Buffer(0)

    # Membres des limites administratives (way(r.r) : voies sans etiquette)
    elements = data.get("elements", [])
    seaux = {}
    for el in elements:
        tags = dict(el.get("tags", {}))
        if el["type"] == "way" and not tags and "frontieres" in cles:
            tags = {"_membre_limite": "1", "boundary": "administrative"}
            el = dict(el, tags=tags)
        if el["type"] == "node":
            if "lat" not in el:
                continue
            g = ogr.Geometry(ogr.wkbPoint)
            g.AddPoint_2D(el["lon"], el["lat"])
            nature = "points"
        elif el["type"] == "way":
            g, nature = _geom_way(el, ogr)
        else:
            g, nature = _geom_relation(el, ogr)
        if g is None:
            continue
        for cle in cles:
            filtre = ITEMS[cle][4]
            if filtre is None or not filtre(tags):
                continue
            try:
                gz = g if g.Within(zone) else g.Intersection(zone)
            except Exception:
                try:
                    gz = g.Buffer(0).Intersection(zone)
                except Exception:  # nosec B112 - erreur non bloquante, ignoree volontairement
                    continue  # nosec B112
            if gz is None or gz.IsEmpty():
                continue
            seaux.setdefault((cle, nature), []).append((el, tags, gz))

    if os.path.exists(sortie_gpkg):
        os.remove(sortie_gpkg)
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(sortie_gpkg)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    types_geom = {"points": ogr.wkbMultiPoint, "lignes": ogr.wkbMultiLineString, "polygones": ogr.wkbMultiPolygon}
    resultat = []
    for (cle, nature), lst in sorted(seaux.items()):
        nom = cle + "_" + nature
        lyr = ds.CreateLayer(nom, srs, types_geom[nature], options=["SPATIAL_INDEX=YES"])
        for champ, t in (("osm_id", ogr.OFTString), ("nom", ogr.OFTString), ("type", ogr.OFTString),
                         ("etiquettes", ogr.OFTString)):
            lyr.CreateField(ogr.FieldDefn(champ, t))
        defn = lyr.GetLayerDefn()
        lyr.StartTransaction()
        n = 0
        vus = set()
        for el, tags, gz in lst:
            ident = el["type"][0] + str(el["id"])
            if ident in vus:
                continue
            vus.add(ident)
            gz = ogr.ForceTo(gz.Clone(), types_geom[nature])
            if gz is None or gz.IsEmpty():
                continue
            f = ogr.Feature(defn)
            f.SetField("osm_id", ident)
            f.SetField("nom", tags.get("name:fr") or tags.get("name") or "")
            f.SetField("type", _type_principal(tags))
            f.SetField("etiquettes", json.dumps({k: v for k, v in tags.items() if not k.startswith("_")},
                                                 ensure_ascii=False)[:2000])
            f.SetGeometry(gz)
            lyr.CreateFeature(f)
            n += 1
        lyr.CommitTransaction()
        resultat.append((cle, nature, nom, n))
    ds = None
    return resultat
