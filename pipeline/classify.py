"""
Tag classification and the emitted attribute schema.

Ported from pack-builder/bundle/build_bundle.py, extended for the tile pipeline:
raw numbers not band indices (bands live in the app), and a render key that keeps
a chain homogeneous in everything the map colours or filters.

Construction and proposed lines tag their attributes under a prefix
(construction:electrified, proposed:voltage, ...). `_get` reads the prefixed tag
for those lines and falls back to the plain tag, so a future line shows its
planned electrification, speed, gauge and protection while lifecycle still marks
it as not in service. A line under construction often keeps its planned values
under proposed: (the East Coast Rail Link's proposed:voltage), so those come
after construction: and before the plain tag.
"""
import colour
import routes
import protection

# MUST stay identical to SpeedBand.bands in swift-app/SwiftApp/Style.swift.
# Used only to cap a chain at one band while merging; never emitted.
BAND_EDGES = [80, 120, 160, 200, 250, 300]

PASSENGER_KINDS = ("rail", "light_rail", "subway", "tram", "narrow_gauge",
                   "monorail", "funicular")
# OSM tags a lifecycle line three ways: railway=<state> (value form),
# railway=rail + <state>=yes (base+flag), and <state>:railway=rail (prefix
# namespace). Razed is almost always the prefix form, so missing it dropped all
# razed track; the prefix form is common for disused/abandoned too.
LIFECYCLE_PREFIXES = ("construction", "proposed", "disused", "abandoned",
                      "razed", "preserved")

MPH_TO_KMH = 1.60934


def lifecycle(p):
    railway = p.get("railway")
    if railway == "proposed" or p.get("proposed:railway"):
        return "proposed"
    if railway == "construction" or p.get("construction") or p.get("construction:railway"):
        return "construction"
    if railway == "preserved" or p.get("railway:preserved") == "yes" or p.get("preserved:railway"):
        return "preserved"
    if railway == "disused" or p.get("disused") not in (None, "no") or p.get("disused:railway"):
        return "disused"
    if railway == "abandoned" or p.get("abandoned") not in (None, "no") or p.get("abandoned:railway"):
        return "abandoned"
    if railway == "razed" or p.get("razed") not in (None, "no") or p.get("razed:railway"):
        return "razed"
    return "present"


def _pfx(p):
    lc = lifecycle(p)
    return lc + ":" if lc in ("construction", "proposed") else ""


_VALUE_PREFIXES = {"construction": ("construction:", "proposed:"), "proposed": ("proposed:",)}


def _get(p, key):
    """Value of `key`, preferring the construction:/proposed: prefixed tag."""
    for pre in _VALUE_PREFIXES.get(lifecycle(p), ()):
        v = p.get(pre + key)
        if v is not None:
            return v
    return p.get(key)


def kind_of(p):
    v = p.get("railway")
    if v in PASSENGER_KINDS:
        return v
    # kind carried under a lifecycle namespace: construction=rail,
    # construction:railway=rail, razed:railway=rail, ...
    for pre in LIFECYCLE_PREFIXES:
        k = p.get(pre) or p.get(pre + ":railway")
        if k in PASSENGER_KINDS:
            return k
    # a lifecycle line with no finer kind (railway=razed, or a bare
    # <state>:railway=yes, or the preserved flag) is track: default to rail.
    if v in LIFECYCLE_PREFIXES or p.get("railway:preserved") == "yes":
        return "rail"
    if any(p.get(pre + ":railway") for pre in LIFECYCLE_PREFIXES):
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


ELECTRIFIED = ("contact_line", "rail", "third_rail", "4th_rail", "ground_level_power_supply")


def electrification_state(p):
    v = _get(p, "electrified")
    if v in ELECTRIFIED:
        return "electrified"
    if v == "construction":
        return "construction"
    # A line in service whose wire goes up is tagged either way:
    # electrified=construction, or electrified=no with construction:electrified.
    if lifecycle(p) == "present" and p.get("construction:electrified") in ELECTRIFIED:
        return "construction"
    if v == "no":
        return "non_electrified"
    return "unknown"


def supply(p):
    """Voltage and frequency, as numbers. While the wire goes up on a line in
    service, the future system is the one to show, and it is tagged under
    construction: or proposed: like a new line's."""
    def get(key):
        if lifecycle(p) == "present" and electrification_state(p) == "construction":
            for pre in _VALUE_PREFIXES["construction"]:
                v = p.get(pre + key)
                if v is not None:
                    return v
        return _get(p, key)
    return first_num(get("voltage")), first_num(get("frequency"))


def proposed_supply(p):
    """(voltage, frequency) of the electrification proposed for a line in
    service that has none yet, either may be None; None when nothing is
    proposed. Its own elec stays non_electrified or unknown."""
    if lifecycle(p) != "present" or electrification_state(p) not in ("non_electrified", "unknown"):
        return None
    if p.get("proposed:electrified") not in ELECTRIFIED:
        return None
    return first_num(p.get("proposed:voltage")), first_num(p.get("proposed:frequency"))


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
        plan = proposed_supply(p)
        return electrification_state(p) + (f"+proposed:{plan}" if plan else "")
    v, f = supply(p)
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


# Set by build_region from routes.way_colours; never an OSM key.
ROUTE_COLOUR = "_route_colour"
# Set by build_region from routes.way_routes; never an OSM key.
ROUTE_SET = "_route_set"


def colour_of(p):
    """The way's own colour tag, else the colour of the one service on it."""
    own = colour.parse(_get(p, "colour") or _get(p, "color"))
    return own if own is not None else p.get(ROUTE_COLOUR)


def protection_of(p):
    return protection.train_protection(p, _pfx(p))


STAGES = ("construction", "proposed")


def _change(p, read, current):
    """(stage, value) of a change to a line in service: the value tagged under
    construction:, else under proposed:, when it differs from the current
    one. None for lines not in service, which show their future values
    through the lifecycle prefixes already."""
    if lifecycle(p) != "present":
        return None
    for stage in STAGES:
        v = read(stage + ":")
        if v is not None:
            return (stage, v) if v != current else None
    return None


def speed_change(p):
    def read(pre):
        v = parse_speed(p.get(pre + "maxspeed"))
        if v is not None:
            return v
        dirs = [parse_speed(p.get(pre + "maxspeed:forward")), parse_speed(p.get(pre + "maxspeed:backward"))]
        dirs = [x for x in dirs if x is not None]
        return max(dirs) if dirs else None
    return _change(p, read, speed_of(p))


def gauge_change(p):
    def read(pre):
        g = p.get(pre + "gauge")
        gs = [x.strip() for x in str(g).split(";") if x.strip()] if g else []
        return gs or None
    return _change(p, read, gauges_of(p))


def protection_change(p):
    """(stage, (system, rank)) of a train protection system being installed
    or proposed on a line in service."""
    def read(pre):
        hit = protection.train_protection(p, pre)
        return hit if hit[0] not in (None, "none") else None
    change = _change(p, read, None)
    if change and change[1][0] == protection_of(p)[0]:
        return None
    return change


def change_key(p):
    """What the map draws of the changes, for the merge key: a chain must not
    run from a stretch with a change onto one without, or across a change
    to a different speed band, gauge class or system."""
    sc, gc, pc = speed_change(p), gauge_change(p), protection_change(p)
    return (sc and (sc[0], band_of(sc[1])),
            gc and (gc[0], "multi" if len(gc[1]) > 1 else gc[1][0]),
            pc and (pc[0], pc[1][0]))


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
    return (kind_of(p), lifecycle(p), _get(p, "usage"), elec_system(p), gauge_class(p), change_key(p))


def is_service(p):
    """Service track (yard, siding, spur, crossover); present only from z12."""
    return bool(p.get("service"))


def keep(p):
    return kind_of(p) is not None


def emit_props(p, maxspeed, osm_id=None, src=None, tunnel=False, bridge=False,
               protection=None, radio=None, traffic_mode=None, colour=None,
               absorbed=None):
    """The MVT attribute record. The out-of-key aggregates (maxspeed, tunnel,
    bridge, protection, radio, traffic_mode, colour) are passed in: on a chain
    they are aggregated over its members, on a raw way they are the way's own. The base
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
    v, f = supply(p)
    if v is not None:
        rec["voltage"] = v
    if f is not None:
        rec["frequency"] = f
    plan = proposed_supply(p)
    if plan:
        rec["elec_proposed"] = True
        if plan[0] is not None:
            rec["proposed_voltage"] = plan[0]
        if plan[1] is not None:
            rec["proposed_frequency"] = plan[1]
    gmm = gauge_mm(p)
    if gmm is not None:
        rec["gauge_mm"] = gmm
    gs = gauges_of(p)
    if len(gs) > 1:
        # Multi-gauge features carry their raw list so the client can colour and
        # label the real gauges. Like gauge_mm, this is the representative
        # member's value on a merged chain (render_key keys on gauge_class only).
        rec["gauge_mm_list"] = ";".join(gs)
    if colour == routes.SHARED:
        rec["colour_shared"] = True
    elif colour is not None:
        rec["colour"] = colour
    if radio:
        rec["radio"] = radio
    if traffic_mode:
        rec["traffic_mode"] = traffic_mode
    if protection and protection[0]:
        rec["train_protection"], rec["tp_rank"] = protection
    # Changes on a line in service; p is homogeneous in them (change_key).
    sc, gc, pc = speed_change(p), gauge_change(p), protection_change(p)
    if sc:
        rec[f"maxspeed_{sc[0]}"] = sc[1]
    if gc:
        rec[f"gauge_{gc[0]}"] = ";".join(gc[1])
    if pc:
        rec[f"tp_{pc[0]}"], rec["tp_change_rank"] = pc[1]
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
    if absorbed:
        # Ways a parallel collapse deleted beside this track, "w1;w2". Lets a
        # client find a route's track where only its neighbour is drawn.
        rec["absorbed"] = ";".join(absorbed)
    return rec
