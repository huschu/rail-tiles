"""
Track graph for finding a path between two stations.

The tiles cannot serve as one: the export drops node ids, simplification drops
vertices and tiles clip ways, so junctions would have to be guessed from
coordinates. This reads the filtered extract with node ids instead.

A junction is a node two or more kept ways share, a way's end, or a stop
position (so a trip can start or end at a platform). An edge is the piece of
one way between two junctions, carrying the way id, its vertex range in the
way, its length and the classification the tiles use. Only track a train can
run on is kept: rail, narrow gauge and light rail in use, disused or being
built, and any track a train or light rail route runs over. Freight-only
track (railway:traffic_mode=freight) stays only where such a route runs, or
a search sends passenger trips through freight bypasses such as
Innsbruck's Inntaltunnel, which is faster than the line through the city. A station
without stop positions gets the nearest track node within HALT_TRACK_M as
one, so every station a train can reach has a junction to start from.

Trains cannot turn sharply at a junction, so the search runs over directed
edges (search.py) and needs the heading at each end of an edge. A heading is
read over HEADING_M of track, not the last segment alone, which is often a few
centimetres long at a switch.

The file the app reads (encode) holds only what a search needs. A junction
two pieces of track merely meet at, where a train can neither turn off nor
stop, is merged away: the file's edges run between the remaining junctions
and list the pieces of way they cover. Only a chunk's own track merges
(through), so an edge crossing a chunk border stays as it is in both chunks.
An edge's outline keeps a vertex wherever the track bends more than
OUTLINE_M off the straight line, which is what a trip's path draws and
keeps. Lengths are whole decimetres and headings 1/65536 of a turn, as the
graph holds them already, so the app's search costs what search.py's does.

One zstd frame of columns; v is an unsigned LEB128 varint, z a zigzag one:

  "RLTG", u32 version, v junctions N, edges E, pieces P, outline vertices V,
      classes C
  vocabularies: kind, lifecycle, usage, service, each v count + v-prefixed
      UTF-8 strings; index 0 is "", no value
  classes: C x (v kind, v lifecycle, v usage, v service, v maxspeed (km/h,
      0 unknown))
  junctions, sorted by node id: N x v node id less the one before; N x z
      lon, N x z lat (1e-7 degrees), each less the one before; N x u8 flags
      (1 = stop position)
  edges, sorted by a, then b: E x v a (junction index) less the edge
      before's, E x z b less a; E x u16 heading leaving a, E x u16 heading
      leaving b
      (1/65536 of a turn from north); E x v pieces; E x v outline vertices
  pieces, from a to b, edge after edge: P x z way id less the piece's
      before; P x v length (dm); P x v class
  outline, edge after edge, between a and b and without them: V x z lon,
      V x z lat, each less the vertex before, a's position first

Stations go in a file of their own (station_file), so the app finds a
station by name before it loads the graph around it:

  "RLTS", u32 version, u32 stations S
  stations, sorted by id: u8-prefixed id, u16-prefixed name, u8 count of
            other names, each a u8-prefixed key ("en" for name:en, "int",
            "alt", "official") and a u16-prefixed name, u8-prefixed kind,
            i32 lon, i32 lat (1e-7 degrees), u16 count, i64 node ids of its
            stop positions, u32-prefixed services as the tiles' `routes`
            JSON; every station a train stops at (TRAIN_STATIONS), with or
            without stop positions on track

also one zstd frame. Names, positions and services are the map's stations
(stations.features); only the stop positions come from the graph.

CI builds the graph per region and writes a sidecar instead (--sidecar),
which graph_join.py merges across regions and cuts into chunks.

Usage: graph.py IN-rail.osm.pbf OUT.rltg.zst OUT.rlts.zst
       graph.py --sidecar IN-rail.osm.pbf OUT.graph.jsonl.zst
"""
import json
import math
import struct
import subprocess
import sys
from array import array

import classify as C
import routes
import stations as ST
from geometry import K, dp_keep

HEADING_M = 20.0
# A station without stop positions starts from track this close.
HALT_TRACK_M = 300.0
# How far an edge's outline may stray from the track.
OUTLINE_M = 5.0
# Routes whose ways stay in the graph whatever their kind, for a trip
# steered onto them.
TRAIN_ROUTES = ("train", "light_rail")

# No track left on the ground, or none yet.
NO_TRACK = ("razed", "proposed", "abandoned")


def read_opl(pbf):
    """The extract as osmium OPL with each way's node locations inline."""
    p = subprocess.Popen(
        ["osmium", "add-locations-to-ways", pbf, "--ignore-missing-nodes", "-f", "opl", "-o", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    yield from p.stdout
    if p.wait() != 0:
        raise RuntimeError(f"osmium add-locations-to-ways failed on {pbf}")


def _tags(field):
    out = {}
    for kv in field[1:].split(","):
        k, _, v = kv.partition("=")
        out[routes._unescape(k)] = routes._unescape(v)
    return out


def _way(line):
    """(way id, OSM version, tags, [(node id, lon, lat)], truncated); a node
    without a location ends the way there."""
    fields = line.rstrip("\n").split(" ")
    wid, version, tags, nodes, truncated = int(fields[0][1:]), 0, {}, [], False
    for f in fields[1:]:
        if f.startswith("v") and len(f) > 1:
            version = int(f[1:])
        elif f.startswith("T") and len(f) > 1:
            tags = _tags(f)
        elif f.startswith("N") and len(f) > 1:
            for ref in f[1:].split(","):
                nid, _, loc = ref[1:].partition("x")
                lon, _, lat = loc.partition("y")
                if not lon or not lat:
                    truncated = True
                    break
                nodes.append((int(nid), float(lon), float(lat)))
    return wid, version, tags, nodes, truncated


def _node(line):
    fields = line.rstrip("\n").split(" ")
    tags = next((_tags(f) for f in fields[1:] if f.startswith("T") and len(f) > 1), {})
    return int(fields[0][1:]), tags


def is_stop(tags):
    return tags.get("railway") == "stop" or tags.get("public_transport") == "stop_position"


def metres(lon1, lat1, lon2, lat2):
    c = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * K * c, (lat2 - lat1) * K)


def bearing(lon1, lat1, lon2, lat2):
    c = math.cos(math.radians((lat1 + lat2) / 2))
    return math.degrees(math.atan2((lon2 - lon1) * c, lat2 - lat1)) % 360.0


def _heading_out(pts):
    """Heading leaving pts[0] along pts, over HEADING_M where the edge is that
    long, to the file's 1/65536 of a turn."""
    lon0, lat0 = pts[0]
    d = 0.0
    for i in range(1, len(pts)):
        d += metres(*pts[i - 1], *pts[i])
        if d >= HEADING_M:
            break
    return round(bearing(lon0, lat0, *pts[i]) / 360.0 * 65536) % 65536 * 360.0 / 65536


def _length(pts):
    """Metres along pts, to the file's decimetre."""
    return round(sum(metres(*pts[k], *pts[k + 1]) for k in range(len(pts) - 1)), 1)


class Graph:
    """Junctions and the edges between them.

    Edge e runs from `a[e]` to `b[e]` along way `way[e]`, vertices
    `i0[e]..i1[e]`. Directed edge 2e travels a to b, 2e+1 travels b to a.
    `head_out[d]` is the heading leaving its start, `head_in(d)` the heading
    arriving at its end, both in the direction of travel."""

    def __init__(self):
        self.node_index = {}
        self.node_id, self.lon, self.lat = [], [], []
        self.stop = []
        self.way, self.i0, self.i1, self.a, self.b, self.length = [], [], [], [], [], []
        self.kind, self.lifecycle, self.usage, self.service, self.maxspeed = [], [], [], [], []
        self.coords = []
        self.head_out = []
        self.out = []  # per node: directed edges leaving it
        self.relations = []
        self.stop_tags = {}
        self.version = {}           # way id -> OSM version
        self.way_nodes = {}         # way id -> array of its node ids
        self.truncated = set()      # ways cut short at a node without a location
        self.pieces = {}            # edge -> [(way, length, class)] where it covers several ways

    def _junction(self, nid, lon, lat):
        j = self.node_index.get(nid)
        if j is None:
            j = self.node_index[nid] = len(self.node_id)
            self.node_id.append(nid)
            self.lon.append(lon)
            self.lat.append(lat)
            self.stop.append(False)
            self.out.append([])
        return j

    def add_edge(self, wid, cls, nodes, i0, i1):
        """The piece i0..i1 of way wid, whose nodes there are [(node id, lon,
        lat)] and whose classification is edge_class's."""
        e = len(self.way)
        pts = [(lon, lat) for _, lon, lat in nodes]
        a = self._junction(*nodes[0])
        b = self._junction(*nodes[-1])
        self.way.append(wid)
        self.i0.append(i0)
        self.i1.append(i1)
        self.a.append(a)
        self.b.append(b)
        self.length.append(_length(pts))
        kind, lifecycle, usage, service, maxspeed = cls
        self.kind.append(kind)
        self.lifecycle.append(lifecycle)
        self.usage.append(usage)
        self.service.append(service)
        self.maxspeed.append(maxspeed)
        self.coords.append(pts)
        self.head_out += [_heading_out(pts), _heading_out(pts[::-1])]
        self.out[a].append(2 * e)
        self.out[b].append(2 * e + 1)

    def add_chain(self, start, end, pieces, outline, heads):
        """An edge of the file (encode): its ends (node id, lon, lat), pieces
        [(way, length, class)], outline [(lon, lat)] from end to end and
        headings leaving each end."""
        e = len(self.way)
        a, b = self._junction(*start), self._junction(*end)
        kind, lifecycle, usage, service, maxspeed = pieces[0][2]
        self.way.append(pieces[0][0])
        self.i0.append(0)
        self.i1.append(0)
        self.a.append(a)
        self.b.append(b)
        self.length.append(sum(p[1] for p in pieces))
        self.kind.append(kind)
        self.lifecycle.append(lifecycle)
        self.usage.append(usage)
        self.service.append(service)
        self.maxspeed.append(maxspeed)
        self.coords.append(list(outline))
        self.head_out += list(heads)
        self.out[a].append(2 * e)
        self.out[b].append(2 * e + 1)
        if len(pieces) > 1:
            self.pieces[e] = pieces

    def split(self, e, k):
        """Cuts edge e at its k-th vertex into e and a new edge; returns the
        junction there."""
        pts, wid, i0, i1, b = self.coords[e], self.way[e], self.i0[e], self.i1[e], self.b[e]
        nid = self.way_nodes[wid][i0 + k]
        j = self._junction(nid, *pts[k])
        self.out[b].remove(2 * e + 1)
        self.b[e], self.i1[e], self.coords[e] = j, i0 + k, pts[:k + 1]
        self.length[e] = _length(pts[:k + 1])
        self.head_out[2 * e:2 * e + 2] = [_heading_out(pts[:k + 1]), _heading_out(pts[k::-1])]
        self.out[j].append(2 * e + 1)
        cls = (self.kind[e], self.lifecycle[e], self.usage[e], self.service[e], self.maxspeed[e])
        rest = [(nid, *pts[k])] + [(0, *p) for p in pts[k + 1:-1]] + [(self.node_id[b], *pts[-1])]
        self.add_edge(wid, cls, rest, i0 + k, i1)
        return j

    @property
    def edge_count(self):
        return len(self.way)

    def head_in(self, d):
        return (self.head_out[d ^ 1] + 180.0) % 360.0

    def head(self, d):
        e = d >> 1
        return self.a[e] if d & 1 else self.b[e]

    def path_coords(self, ds):
        out = []
        for d in ds:
            pts = self.coords[d >> 1]
            pts = pts[::-1] if d & 1 else pts
            out.extend(pts if not out else pts[1:])
        return out


def edge_class(props):
    """(kind, lifecycle, usage, service, maxspeed) of a way's tags."""
    return (C.kind_of(props), C.lifecycle(props), props.get("usage"), props.get("service"),
            C.speed_of(props))


def build(lines):
    """The graph of an extract's OPL lines (read_opl)."""
    import search as S
    found, stops = [], {}
    g = Graph()
    for line in lines:
        t = line[:1]
        if t == "w":
            wid, version, tags, nodes, truncated = _way(line)
            if len(nodes) >= 2 and C.keep(tags) and C.lifecycle(tags) not in NO_TRACK:
                freight = C.traffic_mode_of(tags) == "freight"
                found.append((wid, version, edge_class(tags), freight, nodes, truncated))
        elif t == "n" and "stop" in line:
            nid, tags = _node(line)
            if is_stop(tags):
                stops[nid] = tags
        elif t == "r":
            r = routes.parse_relation(line)
            if r is not None:
                g.relations.append(r)

    on_routes = {int(w[1:]) for r in g.relations if r["tags"].get("route") in TRAIN_ROUTES for w in r["ways"]}
    ways = []
    for wid, version, cls, freight, nodes, truncated in found:
        if (cls[0] in S.TRAIN_KINDS and cls[1] in S.LIFECYCLE and not freight) or wid in on_routes:
            ways.append((wid, cls, nodes))
            g.version[wid] = version
            g.way_nodes[wid] = array("q", (nid for nid, _, _ in nodes))
            if truncated:
                g.truncated.add(wid)
    uses = {}
    for _, _, nodes in ways:
        for nid, _, _ in nodes:
            uses[nid] = uses.get(nid, 0) + 1
    for wid, cls, nodes in ways:
        last = 0
        for i in range(1, len(nodes)):
            nid = nodes[i][0]
            if i == len(nodes) - 1 or uses[nid] > 1 or nid in stops:
                g.add_edge(wid, cls, nodes[last:i + 1], last, i)
                last = i
    for nid, tags in stops.items():
        j = g.node_index.get(nid)
        if j is not None:
            g.stop[j] = True
            g.stop_tags[nid] = tags
    return g


# Which station kinds a train on track of this kind stops at.
_ROUTE_OF_KIND = {"rail": "train", "narrow_gauge": "train", "light_rail": "light_rail",
                  "subway": "subway", "tram": "tram", "monorail": "monorail", "funicular": "funicular"}


def station_stops(g, pbf):
    """{station id: [junction]}: the stop positions on track at each station,
    keyed like the tiles' station ids (n<id>, w<id>, r<id>).

    A stop area ties its stop positions to the stations it holds; a stop area
    without a station goes to the nearest station within stations.NEAR_M of
    its stops, and a stop position no stop area holds to the nearest station
    within NEAR_M that serves its track, as stations.calls places services.
    A train station left without one takes the nearest track node within
    HALT_TRACK_M, cut into a junction and marked a stop, as a halt mapped
    as one node beside the line needs. The app keeps the junctions whose
    track suits the trip."""
    stations, _ = ST.read_points(pbf)
    grid = ST._Grid(stations.values())
    out = {}
    placed = set()
    for r in g.relations:
        if r["tags"].get("public_transport") != "stop_area":
            continue
        on_track = [g.node_index[int(m[1:])] for m, _ in r["members"]
                    if m.startswith("n") and int(m[1:]) in g.node_index]
        if not on_track:
            continue
        held = [m for m, _ in r["members"] if m in stations]
        if not held:
            lon = sum(g.lon[j] for j in on_track) / len(on_track)
            lat = sum(g.lat[j] for j in on_track) / len(on_track)
            held = [s["id"] for s, _ in grid.near(lon, lat, ST.NEAR_M)[:1]]
        for sid in held:
            out.setdefault(sid, set()).update(on_track)
            placed.update(on_track)
    for j, is_stop in enumerate(g.stop):
        if not is_stop or j in placed:
            continue
        kinds = {g.kind[d >> 1] for d in g.out[j]}
        accepts = set().union(*(ST._SERVES.get(_ROUTE_OF_KIND.get(k), set()) for k in kinds))
        for s, _ in grid.near(g.lon[j], g.lat[j], ST.NEAR_M):
            if s["kind"] in accepts:
                out.setdefault(s["id"], set()).add(j)
                break
    bare = [s for s in stations.values() if s["kind"] in TRAIN_STATIONS and s["id"] not in out]
    for sid, j in _track_beside(g, bare).items():
        g.stop[j] = True
        out[sid] = {j}
    return {sid: sorted(js) for sid, js in out.items()}


def _track_beside(g, stations):
    """{station id: junction} at the track node nearest each station within
    HALT_TRACK_M, edges cut where the node is inside one."""
    cell = 0.01
    grid = {}
    for e, pts in enumerate(g.coords):
        for k, (lon, lat) in enumerate(pts):
            grid.setdefault((int(math.floor(lon / cell)), int(math.floor(lat / cell))), []).append((e, k))
    picked = {}
    for s in stations:
        cx, cy = int(math.floor(s["lon"] / cell)), int(math.floor(s["lat"] / cell))
        reach = int(HALT_TRACK_M / (cell * K * max(math.cos(math.radians(s["lat"])), 0.1))) + 1
        best = None
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for e, k in grid.get((cx + dx, cy + dy), ()):
                    d = metres(s["lon"], s["lat"], *g.coords[e][k])
                    if d <= HALT_TRACK_M and (best is None or d < best[0]):
                        best = (d, e, k)
        if best is not None:
            picked[s["id"]] = best[1:]
    # Cut each edge from its far end back, so the nearer vertices keep their index.
    junction = {}
    for e, k in sorted(set(picked.values()), key=lambda ek: (ek[0], -ek[1])):
        last = len(g.coords[e]) - 1
        junction[e, k] = g.a[e] if k == 0 else g.b[e] if k == last else g.split(e, k)
    return {sid: junction[ek] for sid, ek in picked.items()}


VERSION = 5
STATIONS_VERSION = 2
SIDECAR_VERSION = 4

# Station kinds the app finds trips between.
TRAIN_STATIONS = ("train", "light_rail")


def _varint(b, v):
    while v >= 0x80:
        b.append(v & 0x7F | 0x80)
        v >>= 7
    b.append(v)


def _zigzag(b, v):
    _varint(b, v * 2 if v >= 0 else -v * 2 - 1)


def _column(values, put=_varint):
    b = bytearray()
    for v in values:
        put(b, v)
    return b


def _arr(code, values):
    a = array(code, values)
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


def _e7(v):
    return round(v * 1e7)


def write(raw, path):
    """raw as one zstd frame."""
    from compression import zstd
    with open(path, "wb") as f:
        f.write(zstd.compress(raw, level=19))


def chains(g):
    """The file's edges: [(a, b, [directed edges of g from a to b])], merged
    through every junction a train passes without a choice. Such a junction
    has two edges, no stop, a turn the train may take between them, and
    both edges inside its chunk. Each runs from its lower node id to its
    higher, so an edge in two chunks reads the same in both."""
    import search as S
    tile = [tile_of(lon, lat) for lon, lat in zip(g.lon, g.lat)]

    def through(j):
        if len(g.out[j]) != 2 or g.stop[j]:
            return False
        d1, d2 = g.out[j]
        e1, e2 = d1 >> 1, d2 >> 1
        if e1 == e2 or any(tile[g.a[e]] != tile[g.b[e]] for e in (e1, e2)):
            return False
        return S.turn(g.head_in(d1 ^ 1), g.head_out[d2]) <= S.MAX_TURN

    kept = [not through(j) for j in range(len(g.node_id))]
    used = [False] * g.edge_count
    out = []

    def walk(j):
        for d in g.out[j]:
            if used[d >> 1]:
                continue
            run = [d]
            used[d >> 1] = True
            v = g.head(d)
            while not kept[v]:
                d = next(x for x in g.out[v] if x != run[-1] ^ 1)
                run.append(d)
                used[d >> 1] = True
                v = g.head(d)
            if g.node_id[v] < g.node_id[j]:
                out.append((v, j, [x ^ 1 for x in reversed(run)]))
            else:
                out.append((j, v, run))

    for j in range(len(g.node_id)):
        if kept[j]:
            walk(j)
    # Track in a closed ring with no junction of its own.
    for e in range(g.edge_count):
        if not used[e]:
            kept[g.a[e]] = True
            walk(g.a[e])
    return out


def _outline(g, run):
    pts = []
    for d in run:
        p = g.coords[d >> 1]
        p = p[::-1] if d & 1 else p
        pts.extend(p if not pts else p[1:])
    c = math.cos(math.radians(pts[0][1]))
    keep = dp_keep([(lon * K * c, lat * K) for lon, lat in pts], OUTLINE_M)
    return [p for p, k in zip(pts[1:-1], keep[1:-1]) if k]


def encode(g):
    """The uncompressed graph file."""
    edges = chains(g)
    used = sorted({j for a, b, _ in edges for j in (a, b)}, key=g.node_id.__getitem__)
    new = {j: i for i, j in enumerate(used)}
    words = [[""], [""], [""], [""]]
    word_code = [{"": 0}, {"": 0}, {"": 0}, {"": 0}]
    classes, class_code = [], {}

    def class_of(e):
        key = (g.kind[e], g.lifecycle[e], g.usage[e], g.service[e], g.maxspeed[e] or 0)
        c = class_code.get(key)
        if c is None:
            codes = []
            for m, v in enumerate(key[:4]):
                v = v or ""
                if v not in word_code[m]:
                    word_code[m][v] = len(words[m])
                    words[m].append(v)
                codes.append(word_code[m][v])
            c = class_code[key] = len(classes)
            classes.append(codes + [min(key[4], 65535)])
        return c

    rows = []
    for a, b, run in edges:
        pieces = []
        for d in run:
            e = d >> 1
            piece = [g.way[e], round(g.length[e] * 10), class_of(e)]
            if pieces and pieces[-1][0] == piece[0] and pieces[-1][2] == piece[2]:
                pieces[-1][1] += piece[1]
            else:
                pieces.append(piece)
        rows.append((pieces[0][0], new[a], new[b], g.head_out[run[0]], g.head_out[run[-1] ^ 1],
                     pieces, _outline(g, run)))
    rows.sort(key=lambda r: (r[1], r[2], r[0]))

    b = bytearray(b"RLTG") + struct.pack("<I", VERSION)
    for v in (len(used), len(rows), sum(len(r[5]) for r in rows), sum(len(r[6]) for r in rows), len(classes)):
        _varint(b, v)
    for ws in words:
        _varint(b, len(ws))
        for w in ws:
            u = w.encode()
            _varint(b, len(u))
            b += u
    for c in classes:
        for v in c:
            _varint(b, v)
    b += _column(n - m for m, n in zip([0] + [g.node_id[j] for j in used], (g.node_id[j] for j in used)))
    for coord in (g.lon, g.lat):
        e7 = [_e7(coord[j]) for j in used]
        b += _column((n - m for m, n in zip([0] + e7, e7)), _zigzag)
    b += bytes(1 if g.stop[j] else 0 for j in used)
    b += _column(n - m for m, n in zip([0] + [r[1] for r in rows], (r[1] for r in rows)))
    b += _column((r[2] - r[1] for r in rows), _zigzag)
    b += _arr("H", (round(r[3] / 360.0 * 65536) % 65536 for r in rows))
    b += _arr("H", (round(r[4] / 360.0 * 65536) % 65536 for r in rows))
    b += _column(len(r[5]) for r in rows)
    b += _column(len(r[6]) for r in rows)
    ways = [p[0] for r in rows for p in r[5]]
    b += _column((n - m for m, n in zip([0] + ways, ways)), _zigzag)
    b += _column(p[1] for r in rows for p in r[5])
    b += _column(p[2] for r in rows for p in r[5])
    lons, lats = [], []
    for r in rows:
        x, y = _e7(g.lon[used[r[1]]]), _e7(g.lat[used[r[1]]])
        for lon, lat in r[6]:
            lons.append(_e7(lon) - x)
            lats.append(_e7(lat) - y)
            x, y = _e7(lon), _e7(lat)
    b += _column(lons, _zigzag)
    b += _column(lats, _zigzag)
    return bytes(b)


def station_file(features, stops):
    """The uncompressed station file of the map's station features and
    {station id: [stop node ids]}."""
    listed = sorted((f for f in features if f["properties"]["kind"] in TRAIN_STATIONS),
                    key=lambda f: f["properties"]["osm_id"])
    b = bytearray(b"RLTS")
    b += struct.pack("<2I", STATIONS_VERSION, len(listed))
    for f in listed:
        p = f["properties"]
        sid = p["osm_id"]
        lon, lat = f["geometry"]["coordinates"]
        name = (p.get("name") or "").encode()[:65535]
        kind = p["kind"].encode()
        ids = stops.get(sid, [])[:65535]
        routes_json = (p.get("routes") or "").encode()
        b += bytes([len(sid)]) + sid.encode() + struct.pack("<H", len(name)) + name
        others = [(k.encode()[:255], v.encode()[:65535]) for k, v in p.get("names", [])][:255]
        b.append(len(others))
        for k, v in others:
            b += bytes([len(k)]) + k + struct.pack("<H", len(v)) + v
        b += bytes([len(kind)]) + kind + struct.pack("<iiH", _e7(lon), _e7(lat), len(ids)) + _arr("q", ids)
        b += struct.pack("<I", len(routes_json)) + routes_json
    return bytes(b)


# Chunks are tiles at this zoom: about 100 km across in central Europe.
CHUNK_ZOOM = 8


def tile_of(lon, lat):
    n = 1 << CHUNK_ZOOM
    x = int((lon + 180.0) / 360.0 * n)
    s = math.sin(math.radians(max(min(lat, 85.0511), -85.0511)))
    y = int((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def deltas(values):
    """Each value less the one before, which zstd packs far tighter along a
    way, whose node ids and coordinates change little from one to the next."""
    return values[:1] + [b - a for a, b in zip(values, values[1:])]


def sidecar(g, table, path):
    """The region's graph for graph_join.py, as zstd JSON lines.

    Geofabrik extracts overlap at borders, and each cuts a way only where
    its own ways meet it, so the join recuts each way at every region's
    cuts. A way line is [way id, OSM version, class, lons, lats (1e-7),
    node ids, cuts, stops], with lons, lats and node ids as deltas: cuts
    are the node ids it is cut at here, stops those that are stop
    positions. Cuts go by node id, so they still apply
    to another region's newer version of the way. A way cut short at a node
    without a location has no cut where it was cut short.

    A station line is ["stops", station id, [node ids of its stop
    positions]]. The first line is ["sidecar", SIDECAR_VERSION]."""
    pieces = {}
    for e in range(g.edge_count):
        pieces.setdefault(g.way[e], []).append(e)
    proc = subprocess.Popen(["zstd", "-q", "-10", "-T0", "-f", "-o", path], stdin=subprocess.PIPE)
    out = proc.stdin

    def line(rec):
        out.write((json.dumps(rec, separators=(",", ":")) + "\n").encode())

    line(["sidecar", SIDECAR_VERSION])
    for wid, es in pieces.items():
        # A piece cut off at a halt comes last among the edges.
        es.sort(key=g.i0.__getitem__)
        lons, lats, cuts = [], [], []
        for e in es:
            pts = g.coords[e]
            for lon, lat in pts if not lons else pts[1:]:
                lons.append(_e7(lon))
                lats.append(_e7(lat))
            for j in (g.a[e], g.b[e]):
                if not cuts or cuts[-1] != j:
                    cuts.append(j)
        nids = g.way_nodes[wid].tolist()
        assert len(nids) == len(lons), f"way {wid}: pieces leave a gap"
        if wid in g.truncated:
            cuts.pop()
        e = es[0]
        cls = [g.kind[e], g.lifecycle[e], g.usage[e], g.service[e], g.maxspeed[e]]
        line([wid, g.version[wid], cls, deltas(lons), deltas(lats), deltas(nids), [g.node_id[j] for j in cuts],
              [g.node_id[j] for j in cuts if g.stop[j]]])
    for sid, js in table.items():
        line(["stops", sid, sorted(g.node_id[j] for j in js)])
    out.close()
    if proc.wait() != 0:
        raise RuntimeError(f"zstd failed writing {path}")


def main():
    args = sys.argv[1:]
    side = args[:1] == ["--sidecar"]
    if len(args) != 3:
        sys.exit("usage: graph.py IN-rail.osm.pbf OUT.rltg.zst OUT.rlts.zst\n"
                 "       graph.py --sidecar IN-rail.osm.pbf OUT.graph.jsonl.zst")
    g = build(read_opl(args[1 if side else 0]))
    if side:
        sidecar(g, station_stops(g, args[1]), args[2])
        size = ""
    else:
        pbf, graph_out, stations_out = args
        table = station_stops(g, pbf)
        raw = encode(g)
        write(raw, graph_out)
        stops = {sid: sorted(g.node_id[j] for j in js) for sid, js in table.items()}
        write(station_file(ST.features(pbf, routes.read_opl(pbf)), stops), stations_out)
        size = f"; {len(raw) / 1e6:.1f} MB raw"
    print(f"{len(g.node_id)} junctions, {g.edge_count} edges, {len(g.version)} ways{size}")


if __name__ == "__main__":
    main()
