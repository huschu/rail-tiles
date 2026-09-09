"""
Tag classification and the emitted attribute schema.

Ported from pack-builder/bundle/build_bundle.py, extended for the tile pipeline:
raw numbers not band indices (bands live in the app), and a render key that keeps
a chain homogeneous in everything the map colours or filters.

Construction and proposed lines tag their attributes under a prefix
(construction:electrified, proposed:voltage, ...). `_get` reads the prefixed tag
for those lines and falls back to the plain tag, so a future line shows its
planned electrification, speed, gauge and protection while lifecycle still marks
it as not in service.
"""
import protection

# MUST stay identical to SpeedBand.bands in swift-app/SwiftApp/Style.swift.
# Used only to cap a chain at one band while merging; never emitted.
BAND_EDGES = [80, 120, 160, 200, 250, 300]

PASSENGER_KINDS = ("rail", "light_rail", "subway", "tram", "narrow_gauge",
                   "monorail", "funicular")
_KIND_KEYS = ("railway", "construction", "proposed", "disused", "abandoned",
              "razed", "preserved")

MPH_TO_KMH = 1.60934


def lifecycle(p):
    if p.get("railway") == "proposed":
        return "proposed"
    if p.get("construction") or p.get("railway") == "construction":
        return "construction"
    if p.get("railway:preserved") == "yes" or p.get("railway") == "preserved":
        return "preserved"
    if p.get("disused") not in (None, "no"):
        return "disused"
    if p.get("abandoned") not in (None, "no"):
        return "abandoned"
    if p.get("razed") not in (None, "no") or p.get("railway") == "razed":
        return "razed"
    return "present"


def _pfx(p):
    lc = lifecycle(p)
    return lc + ":" if lc in ("construction", "proposed") else ""


def _get(p, key):
    """Value of `key`, preferring the construction:/proposed: prefixed tag."""
    pfx = _pfx(p)
    if pfx:
        v = p.get(pfx + key)
        if v is not None:
            return v
    return p.get(key)


def kind_of(p):
    for key in _KIND_KEYS:
        v = p.get(key)
        if v in PASSENGER_KINDS:
            return v
    if p.get("railway") == "preserved" or p.get("railway:preserved") == "yes":
        return "rail"
    return None


def parse_speed(raw):
    """One maxspeed value -> km/h int, or None. Handles the mph unit (most UK
    railway speeds are tagged '60 mph'); a bare number is km/h per OSM default."""
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
    """Line speed in km/h, falling back to maxspeed:forward/:backward (higher)."""
    v = parse_speed(_get(p, "maxspeed"))
    if v is not None:
        return v
    dirs = [parse_speed(_get(p, "maxspeed:forward")), parse_speed(_get(p, "maxspeed:backward"))]
    dirs = [x for x in dirs if x is not None]
    return max(dirs) if dirs else None


def band_of(v):
    if v is None:
        return None
    for i, e in enumerate(BAND_EDGES):
        if v <= e:
            return i
    return len(BAND_EDGES)


def electrification_state(p):
    v = _get(p, "electrified")
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
    """Detailed electrification identity for the merge key only (not emitted)."""
    if electrification_state(p) in ("non_electrified", "unknown"):
        return electrification_state(p)
    v, f = first_num(_get(p, "voltage")), first_num(_get(p, "frequency"))
    if v is None:
        return "elec-unknown"
    return f"{'dc' if f == 0 else 'ac'}{int(v)}@{f}"


def gauges_of(p):
    g = _get(p, "gauge")
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


def radio_of(p):
    return _get(p, "railway:radio")


def traffic_mode_of(p):
    return _get(p, "railway:traffic_mode")


def protection_of(p):
    return protection.train_protection(p, _pfx(p))


def is_tunnel(p):
    v = _get(p, "tunnel")
    return bool(v) and v != "no"


def is_bridge(p):
    v = _get(p, "bridge")
    return bool(v) and v != "no"


def render_key(p):
    """Chains merge on the dimensions that must stay exact to draw one truthful
    colour AND that run in long stretches. Train protection, radio and traffic
    mode are deliberately excluded: keying on them would fragment low-zoom chains
    and defeat parallel collapse, so they ride as per-chain aggregates instead
    (dominant value over the members), exact on raw ways from z12 up."""
    return (kind_of(p), lifecycle(p), _get(p, "usage"), elec_system(p), gauge_class(p))


def is_service(p):
    """Service track (yard, siding, spur, crossover); present only from z12."""
    return bool(p.get("service"))


def keep(p):
    return kind_of(p) is not None


def emit_props(p, maxspeed, osm_id=None, src=None, tunnel=False, bridge=False,
               protection=None, radio=None, traffic_mode=None):
    """The MVT attribute record. The out-of-key aggregates (maxspeed, tunnel,
    bridge, protection, radio, traffic_mode) are passed in: on a chain they are
    aggregated over its members, on a raw way they are the way's own. The base
    fields come from p, which is homogeneous in them (they are in the key)."""
    rec = {
        "kind": kind_of(p),
        "lifecycle": lifecycle(p),
        "elec": electrification_state(p),
        "gauge_class": gauge_class(p),
    }
    usage = _get(p, "usage")
    if usage:
        rec["usage"] = usage
    if p.get("service"):
        rec["service"] = p["service"]
    if maxspeed is not None:
        rec["maxspeed"] = int(maxspeed)
    v, f = first_num(_get(p, "voltage")), first_num(_get(p, "frequency"))
    if v is not None:
        rec["voltage"] = v
    if f is not None:
        rec["frequency"] = f
    gmm = gauge_mm(p)
    if gmm is not None:
        rec["gauge_mm"] = gmm
    if radio:
        rec["radio"] = radio
    if traffic_mode:
        rec["traffic_mode"] = traffic_mode
    if protection and protection[0]:
        rec["train_protection"], rec["tp_rank"] = protection
    if tunnel:
        rec["tunnel"] = True
    if bridge:
        rec["bridge"] = True
    if p.get("name"):
        rec["name"] = p["name"]
    if p.get("ref"):
        rec["ref"] = p["ref"]
    if osm_id:
        rec["osm_id"] = osm_id
    if src:
        import json
        rec["src"] = json.dumps(src, separators=(",", ":"))
    return rec
