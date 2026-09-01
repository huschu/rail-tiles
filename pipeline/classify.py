"""
Tag classification and the emitted attribute schema.

Ported from pack-builder/bundle/build_bundle.py (the bundled build), extended in
two ways for the tile pipeline:

  - It emits raw numbers, not band indices. Speed bands live in the app
    (SpeedBand.bands in Style.swift); the tiles carry the actual maxspeed. The
    band edges below exist only as a merge guard so a chain never straddles two
    of the colours the app will draw. They are never written to a feature.

  - render_key keeps the exact electrification system (ac15000@16.7 ...) so a
    chain is homogeneous in voltage/frequency and the single voltage/frequency
    it emits is truthful. gauge_class likewise.

The lean tile schema (see TILE-PIPELINE.md, "Tile schema"): render key plus
speed/voltage/frequency/gauge as numbers, plus osm id and name/ref for tap
identity. Deep fields (operator, owner, wikidata, protection, dates) are not
carried; a later per-way lookup can fetch them.
"""

# MUST stay identical to SpeedBand.bands in swift-app/SwiftApp/Style.swift.
# Used only to cap a chain at one band while merging; never emitted.
BAND_EDGES = [80, 120, 160, 200, 250, 300]

PASSENGER_KINDS = ("rail", "light_rail", "subway", "tram", "narrow_gauge", "monorail")


MPH_TO_KMH = 1.60934


def parse_speed(raw):
    """One maxspeed value -> km/h int, or None. Handles the mph unit (most UK
    railway speeds are tagged '60 mph'); a bare number is km/h per OSM default,
    so without this the whole UK parsed to None and rendered grey."""
    if not raw:
        return None
    for part in str(raw).split(";"):
        s = part.strip().lower().replace("<", "").replace(">", "").replace("~", "")
        mult = 1.0
        if "mph" in s:
            mult = MPH_TO_KMH
            s = s.replace("mph", "")
        else:
            for u in ("km/h", "kmh", "kph"):
                s = s.replace(u, "")
        s = s.strip()
        try:
            v = float(s)
        except ValueError:
            continue
        if v > 0:
            return int(round(v * mult))
    return None


def speed_of(p):
    """A way's line speed in km/h. Falls back to the directional tags
    (maxspeed:forward / :backward), taking the higher, when maxspeed is absent."""
    v = parse_speed(p.get("maxspeed"))
    if v is not None:
        return v
    dirs = [parse_speed(p.get("maxspeed:forward")), parse_speed(p.get("maxspeed:backward"))]
    dirs = [x for x in dirs if x is not None]
    return max(dirs) if dirs else None


def band_of(v):
    if v is None:
        return None
    for i, e in enumerate(BAND_EDGES):
        if v <= e:
            return i
    return len(BAND_EDGES)


def kind_of(p):
    for key in ("railway", "construction", "proposed", "disused"):
        v = p.get(key)
        if v in PASSENGER_KINDS:
            return v
    return None


def lifecycle(p):
    if p.get("railway") == "proposed":
        return "proposed"
    if p.get("construction") or p.get("railway") == "construction":
        return "construction"
    if p.get("disused") not in (None, "no"):
        return "disused"
    if p.get("abandoned") not in (None, "no"):
        return "abandoned"
    return "present"


def electrification_state(p):
    v = p.get("electrified")
    if v in ("contact_line", "rail", "third_rail", "4th_rail", "ground_level_power_supply"):
        return "electrified"
    if v == "no":
        return "non_electrified"
    if v == "construction":
        return "construction"
    return "unknown"


def first_num(raw):
    if not raw:
        return None
    try:
        return float(str(raw).split(";")[0])
    except ValueError:
        return None


def elec_system(p):
    """Detailed electrification identity for the merge key only (not emitted).
    A chain homogeneous in this is homogeneous in voltage and frequency."""
    if electrification_state(p) in ("non_electrified", "unknown"):
        return electrification_state(p)
    v, f = first_num(p.get("voltage")), first_num(p.get("frequency"))
    if v is None:
        return "elec-unknown"
    return f"{'dc' if f == 0 else 'ac'}{int(v)}@{f}"


def gauges_of(p):
    g = p.get("gauge")
    if not g:
        return []
    return [x.strip() for x in str(g).split(";") if x.strip()]


def gauge_class(p):
    gs = gauges_of(p)
    if len(gs) > 1:
        return "multi"
    if not gs:
        return "unknown"
    try:
        mm = int(float(gs[0]))
    except ValueError:
        return "other"
    if 1432 <= mm <= 1445:
        return "standard"
    return "narrow" if mm < 1432 else "broad"


def gauge_mm(p):
    gs = gauges_of(p)
    if len(gs) != 1:
        return None
    try:
        return int(float(gs[0]))
    except ValueError:
        return None


def render_key(p):
    """Chains must be homogeneous in everything the map can express, so one
    colour is truthful. Speed is excluded here and capped by the band rule."""
    return (kind_of(p), lifecycle(p), p.get("usage"), elec_system(p), gauge_class(p))


def is_service(p):
    """Service track (yard, siding, spur, crossover). The single most expensive
    category; present only from z12 up (rule 2)."""
    return bool(p.get("service"))


def keep(p):
    """A railway way we tile at all: any recognised kind."""
    return kind_of(p) is not None


def emit_props(p, maxspeed, osm_id=None, src=None):
    """The lean MVT attribute record. maxspeed is passed in because a chain
    takes its fastest member rather than any single way's tag."""
    rec = {
        "kind": kind_of(p),
        "lifecycle": lifecycle(p),
        "elec": electrification_state(p),
        "gauge_class": gauge_class(p),
    }
    usage = p.get("usage")
    service = p.get("service")
    if usage:
        rec["usage"] = usage
    if service:
        rec["service"] = service
    if maxspeed is not None:
        rec["maxspeed"] = int(maxspeed)
    v, f = first_num(p.get("voltage")), first_num(p.get("frequency"))
    if v is not None:
        rec["voltage"] = v
    if f is not None:
        rec["frequency"] = f
    gmm = gauge_mm(p)
    if gmm is not None:
        rec["gauge_mm"] = gmm
    if p.get("name"):
        rec["name"] = p["name"]
    if p.get("ref"):
        rec["ref"] = p["ref"]
    if osm_id:
        rec["osm_id"] = osm_id
    if src:
        # MVT attributes are scalar, so source spans ride as a JSON string:
        # [[osm_id, start_vertex, end_vertex], ...]. The app parses on tap.
        import json
        rec["src"] = json.dumps(src, separators=(",", ":"))
    return rec
