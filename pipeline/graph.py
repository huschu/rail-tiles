"""
Track graph for finding a path between two stations.

The tiles cannot serve as one: the export drops node ids, simplification drops
vertices and tiles clip ways, so junctions would have to be guessed from
coordinates. This reads the filtered extract with node ids instead.

A junction is a node two or more kept ways share, a way's end, or a stop
position (so a trip can start or end at a platform). An edge is the piece of
one way between two junctions, carrying the way id, its vertex range in the
way, its length and the classification the tiles use.

Trains cannot turn sharply at a junction, so the search runs over directed
edges (search.py) and needs the heading at each end of an edge. A heading is
read over HEADING_M of track, not the last segment alone, which is often a few
centimetres long at a switch.

The file the app reads (write) is one zstd frame of little-endian arrays,
fixed width so the app copies them out without parsing:

  "RLTG", u32 version, u32 junctions N, edges E, inner vertices V, stations S
  vocabularies: kind, lifecycle, usage, service, each u8 count + u8-prefixed
                UTF-8 strings; index 0 is "", no value
  junctions, sorted by node id: i64 node id, i32 lon, i32 lat (1e-7 degrees),
                u8 flags (1 = stop position)
  edges: i64 way, i32 a, i32 b, i32 i0, i32 i1, f32 length (m), u8 kind,
         lifecycle, usage, service, u16 maxspeed (0 unknown), f32 heading
         leaving a, f32 heading leaving b, i32 first inner vertex
         (E + 1 offsets)
  inner vertices: i32 lon, i32 lat
  stations: u8-prefixed id, u16-prefixed name, u8-prefixed kind, i32 lon,
            i32 lat, u16 count, i32 junctions; every station a train stops at
            (TRAIN_STATIONS), with or without stop positions on track

Usage: graph.py IN-rail.osm.pbf OUT.rltg.zst
"""
import math
import struct
import subprocess
import sys
from array import array
from compression import zstd

import classify as C
import routes
import stations as ST

K = 111320.0  # metres per degree latitude

HEADING_M = 20.0

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
    """(way id, tags, [(node id, lon, lat)]); a node without a location ends
    the way there."""
    fields = line.rstrip("\n").split(" ")
    wid, tags, nodes = int(fields[0][1:]), {}, []
    for f in fields[1:]:
        if f.startswith("T") and len(f) > 1:
            tags = _tags(f)
        elif f.startswith("N") and len(f) > 1:
            for ref in f[1:].split(","):
                nid, _, loc = ref[1:].partition("x")
                lon, _, lat = loc.partition("y")
                if not lon or not lat:
                    break
                nodes.append((int(nid), float(lon), float(lat)))
    return wid, tags, nodes


def _node(line):
    fields = line.rstrip("\n").split(" ")
    tags = next((_tags(f) for f in fields[1:] if f.startswith("T") and len(f) > 1), {})
    lon = lat = None
    for f in fields[1:]:
        if f.startswith("x") and len(f) > 1:
            lon = float(f[1:])
        elif f.startswith("y") and len(f) > 1:
            lat = float(f[1:])
    return int(fields[0][1:]), tags, lon, lat


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
    long."""
    lon0, lat0 = pts[0]
    d = 0.0
    for i in range(1, len(pts)):
        d += metres(*pts[i - 1], *pts[i])
        if d >= HEADING_M:
            break
    return bearing(lon0, lat0, *pts[i])


class Graph:
    """Junctions and the edges between them.

    Edge e runs from `a[e]` to `b[e]` along way `way[e]`, vertices
    `i0[e]..i1[e]`. Directed edge 2e travels a to b, 2e+1 travels b to a.
    `head_out[d]` is the heading leaving its start, `head_in[d]` the heading
    arriving at its end, both in the direction of travel."""

    def __init__(self):
        self.node_index = {}
        self.node_id, self.lon, self.lat = [], [], []
        self.stop = []
        self.way, self.i0, self.i1, self.a, self.b, self.length = [], [], [], [], [], []
        self.kind, self.lifecycle, self.usage, self.service, self.maxspeed = [], [], [], [], []
        self.coords = []
        self.head_out, self.head_in = [], []
        self.out = []  # per node: directed edges leaving it
        self.way_props = {}
        self.relations = []
        self.stop_tags = {}

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

    def _edge(self, wid, props, nodes, i0, i1):
        e = len(self.way)
        pts = [(lon, lat) for _, lon, lat in nodes[i0:i1 + 1]]
        a = self._junction(*nodes[i0])
        b = self._junction(*nodes[i1])
        self.way.append(wid)
        self.i0.append(i0)
        self.i1.append(i1)
        self.a.append(a)
        self.b.append(b)
        self.length.append(sum(metres(*pts[k], *pts[k + 1]) for k in range(len(pts) - 1)))
        self.kind.append(C.kind_of(props))
        self.lifecycle.append(C.lifecycle(props))
        self.usage.append(props.get("usage"))
        self.service.append(props.get("service"))
        self.maxspeed.append(C.speed_of(props))
        self.coords.append(pts)
        rev = pts[::-1]
        fwd_out, rev_out = _heading_out(pts), _heading_out(rev)
        self.head_out += [fwd_out, rev_out]
        self.head_in += [(rev_out + 180.0) % 360.0, (fwd_out + 180.0) % 360.0]
        self.out[a].append(2 * e)
        self.out[b].append(2 * e + 1)

    @property
    def edge_count(self):
        return len(self.way)

    def tail(self, d):
        e = d >> 1
        return self.b[e] if d & 1 else self.a[e]

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


def build(lines):
    """The graph of an extract's OPL lines (read_opl)."""
    ways, stops = [], {}
    g = Graph()
    for line in lines:
        t = line[:1]
        if t == "w":
            wid, tags, nodes = _way(line)
            if len(nodes) >= 2 and C.keep(tags) and C.lifecycle(tags) not in NO_TRACK:
                ways.append((wid, tags, nodes))
        elif t == "n":
            nid, tags, lon, lat = _node(line)
            if is_stop(tags):
                stops[nid] = tags
        elif t == "r":
            r = routes.parse_relation(line)
            if r is not None:
                g.relations.append(r)

    uses = {}
    for _, _, nodes in ways:
        for nid, _, _ in nodes:
            uses[nid] = uses.get(nid, 0) + 1
    for wid, tags, nodes in ways:
        g.way_props[wid] = tags
        last = 0
        for i in range(1, len(nodes)):
            nid = nodes[i][0]
            if i == len(nodes) - 1 or uses[nid] > 1 or nid in stops:
                g._edge(wid, tags, nodes, last, i)
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
    The app keeps the junctions whose track suits the trip."""
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
    return {sid: sorted(js) for sid, js in out.items()}, stations


VERSION = 2

# Station kinds the app finds trips between.
TRAIN_STATIONS = ("train", "light_rail")


def _vocab(values):
    """Tag values by frequency, None first. Past 255 the rarest share
    "other": a free-form tag like service can take any value."""
    counts = {}
    for v in values:
        if v is not None:
            counts[v] = counts.get(v, 0) + 1
    words = [None] + sorted(counts, key=lambda v: -counts[v])
    if len(words) > 255:
        words = words[:254] + ["other"]
    code = {v: i for i, v in enumerate(words)}
    other = len(words) - 1
    return words, lambda v: code.get(v, other)


def _strings(words):
    b = bytearray([len(words)])
    for w in words:
        u = (w or "").encode()
        b += bytes([len(u)]) + u
    return b


def _arr(code, values):
    a = array(code, values)
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


def _e7(v):
    return round(v * 1e7)


def write(g, table, stations, path):
    """The graph and its stations as the app's file (module docstring)."""
    order = sorted(range(len(g.node_id)), key=g.node_id.__getitem__)
    new = [0] * len(order)
    for i, j in enumerate(order):
        new[j] = i
    E = g.edge_count
    vocabs = [_vocab(g.kind), _vocab(g.lifecycle), _vocab(g.usage), _vocab(g.service)]
    starts, vlon, vlat = [0], [], []
    for pts in g.coords:
        for lon, lat in pts[1:-1]:
            vlon.append(_e7(lon))
            vlat.append(_e7(lat))
        starts.append(len(vlon))
    b = bytearray(b"RLTG")
    listed = sorted(sid for sid, st in stations.items() if st["kind"] in TRAIN_STATIONS)
    b += struct.pack("<5I", VERSION, len(order), E, len(vlon), len(listed))
    for words, _ in vocabs:
        b += _strings(words)
    b += _arr("q", (g.node_id[j] for j in order))
    b += _arr("i", (_e7(g.lon[j]) for j in order))
    b += _arr("i", (_e7(g.lat[j]) for j in order))
    b += _arr("B", (1 if g.stop[j] else 0 for j in order))
    b += _arr("q", g.way)
    b += _arr("i", (new[j] for j in g.a))
    b += _arr("i", (new[j] for j in g.b))
    b += _arr("i", g.i0)
    b += _arr("i", g.i1)
    b += _arr("f", g.length)
    for values, (_, code) in zip((g.kind, g.lifecycle, g.usage, g.service), vocabs):
        b += _arr("B", (code(v) for v in values))
    b += _arr("H", (min(v or 0, 65535) for v in g.maxspeed))
    b += _arr("f", g.head_out[0::2])
    b += _arr("f", g.head_out[1::2])
    b += _arr("i", starts)
    b += _arr("i", vlon)
    b += _arr("i", vlat)
    for sid in listed:
        st = stations[sid]
        name = (st["name"] or "").encode()[:65535]
        kind = st["kind"].encode()
        js = table.get(sid, [])[:65535]
        b += bytes([len(sid)]) + sid.encode() + struct.pack("<H", len(name)) + name
        b += bytes([len(kind)]) + kind + struct.pack("<iiH", _e7(st["lon"]), _e7(st["lat"]), len(js))
        b += _arr("i", (new[j] for j in js))
    with open(path, "wb") as f:
        f.write(zstd.compress(bytes(b), level=19))
    return len(b)


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: graph.py IN-rail.osm.pbf OUT.rltg.zst")
    pbf, out = sys.argv[1:]
    g = build(read_opl(pbf))
    table, stations = station_stops(g, pbf)
    raw = write(g, table, stations, out)
    print(f"{len(g.node_id)} junctions, {g.edge_count} edges, {len(stations)} stations; "
          f"{raw / 1e6:.1f} MB raw")


if __name__ == "__main__":
    main()
