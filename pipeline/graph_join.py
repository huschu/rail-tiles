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
branch onto a border way then finds a junction there. Only the newest OSM
version of a way counts: a region carried forward from an older build, or
cut from another mirror, may hold an older one whose node indices differ.

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

STATIONS_VERSION = 1
# Way lines held in memory before they go to their chunk's file.
FLUSH_BYTES = 256 << 20


def split(sidecars, tmp):
    """Sends each way line to its chunk's file. Returns ({(x, y): path},
    {way id: [(version, cuts)]} of the ways a region cut short, [station
    records] per region)."""
    files, held, size = {}, {}, 0
    short, stations = {}, []

    def flush():
        nonlocal size
        for t, lines in held.items():
            path = files.setdefault(t, os.path.join(tmp, f"{t[0]}_{t[1]}.jsonl"))
            with open(path, "a", encoding="utf-8") as f:
                f.writelines(lines)
        held.clear()
        size = 0

    for path in sidecars:
        own = []
        with zstd.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if line.startswith('["station"'):
                    own.append(json.loads(line)[1])
                    continue
                if line.startswith('["cuts"'):
                    _, wid, version, cuts = json.loads(line)
                    short.setdefault(wid, []).append((version, cuts))
                    continue
                t = tuple(json.loads(line[1:line.index("]") + 1]))
                held.setdefault(t, []).append(line)
                size += len(line)
                if size > FLUSH_BYTES:
                    flush()
        stations.append(own)
    flush()
    return files, short, stations


def merge_stations(per_region):
    """One record per station across regions. stations.merge unites a
    border station's services as the map tiles do; the stop positions
    unite here."""
    first, stops, lists = {}, {}, []
    for records in per_region:
        lists.append([{"properties": {"osm_id": r["id"], "name": r["name"], "routes": _routes_json(r["routes"])},
                       "tippecanoe": {"minzoom": 0}} for r in records])
        for r in records:
            first.setdefault(r["id"], r)
            stops.setdefault(r["id"], set()).update(r["stops"])
    merged = {}
    for f in ST.merge(lists):
        p = f["properties"]
        sid = p["osm_id"]
        merged[sid] = {**first[sid], "name": p["name"] or "", "routes": p["routes"],
                       "stops": sorted(stops[sid])}
    return merged


def _routes_json(entries):
    return json.dumps(entries, ensure_ascii=False, separators=(",", ":")) if entries else ""


def chunk(t, path, short):
    """The chunk at tile t from its way lines and the cuts of ways cut
    short, and how many cuts disagreed between regions and how many way
    versions were older than another region's."""
    versions = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            _, wid, version, cls, lons, lats, cuts = json.loads(line)
            versions.setdefault(wid, []).append((version, cls, lons, lats, cuts))
    g = G.Graph()
    stops, conflicts, stale = set(), 0, 0
    for wid in sorted(versions):
        newest = max(v[0] for v in versions[wid])
        vs = [v for v in versions[wid] if v[0] == newest]
        stale += len(versions[wid]) - len(vs)
        _, cls, lons, lats, _ = max(vs, key=lambda v: len(v[2]))
        last = len(lons) - 1
        at = {}
        for cuts in [v[4] for v in vs] + [c for ver, c in short.get(wid, ()) if ver == newest]:
            for i, nid, flag in cuts:
                # Where a region cut a way short is no junction, unless the
                # whole way ends there too.
                if i > last or (flag == 2 and i != last):
                    continue
                if at.setdefault(i, nid) != nid:
                    conflicts += 1
                if flag == 1:
                    stops.add(nid)
        nodes = [(at.get(i, 0), lon / 1e7, lat / 1e7) for i, (lon, lat) in enumerate(zip(lons, lats))]
        cut = sorted(at)
        for i0, i1 in zip(cut, cut[1:]):
            if G.tile_of(*nodes[i0][1:]) == t or G.tile_of(*nodes[i1][1:]) == t:
                g.add_edge(wid, tuple(cls), nodes[i0:i1 + 1], i0, i1)
    for nid in stops:
        j = g.node_index.get(nid)
        if j is not None:
            g.stop[j] = True
    return g, conflicts, stale


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


def write_stations(merged, path):
    b = bytearray(b"RLTS")
    b += struct.pack("<2I", STATIONS_VERSION, len(merged))
    for sid in sorted(merged):
        st = merged[sid]
        stops = st["stops"][:65535]
        b += G.station_record(sid, st, st["lon"], st["lat"], len(stops), struct.pack(f"<{len(stops)}q", *stops))
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


_short = None


def _hold(short):
    global _short
    _short = short


def _build(job):
    """A chunk's compressed tile, or None without edges, with its counts."""
    t, path = job
    g, conflicts, stale = chunk(t, path, _short)
    os.remove(path)
    blob = zstd.compress(G.encode(g, {}, {}), level=LEVEL) if g.edge_count else None
    return blob, len(g.node_id), g.edge_count, conflicts, stale


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
    ents, bounds, junctions, edges, conflicts, stale = [], [], 0, 0, 0, 0
    with tempfile.TemporaryDirectory(prefix="graph-") as tmp, tempfile.TemporaryFile(dir=tmp) as data:
        files, short, per_region = split(sidecars, tmp)
        order = sorted((zxy_to_tileid(G.CHUNK_ZOOM, *t), t) for t in files)
        with ProcessPoolExecutor(initializer=_hold, initargs=(short,)) as ex:
            jobs = [(t, files[t]) for _, t in order]
            for (tid, t), (blob, n, e, c, old) in zip(order, ex.map(_build, jobs, buffersize=os.cpu_count())):
                conflicts += c
                stale += old
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
    merged = merge_stations(per_region)
    write_stations(merged, stations_path)
    if conflicts:
        print(f"::warning::track graph: {conflicts} cuts name a different node in another region")
    if stale:
        print(f"track graph: dropped {stale} way versions older than another region's", file=sys.stderr)
    print(f"track graph: {len(sidecars)} regions, {len(ents)} chunks, {junctions:,} junctions and "
          f"{edges:,} edges counted per chunk, {len(merged):,} stations; "
          f"{os.path.getsize(graph_path) / 1e6:.1f} MB + {os.path.getsize(stations_path) / 1e6:.1f} MB",
          file=sys.stderr)


if __name__ == "__main__":
    main()
