#!/usr/bin/env python3
"""
Merge the regions' track graphs (graph.py --sidecar) into the planet's,
cut into chunks the app loads as a trip's search reaches them.

The chunks are the tiles of one zoom (graph.CHUNK_ZOOM) in a PMTiles
archive: tile type 0, each tile one zstd frame of graph.py's file holding
every edge with an end in the tile. An edge between two chunks is in both,
so loading the chunk a junction lies in gives every edge at that junction.
The app joins chunks by OSM node id and drops an edge it already has by way
and first vertex.

Regions overlap at borders, and each cuts a way only where its own ways
meet it, so the join gathers every region's copy of a way and cuts the
newest version wherever any copy has a cut, by node id. A neighbour's
branch onto a border way then finds a junction there, even when the
neighbour holds an older version of the way. The ways go to bucket files by
way id first, so each bucket holds every copy of its ways.

The station file (graph.py) takes names, positions and services from the
map's stations, which the regions' station sidecars give and
stations.merge merges as the map's join does, and the stop positions from
the graphs. A station in two regions lists the services and stop positions
of both.

Needs Python 3.14+ (compression.zstd).

Usage: graph_join.py REGIONS_DIR OUT_DIR TAG
"""
import glob
import gzip
import json
import math
import os
import struct
import sys
import tempfile
from compression import zstd
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import graph as G
import stations as ST
from diff_tiles import Archive, zxy_to_tileid
from join_planet import ATTRIBUTION
from zstd_tiles import HEADER_LEN, LEVEL, ZSTD, directories, write_pmtiles

BUCKETS = 64
# Lines held in memory before they go to their files.
FLUSH_BYTES = 256 << 20


class Spill:
    """Lines bound for many files, appended in batches."""

    def __init__(self, path_of):
        self.path_of, self.paths, self.held, self.size = path_of, {}, {}, 0

    def add(self, key, lines):
        self.held.setdefault(key, []).extend(lines)
        self.size += sum(map(len, lines))
        if self.size > FLUSH_BYTES:
            self.flush()

    def flush(self):
        for key, lines in self.held.items():
            path = self.paths.setdefault(key, self.path_of(key))
            with open(path, "a", encoding="utf-8") as f:
                f.writelines(lines)
        self.held.clear()
        self.size = 0


def split(sidecars, tmp):
    """Sends each way line to its bucket's file. Returns ([bucket paths],
    {station id: {stop node ids}})."""
    buckets = Spill(lambda b: os.path.join(tmp, f"bucket{b}.jsonl"))
    stops = {}
    for path in sidecars:
        with zstd.open(path, "rt", encoding="utf-8") as f:
            if f.readline() != f'["sidecar",{G.SIDECAR_VERSION}]\n':
                # A region carried forward from a build before this format.
                print(f"::warning::track graph: skipping {os.path.basename(path)}, an older sidecar")
                continue
            for line in f:
                if line.startswith('["stops"'):
                    _, sid, ids = json.loads(line)
                    stops.setdefault(sid, set()).update(ids)
                else:
                    buckets.add(int(line[1:line.index(",")]) % BUCKETS, [line])
    buckets.flush()
    return list(buckets.paths.values()), stops


def way_edges(copies):
    """The newest version of a way cut at every copy's cuts, from the
    regions' copies [(version, class, lons, lats, node ids, cuts, stops)]:
    (class, [(i0, i1, a, b, stop flags, lons, lats)], copies older than the
    newest). Flag 1 marks a stop at a, 2 at b."""
    newest = max(c[0] for c in copies)
    _, cls, lons, lats, nids, _, _ = max((c for c in copies if c[0] == newest), key=lambda c: len(c[4]))
    cut, stop = set(), set()
    for c in copies:
        cut.update(c[5])
        stop.update(c[6])
    last = len(nids) - 1
    at = [i for i, nid in enumerate(nids) if i == 0 or i == last or nid in cut]
    edges = []
    for i0, i1 in zip(at, at[1:]):
        a, b = nids[i0], nids[i1]
        edges.append((i0, i1, a, b, (a in stop) | (b in stop) << 1, lons[i0:i1 + 1], lats[i0:i1 + 1]))
    return cls, edges, sum(1 for c in copies if c[0] < newest)


def bucket(path):
    """The edge lines of a bucket's ways by chunk tile, and how many way
    copies were older than another region's."""
    ways = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            wid, *copy = json.loads(line)
            ways.setdefault(wid, []).append(copy)
    os.remove(path)
    out, stale = {}, 0
    for wid, copies in ways.items():
        cls, edges, old = way_edges(copies)
        stale += old
        for i0, i1, a, b, flags, lons, lats in edges:
            line = json.dumps([wid, i0, i1, cls, a, b, flags, lons, lats], separators=(",", ":")) + "\n"
            for t in {G.tile_of(lons[0] / 1e7, lats[0] / 1e7), G.tile_of(lons[-1] / 1e7, lats[-1] / 1e7)}:
                out.setdefault(t, []).append(line)
    return out, stale


def chunk(path):
    """The graph of a chunk's edge lines, in way order, which compresses
    best."""
    g, stops = G.Graph(), set()
    with open(path, encoding="utf-8") as f:
        records = sorted((json.loads(line) for line in f), key=lambda r: (r[0], r[1]))
    for wid, i0, i1, cls, a, b, flags, lons, lats in records:
        pts = [(lon / 1e7, lat / 1e7) for lon, lat in zip(lons, lats)]
        nodes = [(a, *pts[0])] + [(0, *p) for p in pts[1:-1]] + [(b, *pts[-1])]
        g.add_edge(wid, tuple(cls), nodes, i0, i1)
        if flags & 1:
            stops.add(a)
        if flags & 2:
            stops.add(b)
    for nid in stops:
        g.stop[g.node_index[nid]] = True
    return g


def tile_bounds(x, y):
    n = 1 << G.CHUNK_ZOOM

    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


def write_archive(ents, bounds, data, path):
    """A PMTiles v3 archive, tile type 0, zstd tiles, one zoom, of the
    entries [(tile id, 1, offset, length)] in tile id order into the open
    file data, which holds the tiles; bounds are the tiles' [(w, s, e, n)]."""
    root, leaves = directories(ents)
    meta = gzip.compress(json.dumps({
        "name": "track-graph", "format": "rltg", "version": G.VERSION,
        "attribution": ATTRIBUTION,
    }).encode(), mtime=0)
    w, s = min(b[0] for b in bounds), min(b[1] for b in bounds)
    e, n = max(b[2] for b in bounds), max(b[3] for b in bounds)
    head = bytearray(HEADER_LEN)
    head[:8] = b"PMTiles\x03"
    head[96] = 1                     # clustered
    head[97] = 2                     # gzip directories and metadata
    head[98] = ZSTD
    head[99] = 0                     # tile type: other
    head[100] = head[101] = G.CHUNK_ZOOM
    struct.pack_into("<iiii", head, 102, *(round(v * 1e7) for v in (w, s, e, n)))
    head[118] = G.CHUNK_ZOOM
    struct.pack_into("<ii", head, 119, round((w + e) / 2 * 1e7), round((s + n) / 2 * 1e7))
    write_pmtiles(head, root, meta, leaves, data, (len(ents),) * 3, path)


def write_stations(regions_dir, stops, path):
    """The station file of the regions' station sidecars, merged as the
    map's join merges them, with the graphs' stop positions."""
    lists = []
    for sc in sorted(glob.glob(os.path.join(regions_dir, "**", "*.stations.geojsonseq"), recursive=True)):
        with open(sc, encoding="utf-8") as f:
            lists.append([json.loads(line) for line in f if line.strip()])
    features = ST.merge(lists)
    with open(path, "wb") as f:
        f.write(zstd.compress(G.station_file(features, {sid: sorted(ids) for sid, ids in stops.items()}),
                              level=LEVEL))
    return sum(1 for f in features if f["properties"]["kind"] in G.TRAIN_STATIONS)


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
    _, n, e, v = struct.unpack_from("<4I", raw, 4)
    pos = 20
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
    arc = Archive(path)
    g, seen, stops = G.Graph(), set(), set()
    try:
        for _, off, ln in arc.tiles():
            part = decode(arc.tile_at(off, ln))
            stops.update(nid for nid, (_, _, stop) in part["junctions"].items() if stop)
            for wid, na, nb, i0, i1, cls, pts in part["edges"]:
                if (wid, i0) in seen:
                    continue
                seen.add((wid, i0))
                nodes = [(na, *pts[0])] + [(0, *p) for p in pts[1:-1]] + [(nb, *pts[-1])]
                g.add_edge(wid, cls, nodes, i0, i1)
    finally:
        arc.mm.close()
        arc.f.close()
    for nid in stops:
        g.stop[g.node_index[nid]] = True
    return g


def _build(path):
    """A chunk's compressed tile, or None without edges, with its counts."""
    g = chunk(path)
    os.remove(path)
    blob = zstd.compress(G.encode(g), level=LEVEL) if g.edge_count else None
    return blob, len(g.node_id), g.edge_count


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: graph_join.py REGIONS_DIR OUT_DIR TAG")
    regions_dir, out_dir, tag = sys.argv[1:]
    sidecars = sorted(glob.glob(os.path.join(regions_dir, "**", "*.graph.jsonl.zst"), recursive=True))
    if not sidecars:
        sys.exit(f"no graph sidecars in {regions_dir}")
    os.makedirs(out_dir, exist_ok=True)
    graph_path = os.path.join(out_dir, f"track-graph-{tag}.rltg.pmtiles")
    stations_path = os.path.join(out_dir, f"track-stations-{tag}.rlts.zst")
    ents, bounds, junctions, edges, stale = [], [], 0, 0, 0
    with tempfile.TemporaryDirectory(prefix="graph-") as tmp, tempfile.TemporaryFile(dir=tmp) as data, \
            ProcessPoolExecutor() as ex:
        buckets, stops = split(sidecars, tmp)
        tiles = Spill(lambda t: os.path.join(tmp, f"{t[0]}_{t[1]}.jsonl"))
        for out, old in ex.map(bucket, buckets, buffersize=os.cpu_count()):
            stale += old
            for t, lines in out.items():
                tiles.add(t, lines)
        tiles.flush()
        order = sorted((zxy_to_tileid(G.CHUNK_ZOOM, *t), t) for t in tiles.paths)
        paths = [tiles.paths[t] for _, t in order]
        for (tid, t), (blob, n, e) in zip(order, ex.map(_build, paths, buffersize=os.cpu_count())):
            if blob is None:
                continue
            junctions += n
            edges += e
            ents.append((tid, 1, data.tell(), len(blob)))
            bounds.append(tile_bounds(*t))
            data.write(blob)
        if not ents:
            sys.exit("no track in the graph sidecars")
        write_archive(ents, bounds, data, graph_path)
    stations = write_stations(regions_dir, stops, stations_path)
    if stale:
        print(f"track graph: {stale} way copies older than another region's", file=sys.stderr)
    print(f"track graph: {len(sidecars)} regions, {len(ents)} chunks, {junctions:,} junctions and "
          f"{edges:,} edges counted per chunk, {stations:,} stations; "
          f"{os.path.getsize(graph_path) / 1e6:.1f} MB + {os.path.getsize(stations_path) / 1e6:.1f} MB",
          file=sys.stderr)


if __name__ == "__main__":
    main()
