#!/usr/bin/env python3
"""
Merge the regions' track graphs (graph.py --sidecar) into the planet's,
cut into chunks the app loads as a trip's search reaches them.

The chunks are the tiles of one zoom (graph.CHUNK_ZOOM) in a PMTiles
archive: tile type 0, each tile one zstd frame of graph.py's file holding
every edge with an end in the tile and no stations. An edge between two
chunks is in both, so loading the chunk a junction lies in gives every edge
at that junction. The app joins chunks by OSM node id and drops an edge it
already has by way and first vertex.

Regions overlap at borders, and each cuts a way only where its own ways
meet it, so a chunk recuts each way at every region's cuts. A neighbour's
branch onto a border way then finds a junction there.

Stations go in a file of their own, so the app finds any station by name
before it loads the chunk it is in:

  "RLTS", u32 version, u32 stations S
  stations, sorted by id: u8-prefixed id, u16-prefixed name, u8-prefixed
            kind, i32 lon, i32 lat (1e-7 degrees), u16 count, i64 node ids
            of its stop positions, u32-prefixed services as the tiles'
            `routes` JSON

all one zstd frame. A station in two regions lists the services and stop
positions of both.

Needs Python 3.14+ (compression.zstd).

Usage: graph_join.py REGIONS_DIR OUT_DIR TAG
Prints JSON {graph, stations, chunks} for the workflow to consume.
"""
import glob
import gzip
import json
import os
import struct
import sys
import tempfile
from compression import zstd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import graph as G
import stations as ST
from diff_tiles import zxy_to_tileid
from zstd_tiles import HEADER_LEN, ZSTD, directories

STATIONS_VERSION = 1
LEVEL = 19
ATTRIBUTION = '<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>'
# Way lines held in memory before they go to their chunk's file.
FLUSH_BYTES = 256 << 20


def split(sidecars, tmp):
    """Sends each way line to its chunk's file and merges the stations.
    Returns ({(x, y): path}, {station id: record})."""
    files, held, size = {}, {}, 0
    merged = {}

    def flush():
        for t, lines in held.items():
            path = files.setdefault(t, os.path.join(tmp, f"{t[0]}_{t[1]}.jsonl"))
            with open(path, "a") as f:
                f.writelines(lines)
        held.clear()

    for path in sidecars:
        with zstd.open(path, "rt") as f:
            for line in f:
                if line.startswith('["station"'):
                    _merge_station(merged, json.loads(line)[1])
                    continue
                t = tuple(json.loads(line[1:line.index("]") + 1]))
                held.setdefault(t, []).append(line)
                size += len(line)
                if size > FLUSH_BYTES:
                    flush()
                    size = 0
    flush()
    return files, merged


def _merge_station(merged, rec):
    m = merged.get(rec["id"])
    if m is None:
        merged[rec["id"]] = rec
        return
    if not m["name"] and rec["name"]:
        m["name"] = rec["name"]
    m["stops"] = sorted(set(m["stops"]) | set(rec["stops"]))
    groups = {}
    for routes in (m["routes"], rec["routes"]):
        for route, title, colour, network, ids in routes:
            g = groups.setdefault((route, network or "", title),
                                  {"route": route, "title": title, "network": network,
                                   "colour": None, "ids": []})
            g["ids"] = sorted(set(g["ids"]) | set(ids))
            if g["colour"] is None:
                g["colour"] = colour
    m["routes"] = ST._entries(groups)


def chunk(t, path):
    """The chunk at tile t from its way lines, and how many ways disagreed
    between regions."""
    versions = {}
    with open(path) as f:
        for line in f:
            _, wid, cls, lons, lats, cuts = json.loads(line)
            versions.setdefault(wid, []).append((cls, lons, lats, cuts))
    g = G.Graph()
    stops, conflicts = set(), 0
    for wid in sorted(versions):
        vs = versions[wid]
        cls, lons, lats, _ = max(vs, key=lambda v: len(v[1]))
        at = {}
        for _, _, _, cuts in vs:
            for i, nid, stop in cuts:
                if at.setdefault(i, nid) != nid:
                    conflicts += 1
                if stop:
                    stops.add(nid)
        if any(i >= len(lons) for i in at):
            # A region cut off the way's end at a node it lacked; the longest
            # version is the whole way, so its indices hold.
            at = {i: nid for i, nid in at.items() if i < len(lons)}
        nodes = [(at.get(i, 0), lon / 1e7, lat / 1e7) for i, (lon, lat) in enumerate(zip(lons, lats))]
        cut = sorted(at)
        for i0, i1 in zip(cut, cut[1:]):
            if G.tile_of(*nodes[i0][1:]) == t or G.tile_of(*nodes[i1][1:]) == t:
                g.add_edge(wid, tuple(cls), nodes, i0, i1)
    for nid in stops:
        j = g.node_index.get(nid)
        if j is not None:
            g.stop[j] = True
    return g, conflicts


def tile_bounds(x, y, z=G.CHUNK_ZOOM):
    import math
    n = 1 << z

    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


def write_archive(tiles, path):
    """tiles: [(tile id, (x, y), bytes)] as a PMTiles v3 archive, tile type 0,
    zstd tiles, one zoom."""
    tiles.sort()
    ents, data = [], bytearray()
    for tid, _, blob in tiles:
        ents.append((tid, 1, len(data), len(blob)))
        data += blob
    root, leaves = directories(ents)
    meta = gzip.compress(json.dumps({
        "name": "track-graph", "format": "rltg", "version": G.VERSION,
        "attribution": ATTRIBUTION,
    }).encode(), mtime=0)
    bounds = [tile_bounds(*xy) for _, xy, _ in tiles]
    w, s = min(b[0] for b in bounds), min(b[1] for b in bounds)
    e, n = max(b[2] for b in bounds), max(b[3] for b in bounds)
    head = bytearray(HEADER_LEN)
    head[:8] = b"PMTiles\x03"
    root_off = HEADER_LEN
    meta_off = root_off + len(root)
    leaf_off = meta_off + len(meta)
    data_off = leaf_off + len(leaves)
    struct.pack_into("<11Q", head, 8, root_off, len(root), meta_off, len(meta), leaf_off, len(leaves),
                     data_off, len(data), len(ents), len(ents), len(ents))
    head[96] = 1                     # clustered
    head[97] = 2                     # gzip directories and metadata
    head[98] = ZSTD
    head[99] = 0                     # tile type: other
    head[100] = head[101] = G.CHUNK_ZOOM
    struct.pack_into("<iiii", head, 102, *(round(v * 1e7) for v in (w, s, e, n)))
    head[118] = G.CHUNK_ZOOM
    struct.pack_into("<ii", head, 119, round((w + e) / 2 * 1e7), round((s + n) / 2 * 1e7))
    part = path + ".part"
    with open(part, "wb") as f:
        f.write(head); f.write(root); f.write(meta); f.write(leaves); f.write(data)
    os.replace(part, path)


def write_stations(merged, path):
    b = bytearray(b"RLTS")
    b += struct.pack("<2I", STATIONS_VERSION, len(merged))
    for sid in sorted(merged):
        st = merged[sid]
        name = (st["name"] or "").encode()[:65535]
        kind = st["kind"].encode()
        stops = st["stops"][:65535]
        b += bytes([len(sid)]) + sid.encode() + struct.pack("<H", len(name)) + name
        b += bytes([len(kind)]) + kind + struct.pack("<iiH", st["lon"], st["lat"], len(stops))
        b += struct.pack(f"<{len(stops)}q", *stops)
        routes = json.dumps(st["routes"], ensure_ascii=False, separators=(",", ":")).encode() if st["routes"] else b""
        b += struct.pack("<I", len(routes)) + routes
    with open(path, "wb") as f:
        f.write(zstd.compress(bytes(b), level=LEVEL))


def decode(raw):
    """graph.py's file as {"junctions": {node id: (lon, lat, stop)}, "edges":
    [(way, a node, b node, i0, i1, class, [(lon, lat)])]}, coordinates in
    degrees."""
    from array import array

    def take(code, n):
        nonlocal pos
        a = array(code)
        a.frombytes(raw[pos:pos + n * a.itemsize])
        pos += n * a.itemsize
        if sys.byteorder != "little":
            a.byteswap()
        return a

    if raw[:4] != b"RLTG":
        raise ValueError("not a track graph")
    _, n, e, v, _ = struct.unpack_from("<5I", raw, 4)
    pos = 24
    vocabs = []
    for _ in range(4):
        words = []
        count = raw[pos]; pos += 1
        for _ in range(count):
            ln = raw[pos]; pos += 1
            words.append(raw[pos:pos + ln].decode() or None); pos += ln
        vocabs.append(words)
    nid, lon, lat, flags = take("q", n), take("i", n), take("i", n), take("B", n)
    way, a, b, i0, i1 = take("q", e), take("i", e), take("i", e), take("i", e), take("i", e)
    take("f", e)
    codes = [take("B", e) for _ in range(4)]
    speed = take("H", e)
    take("f", e); take("f", e)
    starts, vlon, vlat = take("i", e + 1), take("i", v), take("i", v)
    junctions = {nid[j]: (lon[j] / 1e7, lat[j] / 1e7, bool(flags[j])) for j in range(n)}
    edges = []
    for k in range(e):
        cls = tuple(vocabs[m][codes[m][k]] for m in range(4)) + (speed[k] or None,)
        pa, pb = junctions[nid[a[k]]], junctions[nid[b[k]]]
        inner = [(vlon[q] / 1e7, vlat[q] / 1e7) for q in range(starts[k], starts[k + 1])]
        edges.append((way[k], nid[a[k]], nid[b[k]], i0[k], i1[k], cls, [pa[:2], *inner, pb[:2]]))
    return {"junctions": junctions, "edges": edges}


def load(path):
    """Every chunk of an archive joined into one graph.Graph, as the app joins
    the chunks it loads."""
    from diff_tiles import Archive
    arc = Archive(path)
    g, seen, stops = G.Graph(), set(), set()
    for _, off, ln in arc.tiles():
        part = decode(arc.tile_at(off, ln))
        stops.update(nid for nid, (_, _, stop) in part["junctions"].items() if stop)
        for wid, na, nb, i0, i1, cls, pts in part["edges"]:
            if (wid, i0) in seen:
                continue
            seen.add((wid, i0))
            nodes = [(na, *pts[0])] + [(0, *p) for p in pts[1:-1]] + [(nb, *pts[-1])]
            g.add_edge(wid, cls, nodes, 0, len(nodes) - 1)
            g.i0[-1], g.i1[-1] = i0, i1
    for nid in stops:
        g.stop[g.node_index[nid]] = True
    return g


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: graph_join.py REGIONS_DIR OUT_DIR TAG")
    regions_dir, out_dir, tag = sys.argv[1:]
    sidecars = sorted(glob.glob(os.path.join(regions_dir, "**", "*.graph.jsonl.zst"), recursive=True))
    if not sidecars:
        sys.exit(f"no graph sidecars in {regions_dir}")
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="graph-") as tmp:
        files, merged = split(sidecars, tmp)
        tiles, junctions, edges, conflicts = [], 0, 0, 0
        for t, path in files.items():
            g, c = chunk(t, path)
            os.remove(path)
            conflicts += c
            if not g.edge_count:
                continue
            junctions += len(g.node_id)
            edges += g.edge_count
            tiles.append((zxy_to_tileid(G.CHUNK_ZOOM, *t), t, zstd.compress(G.encode(g, {}, {}), level=LEVEL)))
    if conflicts:
        print(f"::warning::track graph: {conflicts} cuts name a different node in another region",
              file=sys.stderr)
    graph_path = os.path.join(out_dir, f"track-graph-{tag}.rltg.pmtiles")
    stations_path = os.path.join(out_dir, f"track-stations-{tag}.rlts.zst")
    write_archive(tiles, graph_path)
    write_stations(merged, stations_path)
    print(f"track graph: {len(sidecars)} regions, {len(tiles)} chunks, {junctions:,} junctions and "
          f"{edges:,} edges counted per chunk, {len(merged):,} stations; "
          f"{os.path.getsize(graph_path) / 1e6:.1f} MB + {os.path.getsize(stations_path) / 1e6:.1f} MB",
          file=sys.stderr)
    print(json.dumps({"graph": graph_path, "stations": stations_path, "chunks": len(tiles),
                      "zoom": G.CHUNK_ZOOM}))


if __name__ == "__main__":
    main()
