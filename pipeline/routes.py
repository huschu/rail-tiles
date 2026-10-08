"""
Line colours from route relations.

Most line colours sit on the route relation (U1, RE 7, M10), not on the track.
A way takes a colour when every coloured service over it shows about the same
colour: one service, lines sharing a colour (M10 and 12), or near shades such
as the Ring's S41 and S42. Services without a colour do not count. A track
shared by clearly different colours is marked SHARED rather than showing one
of them at random. A colour tag on the way itself always wins.

A service is a route type, network and ref (or name): the two directions of
one line are separate relations but one service, while S1 in two cities is
two services.

The app highlights a line or service by matching its member ways against the
ways a tile feature was built from. Below z12 a feature is a chain of many
ways, so chains are also split where the set of relations over the track
changes (way_routes): otherwise one member way lights up the whole chain,
far past where the route turns off.
"""
import re
import subprocess

import colour

ROUTES = ("train", "subway", "light_rail", "tram", "monorail")

# Every relation the app can highlight: its services plus the infrastructure
# lines (route=railway, tracks). Must match the relations fetch_region.sh keeps.
HIGHLIGHTED = ROUTES + ("funicular", "railway", "tracks")

# Colours closer than this read as one line on the map.
SIMILAR = 25

# Stands in for a colour on track shared by clearly different lines. It rides
# through the per-segment attributes like a colour, so chains split where a
# shared stretch begins; the tiles carry it as `colour_shared`.
SHARED = -1

_ESC = re.compile(r"%([0-9a-fA-F]+)%")


def _unescape(s):
    return _ESC.sub(lambda m: chr(int(m.group(1), 16)), s)


def parse_relation(line):
    """One osmium OPL relation line as {rid, version, tags, ways ("w<id>"),
    nodes [(id, role)], members [("n<id>" | "w<id>" | "r<id>", role)]}, or
    None for any other line."""
    if not line.startswith("r"):
        return None
    rid = line[1:].split(" ", 1)[0]
    version, tags, ways, nodes, members = 0, {}, [], [], []
    for field in line.rstrip("\n").split(" "):
        if field.startswith("v") and field[1:].isdigit():
            version = int(field[1:])
        elif field.startswith("T") and len(field) > 1:
            for kv in field[1:].split(","):
                k, _, v = kv.partition("=")
                tags[_unescape(k)] = _unescape(v)
        elif field.startswith("M") and len(field) > 1:
            for m in field[1:].split(","):
                ref, _, role = m.partition("@")
                members.append((ref, _unescape(role)))
                if ref.startswith("w"):
                    ways.append(ref)
                elif ref.startswith("n"):
                    nodes.append((ref[1:], _unescape(role)))
    return {"rid": rid, "version": version, "tags": tags, "ways": ways, "nodes": nodes,
            "members": members}


def parse_opl(lines):
    """Relations from osmium OPL as (tags, way ids "w<id>", relation id)."""
    out = []
    for line in lines:
        r = parse_relation(line)
        if r is not None:
            out.append((r["tags"], r["ways"], r["rid"]))
    return out


def way_colours(relations):
    """{way id: 24-bit RGB, or SHARED} for ways with at least one coloured service."""
    services = {}                       # service -> set of parsed colours
    on_way = {}                         # way -> set of services
    for tags, ways, *_ in relations:
        route = tags.get("route")
        if route not in ROUTES:
            continue
        name = tags.get("ref") or tags.get("name")
        if not name:
            continue
        service = (route, tags.get("network") or tags.get("operator") or "", name)
        rgb = colour.parse(tags.get("colour") or tags.get("color"))
        services.setdefault(service, set())
        if rgb is not None:
            services[service].add(rgb)
        for w in ways:
            on_way.setdefault(w, set()).add(service)
    out = {}
    for w, svc in on_way.items():
        # A direction without a colour tag defers to the other; a service whose
        # directions disagree is ambiguous and leaves the way uncoloured.
        if any(len(services[s]) > 1 for s in svc):
            continue
        coloured = sorted((s, next(iter(services[s]))) for s in svc if services[s])
        if not coloured:
            continue
        rgbs = [rgb for _, rgb in coloured]
        if any(colour.delta_e(a, b) >= SIMILAR for a in rgbs for b in rgbs):
            out[w] = SHARED
            continue
        # The colour most of the services use; ties go to the first service in
        # sort order, so every way of the Ring shows the same one.
        out[w] = max(rgbs, key=lambda rgb: (rgbs.count(rgb), -rgbs.index(rgb)))
    return out


def way_routes(relations):
    """{way id: frozenset of the highlighted relation ids over it}. Ways on no
    such relation are absent."""
    out = {}
    for tags, ways, rid in relations:
        if tags.get("route") not in HIGHLIGHTED:
            continue
        for w in ways:
            out.setdefault(w, set()).add(rid)
    return {w: frozenset(ids) for w, ids in out.items()}


def read_opl(pbf):
    """The relation lines of the filtered extract, as osmium OPL."""
    r = subprocess.run(["osmium", "cat", pbf, "-t", "relation", "-f", "opl", "-o", "-"],
                       check=True, capture_output=True, text=True)
    return r.stdout.splitlines()


def load(lines):
    """Route colours and route membership for the relations kept in the
    filtered extract."""
    relations = parse_opl(lines)
    return way_colours(relations), way_routes(relations)
