"""
Stations and the services that call at them.

A station is a node tagged railway=station or railway=halt, or
public_transport=station for a rail mode (train, subway, light_rail, monorail,
funicular). A station mapped only as an area becomes a point at its centroid.
A railway=tram_stop node is a station of kind tram.

A route relation calls at a station when one of its stop or platform members
is in a public_transport=stop_area that holds the station, or is the station
itself. A stop the stop areas do not place goes to the nearest station of the
same mode within NEAR_M, using the stop position's own location. A stop area
that holds both a train and a metro station gives each only the routes of its
own mode, so the metro lines under a main-line station stay with the metro.

The tiles carry, per station, one entry per service: the two directions of a
line are separate relations but one service, grouped the way the app's detail
sheet groups them (route type, network, ref or name). Each entry lists its
relation ids; the app fetches the relations' members from the OSM API only
when one is opened.
"""
import json
import math
import os
import subprocess
import tempfile
from collections import defaultdict

import routes

SERVICES = ("train", "subway", "light_rail", "monorail", "funicular", "tram")
MODES = ("train", "subway", "light_rail", "monorail", "funicular")
KINDS = MODES + ("tram",)

# Order of services in a station's list: main-line first.
_ORDER = {r: i for i, r in enumerate(SERVICES)}

# A stop no stop area places counts for a station this close.
NEAR_M = 250

# Main-line stations from z9; halts and urban stations once a city fills the
# screen; tram stops, the densest, one zoom later.
STATION_MIN_ZOOM = 9
MINOR_MIN_ZOOM = 12
TRAM_MIN_ZOOM = 13
# The app draws z15 and z16 from the z14 points, which already place a
# station to about a metre; repeating them would add ~40% to the layer.
STATION_MAX_ZOOM = 14

# Which station kinds a route type calls at.
_SERVES = {
    "train": {"train"},
    "subway": {"subway"},
    "light_rail": {"light_rail", "subway", "train", "tram"},
    "monorail": {"monorail"},
    "funicular": {"funicular"},
    "tram": {"tram", "light_rail"},
}


def station_kind(tags):
    """The rail mode of a station, or None for anything else (bus stations,
    disused stations)."""
    railway = tags.get("railway")
    if railway == "tram_stop":
        return "tram"
    if railway not in ("station", "halt"):
        if tags.get("public_transport") != "station":
            return None
        if not any(tags.get(m) == "yes" for m in MODES):
            return None
    st = tags.get("station")
    if st in MODES:
        return st
    if tags.get("train") == "yes":
        return "train"
    for m in ("subway", "light_rail", "monorail", "funicular"):
        if tags.get(m) == "yes":
            return m
    return "train"


def _centroid(ring):
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def read_points(pbf):
    """Stations and stop positions of the filtered extract.

    Returns ({"n<id>": station}, {"n<id>": (lon, lat)}). A station is
    {id, name, kind, halt, lon, lat}."""
    with tempfile.TemporaryDirectory() as tmp:
        sub = os.path.join(tmp, "stations.osm.pbf")
        out = os.path.join(tmp, "stations.geojsonseq")
        subprocess.run(
            ["osmium", "tags-filter", pbf,
             "n/railway=station,halt,stop,tram_stop", "n/public_transport=station,stop_position",
             "w/railway=station", "w/public_transport=station",
             "-o", sub, "--overwrite"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            ["osmium", "export", sub, "-f", "geojsonseq",
             "--geometry-types=point,polygon", "--add-unique-id=type_id",
             "-o", out, "--overwrite"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stations, stops = {}, {}
        with open(out) as f:
            for line in f:
                line = line.strip().lstrip("\x1e")
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                oid = o.get("id") or ""
                p = o.get("properties") or {}
                g = o.get("geometry") or {}
                if g.get("type") == "Point":
                    lon, lat = g["coordinates"][:2]
                elif g.get("type") in ("Polygon", "MultiPolygon"):
                    ring = g["coordinates"][0] if g["type"] == "Polygon" else g["coordinates"][0][0]
                    if not ring:
                        continue
                    lon, lat = _centroid(ring)
                    # osmium names an area after the way it came from: a2N for
                    # way N, a2N+1 for relation N.
                    if oid.startswith("a"):
                        n = int(oid[1:])
                        oid = f"w{n // 2}" if n % 2 == 0 else f"r{n // 2}"
                else:
                    continue
                kind = station_kind(p)
                if kind is not None:
                    stations[oid] = {"id": oid, "name": p.get("name"), "kind": kind,
                                     "halt": p.get("railway") == "halt", "lon": lon, "lat": lat}
                elif oid.startswith("n"):
                    stops[oid] = (lon, lat)
    return _drop_area_duplicates(stations), stops


def _drop_area_duplicates(stations):
    """An area mapped around a station node it duplicates is dropped; the
    node is the feature routes and stop areas name. A nearby node of another
    name is a different station, as two termini across a square are."""
    nodes = [s for s in stations.values() if s["id"].startswith("n")]
    grid = _Grid(nodes)

    def same(a, b):
        return a["kind"] == b["kind"] and (not a["name"] or not b["name"]
                                           or a["name"].casefold() == b["name"].casefold())

    out = {}
    for sid, s in stations.items():
        if not sid.startswith("n"):
            if any(same(n, s) for n, _ in grid.near(s["lon"], s["lat"], NEAR_M)):
                continue
        out[sid] = s
    return out


class _Grid:
    """Stations bucketed by ~1 km cells for radius queries."""
    CELL = 0.01

    def __init__(self, stations):
        self.cells = defaultdict(list)
        for s in stations:
            self.cells[self._key(s["lon"], s["lat"])].append(s)

    def _key(self, lon, lat):
        return (int(math.floor(lon / self.CELL)), int(math.floor(lat / self.CELL)))

    def near(self, lon, lat, radius_m):
        """[(station, metres)] within radius, nearest first."""
        cx, cy = self._key(lon, lat)
        reach = int(radius_m / (self.CELL * 111320 * max(math.cos(math.radians(lat)), 0.1))) + 1
        out = []
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for s in self.cells.get((cx + dx, cy + dy), ()):
                    d = _metres(lon, lat, s["lon"], s["lat"])
                    if d <= radius_m:
                        out.append((s, d))
        out.sort(key=lambda t: t[1])
        return out


def _metres(lon0, lat0, lon1, lat1):
    k = 111320.0
    dx = (lon1 - lon0) * k * math.cos(math.radians((lat0 + lat1) / 2))
    dy = (lat1 - lat0) * k
    return math.hypot(dx, dy)


def service_title(tags):
    """The chip title the app shows: the refs, or the name when there is
    none. Matches DetailSheetRows.relationRows."""
    refs = [r.strip() for r in (tags.get("ref") or "").split(";") if r.strip()]
    if refs:
        return " / ".join(refs)
    name = (tags.get("name") or "").strip()
    return name or None


def calls(relations, stations, stops):
    """{station id: set of route relation ids} for the services calling at
    each station."""
    grid = _Grid(stations.values())
    by_member = defaultdict(set)              # member ref -> station ids
    for r in relations:
        if r["tags"].get("public_transport") != "stop_area":
            continue
        held = [m for m, _ in r["members"] if m in stations]
        if not held:
            # A stop area without its station node: the nearest station to
            # its located members, when one is close.
            located = [stops[m] for m, _ in r["members"] if m in stops]
            if located:
                lon = sum(p[0] for p in located) / len(located)
                lat = sum(p[1] for p in located) / len(located)
                held = [s["id"] for s, _ in grid.near(lon, lat, NEAR_M)[:1]]
        for m, _ in r["members"]:
            by_member[m].update(held)
    for sid in stations:
        by_member[sid].add(sid)

    out = defaultdict(set)
    for r in relations:
        route = r["tags"].get("route")
        if route not in SERVICES:
            continue
        accepts = _SERVES[route]
        hit = set()
        for m, role in r["members"]:
            if not (role.startswith("stop") or role.startswith("platform")):
                continue
            held = by_member.get(m)
            if held:
                fit = {s for s in held if stations[s]["kind"] in accepts}
                hit |= fit or held
            elif m in stops:
                lon, lat = stops[m]
                for s, _ in grid.near(lon, lat, NEAR_M):
                    if s["kind"] in accepts:
                        hit.add(s["id"])
                        break
        for s in hit:
            out[s].add(r["rid"])
    return out


def services(rids, rel_by_id):
    """One entry per service calling at a station, sorted main-line first:
    [route, title, colour or None, network or None, [relation ids]]."""
    groups = {}
    for rid in rids:
        tags = rel_by_id[rid]["tags"]
        title = service_title(tags)
        if title is None:
            continue
        route = tags["route"]
        network = (tags.get("network") or tags.get("operator") or "").strip() or None
        g = groups.setdefault((route, network or "", title),
                              {"route": route, "title": title, "network": network,
                               "colour": None, "ids": []})
        g["ids"].append(int(rid))
        if g["colour"] is None:
            g["colour"] = routes.colour.parse(tags.get("colour") or tags.get("color"))
    return _entries(groups)


def _entries(groups):
    """Service groups as sorted entries, main-line first, then by network and
    title."""
    entries = sorted(groups.values(), key=lambda g: (_ORDER[g["route"]], g["network"] is None,
                                                      g["network"] or "", _natural(g["title"])))
    return [[g["route"], g["title"], g["colour"], g["network"], sorted(g["ids"])] for g in entries]


def merge(feature_lists):
    """One feature per station across region extracts. Geofabrik extracts
    overlap at borders and each holds only the routes it contains, so a border
    station built in two regions lists the services of both."""
    merged = {}
    for features in feature_lists:
        for f in features:
            sid = f["properties"]["osm_id"]
            m = merged.get(sid)
            if m is None:
                merged[sid] = json.loads(json.dumps(f))
                continue
            m["tippecanoe"]["minzoom"] = min(m["tippecanoe"]["minzoom"], f["tippecanoe"]["minzoom"])
            if not m["properties"].get("name") and f["properties"].get("name"):
                m["properties"]["name"] = f["properties"]["name"]
            groups = {}
            for props in (m["properties"], f["properties"]):
                for route, title, colour, network, ids in json.loads(props.get("routes") or "[]"):
                    g = groups.setdefault((route, network or "", title),
                                          {"route": route, "title": title, "network": network,
                                           "colour": None, "ids": []})
                    g["ids"] = sorted(set(g["ids"]) | set(ids))
                    if g["colour"] is None:
                        g["colour"] = colour
            if groups:
                m["properties"]["routes"] = json.dumps(_entries(groups), ensure_ascii=False,
                                                       separators=(",", ":"))
    return list(merged.values())


def _natural(s):
    """Sort key putting C-2 before C-10."""
    out, num = [], ""
    for ch in s.lower() + "\0":
        if ch.isdigit():
            num += ch
            continue
        if num:
            out.append((0, int(num), ""))
            num = ""
        out.append((1, 0, ch))
    return out


def features(pbf, opl_lines):
    """GeoJSON point features, one per station, each with its own min zoom."""
    stations, stops = read_points(pbf)
    relations = [r for r in (routes.parse_relation(l) for l in opl_lines) if r is not None]
    rel_by_id = {r["rid"]: r for r in relations}
    served = calls(relations, stations, stops)
    out = []
    for sid, s in stations.items():
        entries = services(served.get(sid, ()), rel_by_id)
        props = {"osm_id": sid, "kind": s["kind"]}
        if s["name"]:
            props["name"] = s["name"]
        if s["halt"]:
            props["halt"] = True
        if entries:
            props["routes"] = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
        if s["kind"] == "train" and not s["halt"]:
            minzoom = STATION_MIN_ZOOM
        elif s["kind"] == "tram":
            minzoom = TRAM_MIN_ZOOM
        else:
            minzoom = MINOR_MIN_ZOOM
        out.append({"type": "Feature",
                    "tippecanoe": {"minzoom": minzoom},
                    "properties": props,
                    "geometry": {"type": "Point",
                                 "coordinates": [round(s["lon"], 6), round(s["lat"], 6)]}})
    return out
