import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from itertools import accumulate

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph as G
import graph_join as J
import search as S
from diff_tiles import Archive as PMTiles

RAIL = ["rail", None, "main", None, 160]


def add_chain(g, start, end, pieces, outline, heads):
    """An edge of the file (encode): its ends (node id, lon, lat), pieces
    [(way, length, class)], outline [(lon, lat)] from end to end and
    headings leaving each end."""
    e = len(g.way)
    a, b = g._junction(*start), g._junction(*end)
    g.way.append(pieces[0][0])
    g.i0.append(0)
    g.i1.append(0)
    g.a.append(a)
    g.b.append(b)
    g.length.append(sum(p[1] for p in pieces))
    g.cls.append(tuple(pieces[0][2]))
    g.coords.append(list(outline))
    g.head_out += list(heads)
    g.out[a].append(2 * e)
    g.out[b].append(2 * e + 1)
    if len(pieces) > 1:
        g.pieces[e] = pieces


class _Reader:
    def __init__(self, raw, pos):
        self.raw, self.pos = raw, pos

    def varint(self):
        v = shift = 0
        while True:
            c = self.raw[self.pos]
            self.pos += 1
            v |= (c & 0x7F) << shift
            if c < 0x80:
                return v
            shift += 7

    def zigzag(self):
        v = self.varint()
        return v >> 1 if not v & 1 else -(v >> 1) - 1

    def column(self, n, signed=False):
        read = self.zigzag if signed else self.varint
        return [read() for _ in range(n)]

    def u16(self, n):
        out = struct.unpack_from(f"<{n}H", self.raw, self.pos)
        self.pos += 2 * n
        return out

    def u8(self, n):
        out = self.raw[self.pos:self.pos + n]
        self.pos += n
        return out


def decode(raw):
    """graph.py's file as {"junctions": {node id: (lon, lat, stop)}, "edges":
    [(a node, b node, (heading leaving a, leaving b), [(way, length m, class)],
    [(lon, lat)] from a to b)]}, coordinates in degrees."""
    if raw[:4] != b"RLTG":
        raise ValueError("not a track graph")
    if struct.unpack_from("<I", raw, 4)[0] != G.VERSION:
        raise ValueError("another track graph version")
    r = _Reader(raw, 8)
    n, e, p, v, c = (r.varint() for _ in range(5))
    words = [[r.u8(r.varint()).decode() or None for _ in range(r.varint())] for _ in range(4)]
    classes = []
    for _ in range(c):
        codes = [r.varint() for _ in range(5)]
        classes.append(tuple(words[m][codes[m]] for m in range(4)) + (codes[4] or None,))
    nid = list(accumulate(r.column(n)))
    lon, lat = list(accumulate(r.column(n, True))), list(accumulate(r.column(n, True)))
    flags = r.u8(n)
    a = list(accumulate(r.column(e)))
    b = [x + y for x, y in zip(a, r.column(e, True))]
    head_a, head_b = r.u16(e), r.u16(e)
    piece_count, outline_count = r.column(e), r.column(e)
    ways = list(accumulate(r.column(p, True)))
    lengths, piece_class = r.column(p), r.column(p)
    dlon, dlat = r.column(v, True), r.column(v, True)
    junctions = {nid[j]: (lon[j] / 1e7, lat[j] / 1e7, bool(flags[j])) for j in range(n)}
    edges, q, k = [], 0, 0
    for i in range(e):
        pieces = [(ways[x], lengths[x] / 10, classes[piece_class[x]]) for x in range(q, q + piece_count[i])]
        q += piece_count[i]
        x, y = lon[a[i]], lat[a[i]]
        outline = [(lon[a[i]] / 1e7, lat[a[i]] / 1e7)]
        for m in range(k, k + outline_count[i]):
            x, y = x + dlon[m], y + dlat[m]
            outline.append((x / 1e7, y / 1e7))
        k += outline_count[i]
        outline.append((lon[b[i]] / 1e7, lat[b[i]] / 1e7))
        edges.append((nid[a[i]], nid[b[i]], (head_a[i] * 360 / 65536, head_b[i] * 360 / 65536), pieces, outline))
    return {"junctions": junctions, "edges": edges}


def load(path):
    """Every chunk of an archive joined into one graph.Graph, as the app joins
    the chunks it loads: an edge in two chunks comes in once. Its two copies
    read the same field for field; one way can run between the same two
    junctions twice, as tiny loops do, so nothing less tells them apart."""
    arc = PMTiles(path)
    g, seen, stops = G.Graph(), set(), set()
    try:
        for _, off, ln in arc.tiles():
            part = decode(arc.tile_at(off, ln))
            stops.update(nid for nid, (_, _, stop) in part["junctions"].items() if stop)
            for na, nb, heads, pieces, outline in part["edges"]:
                key = (na, nb, heads, tuple(pieces), tuple(outline))
                if key in seen:
                    continue
                seen.add(key)
                add_chain(g, (na, *outline[0]), (nb, *outline[-1]), pieces, outline, heads)
    finally:
        arc.mm.close()
        arc.f.close()
    for nid in stops:
        g.stop[g.node_index[nid]] = True
    return g


def copy(lons, lat, nids, cuts, stops=(), version=1):
    """A region's copy of a way, as way_edges takes it."""
    return (version, RAIL, [round(x * 1e7) for x in lons], [round(lat * 1e7)] * len(lons),
            nids, cuts, list(stops))


def ranges(edges):
    return [(i0, i1) for i0, i1, *_ in edges]


def bucket(lines):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.writelines(lines)
    return J.bucket(f.name)


def way_line(wid, c):
    version, cls, lons, lats, nids, cuts, stops = c
    return json.dumps([wid, version, cls, G.deltas(lons), G.deltas(lats), G.deltas(nids), cuts, stops]) + "\n"


class WayEdges(unittest.TestCase):
    LONS = [10.0, 10.01, 10.02, 10.03, 10.04]
    NIDS = [1, 2, 3, 4, 5]

    def test_a_way_is_cut_wherever_any_region_cut_it(self):
        here = copy(self.LONS, 50.0, self.NIDS, [1, 5])
        neighbour = copy(self.LONS, 50.0, self.NIDS, [1, 3, 5], stops=[3])  # its branch meets the way at node 3
        _, edges, stale = J.way_edges([here, neighbour])
        self.assertEqual(ranges(edges), [(0, 2), (2, 4)])
        self.assertEqual([e[4] for e in edges], [2, 1])
        self.assertEqual(stale, 0)

    def test_an_older_versions_cuts_count_where_its_nodes_still_are(self):
        # The older version had no node 2, so its cut at node 4 sat at index 2.
        new = copy(self.LONS, 50.0, self.NIDS, [1, 5], version=4)
        old = copy([10.0, 10.02, 10.03, 10.04], 50.0, [1, 3, 4, 5], [1, 4, 5], version=3)
        _, edges, stale = J.way_edges([new, old])
        self.assertEqual(ranges(edges), [(0, 3), (3, 4)])
        self.assertEqual(stale, 1)

    def test_a_way_cut_short_adds_its_cuts_and_the_whole_way_counts(self):
        whole = copy(self.LONS, 50.0, self.NIDS, [1, 5])
        # Another region holds the way up to node 3 only, with a branch at node 2.
        short = copy(self.LONS[:3], 50.0, self.NIDS[:3], [1, 2])
        _, edges, _ = J.way_edges([short, whole])
        self.assertEqual(ranges(edges), [(0, 1), (1, 4)])

    def test_an_edge_between_chunks_goes_to_both(self):
        # 8.4375 degrees east is a z7 tile edge.
        west, east = G.tile_of(8.40, 50.0), G.tile_of(8.47, 50.0)
        self.assertNotEqual(west, east)
        out, _ = bucket([way_line(8, copy([8.40, 8.47], 50.0, [1, 2], [1, 2]))])
        self.assertEqual(sorted(out), sorted([west, east]))
        self.assertEqual(out[west], out[east])


class Stations(unittest.TestCase):
    def test_the_station_file_lists_train_stations_with_their_stops(self):
        feats = [{"properties": {"osm_id": "n2", "kind": "train", "name": "Basel SBB", "routes": '[["train"]]',
                                 "names": [["fr", "Bâle CFF"]]},
                  "geometry": {"coordinates": [7.5895, 47.5474]}},
                 {"properties": {"osm_id": "n1", "kind": "tram", "name": "Bankverein"},
                  "geometry": {"coordinates": [7.59, 47.55]}}]
        feats[0]["properties"]["town"] = {"id": "n9", "name": "Basel", "names": [["fr", "Bâle"]],
                                          "place": "city", "population": 177000}
        raw = G.station_file(feats, {"n2": [10, 11], "n1": [12]})
        self.assertEqual(raw[:4], b"RLTS")
        self.assertEqual(struct.unpack_from("<2I", raw, 4), (G.STATIONS_VERSION, 1))
        self.assertEqual(raw[12:19], b"\x05\x00Basel")
        self.assertEqual(raw[19:23], b"\x01\x02fr")
        self.assertEqual(raw[23:25 + len("Bâle".encode())], struct.pack("<H", 5) + "Bâle".encode())
        pos = 25 + len("Bâle".encode())
        self.assertEqual(struct.unpack_from("<BI", raw, pos), (1, 177000))
        pos += 5
        self.assertEqual(struct.unpack_from("<I", raw, pos), (1,))
        pos += 4
        self.assertEqual(raw[pos + 1:pos + 3], b"n2")
        pos += 3
        name_len, = struct.unpack_from("<H", raw, pos)
        pos += 2 + name_len
        self.assertEqual(raw[pos], 1)
        self.assertEqual(raw[pos + 1:pos + 4], b"\x02fr")
        other_len, = struct.unpack_from("<H", raw, pos + 4)
        self.assertEqual(raw[pos + 6:pos + 6 + other_len].decode(), "Bâle CFF")
        pos += 6 + other_len
        pos += 1 + raw[pos]
        lon, lat, count = struct.unpack_from("<iiH", raw, pos)
        pos += 10
        self.assertEqual((lon, lat), (75895000, 475474000))
        self.assertEqual(struct.unpack_from(f"<{count}q", raw, pos), (10, 11))
        pos += 8 * count
        routes_len, = struct.unpack_from("<I", raw, pos)
        self.assertEqual(raw[pos + 4:pos + 4 + routes_len], b'[["train"]]')
        pos += 4 + routes_len
        self.assertEqual(struct.unpack_from("<I", raw, pos), (0,))
        self.assertEqual(pos + 4, len(raw))


def opl_way(wid, nodes, version=1):
    refs = ",".join(f"n{nid}x{lon}y{lat}" for nid, lon, lat in nodes)
    return f"w{wid} v{version} dV c0 t2026-01-01T00:00:00Z i0 u Trailway=rail,usage=main N{refs}\n"


def pieces(g):
    """Each edge as its ways and end nodes."""
    return sorted((tuple(p[0] for p in S.pieces(g, e)), g.node_id[g.a[e]], g.node_id[g.b[e]])
                  for e in range(g.edge_count))


@unittest.skipUnless(shutil.which("zstd"), "needs the zstd command")
class Sidecar(unittest.TestCase):
    def test_a_regions_sidecar_joins_into_the_graph_it_was_built_from(self):
        line = [(1, 10.0, 50.0), (2, 10.01, 50.0), (3, 10.02, 50.0)]
        branch = [(2, 10.01, 50.0), (4, 10.01, 50.01)]
        g = G.build([opl_way(70, line), opl_way(71, branch)])
        with tempfile.TemporaryDirectory() as tmp:
            side = os.path.join(tmp, "x.graph.jsonl.zst")
            G.sidecar(g, {}, side)
            buckets, _ = J.split([side], tmp)
            spill = J.Spill(lambda t: os.path.join(tmp, f"{t[0]}_{t[1]}.jsonl"))
            for path in buckets:
                for t, lines in J.bucket(path)[0].items():
                    spill.add(t, lines)
            spill.flush()
            self.assertEqual(list(spill.paths), [G.tile_of(10.0, 50.0)])
            joined = J.chunk(spill.paths[G.tile_of(10.0, 50.0)])
        self.assertEqual(pieces(joined), pieces(g))

    def test_a_way_cut_at_a_halt_joins_back_whole(self):
        line = [(1, 10.0, 50.0), (2, 10.01, 50.0), (3, 10.02, 50.0), (4, 10.03, 50.0)]
        g = G.build([opl_way(70, line)])
        G._track_beside(g, [{"id": "n9", "kind": "train", "lon": 10.01, "lat": 50.001},
                            {"id": "n8", "kind": "train", "lon": 10.02, "lat": 50.001}])
        with tempfile.TemporaryDirectory() as tmp:
            side = os.path.join(tmp, "x.graph.jsonl.zst")
            G.sidecar(g, {}, side)
            buckets, _ = J.split([side], tmp)
            lines = [l for path in buckets for ls in J.bucket(path)[0].values() for l in ls]
            chunk = os.path.join(tmp, "c.jsonl")
            with open(chunk, "w") as f:
                f.writelines(lines)
            joined = J.chunk(chunk)
        self.assertEqual(pieces(joined), pieces(g))
        self.assertEqual(sorted(joined.length), sorted(g.length))

    def test_a_sidecar_of_another_format_is_skipped(self):
        from compression import zstd
        with tempfile.TemporaryDirectory() as tmp:
            side = os.path.join(tmp, "old.graph.jsonl.zst")
            with zstd.open(side, "wt") as f:
                f.write('[[1,2],8,1,["rail",null,null,null,null],[0,1],[0,0],[[0,1,0],[1,2,0]]]\n')
            self.assertEqual(J.split([side], tmp), ([], {}))


class Archive(unittest.TestCase):
    def test_the_script_joins_region_sidecars_into_chunks(self):
        g = G.build([opl_way(70, [(1, 10.0, 50.0), (2, 10.01, 50.0), (3, 10.02, 50.0)])])
        with tempfile.TemporaryDirectory() as tmp:
            regions = os.path.join(tmp, "regions")
            os.makedirs(os.path.join(regions, "x"))
            G.sidecar(g, {}, os.path.join(regions, "x", "x.graph.jsonl.zst"))
            out = os.path.join(tmp, "out")
            subprocess.run([sys.executable, J.__file__, regions, out, "test"], check=True, capture_output=True)
            self.assertEqual(pieces(load(os.path.join(out, "track-graph-test.rltg.pmtiles"))), pieces(g))

    def test_chunks_read_back_as_one_graph(self):
        from compression import zstd
        out, _ = bucket([way_line(8, copy([8.40, 8.47], 50.0, [1, 2], [1, 2], stops=[1])),
                         way_line(9, copy([8.47, 8.50], 50.0, [2, 3], [2, 3]))])
        ents, bounds = [], []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "graph.pmtiles")
            with tempfile.TemporaryFile() as data:
                for tid, t in sorted((J.zxy_to_tileid(G.CHUNK_ZOOM, *t), t) for t in out):
                    lines = os.path.join(tmp, "chunk.jsonl")
                    with open(lines, "w") as f:
                        f.writelines(out[t])
                    blob = zstd.compress(G.encode(J.chunk(lines)))
                    ents.append((tid, 1, data.tell(), len(blob)))
                    bounds.append(J.tile_bounds(*t))
                    data.write(blob)
                J.write_archive(ents, bounds, data, path)
            g = load(path)
        # Node 2 joins two ways but stays: way 8 crosses into the next chunk.
        self.assertEqual(pieces(g), [((8,), 1, 2), ((9,), 2, 3)])
        self.assertEqual(sorted(g.node_id), [1, 2, 3])
        self.assertTrue(g.stop[g.node_index[1]])


class Encode(unittest.TestCase):
    def test_track_a_train_passes_without_a_choice_merges_into_one_edge(self):
        # Ways 70, 71 and 72 run on in a line inside one chunk; node 3 is a
        # stop, so the merged edges end there.
        line = [(1, 10.0, 50.0), (2, 10.01, 50.0), (3, 10.02, 50.0), (4, 10.03, 50.0), (5, 10.04, 50.0)]
        g = G.build([opl_way(70, line[:2]), opl_way(71, line[1:4]), opl_way(72, line[3:]),
                     "n3 v1 dV c0 t2026-01-01T00:00:00Z i0 u Trailway=stop x10.02 y50.0\n"])
        part = decode(G.encode(g))
        self.assertEqual(sorted((a, b, [p[0] for p in ps]) for a, b, _, ps, _ in part["edges"]),
                         [(1, 3, [70, 71]), (3, 5, [71, 72])])
        self.assertEqual(sorted(part["junctions"]), [1, 3, 5])
        whole = sum(g.length)
        self.assertAlmostEqual(sum(p[1] for _, _, _, ps, _ in part["edges"] for p in ps), whole, places=3)

    def test_a_merged_edge_costs_what_its_pieces_do(self):
        line = [(1, 10.0, 50.0), (2, 10.01, 50.0), (3, 10.02, 50.0)]
        slow = opl_way(71, line[1:]).replace("usage=main", "usage=main,maxspeed=60")
        g = G.build([opl_way(70, line[:2]), slow])
        expected, _ = S.find(g, [g.node_index[1]], g.node_index[3], S.train_factor(g))
        with tempfile.TemporaryDirectory() as tmp:
            merged = G.Graph()
            for a, b, heads, ps, outline in decode(G.encode(g))["edges"]:
                add_chain(merged, (a, *outline[0]), (b, *outline[-1]), ps, outline, heads)
        self.assertEqual(merged.edge_count, 1)
        cost, _ = S.find(merged, [merged.node_index[1]], merged.node_index[3], S.train_factor(merged))
        self.assertAlmostEqual(cost, expected, places=3)

    def test_an_outline_keeps_the_bends(self):
        # A way bending 50 m off its line between two straight stretches.
        pts = [(1, 10.0, 50.0), (2, 10.001, 50.0), (3, 10.002, 50.0), (4, 10.003, 50.00045),
               (5, 10.004, 50.0), (6, 10.005, 50.0)]
        _, _, _, _, outline = decode(G.encode(G.build([opl_way(70, pts)])))["edges"][0]
        self.assertEqual(len(outline), 5)


class Halts(unittest.TestCase):
    def test_a_station_without_stop_positions_starts_from_the_track_beside_it(self):
        g = G.build([opl_way(70, [(1, 10.0, 50.0), (2, 10.01, 50.0), (3, 10.02, 50.0)])])
        station = {"id": "n9", "kind": "train", "lon": 10.0101, "lat": 50.0005}
        self.assertEqual(G._track_beside(g, [station]), {"n9": g.node_index[2]})
        self.assertEqual(G._track_beside(g, [{**station, "lat": 50.01}]), {})

    def test_the_track_node_beside_a_halt_becomes_a_junction(self):
        g = G.build([opl_way(70, [(1, 10.0, 50.0), (2, 10.005, 50.0), (3, 10.01, 50.0)])])
        self.assertEqual(g.edge_count, 1)
        j = G._track_beside(g, [{"id": "n9", "kind": "train", "lon": 10.005, "lat": 50.001}])["n9"]
        self.assertEqual(g.node_id[j], 2)
        self.assertEqual(sorted((g.i0[e], g.i1[e]) for e in range(g.edge_count)), [(0, 1), (1, 2)])
        self.assertAlmostEqual(sum(g.length), G._length([(10.0, 50.0), (10.005, 50.0), (10.01, 50.0)]), places=1)


class Tracks(unittest.TestCase):
    def test_tram_track_stays_only_where_a_train_route_runs(self):
        tram = "w71 v1 dV c0 t2026-01-01T00:00:00Z i0 u Trailway=tram N" + "n2x10.01y50.0,n3x10.02y50.0\n"
        lone = "w72 v1 dV c0 t2026-01-01T00:00:00Z i0 u Trailway=tram N" + "n3x10.02y50.0,n4x10.03y50.0\n"
        route = "r5 v1 dV c0 t2026-01-01T00:00:00Z i0 u Troute=train Mw71@\n"
        g = G.build([opl_way(70, [(1, 10.0, 50.0), (2, 10.01, 50.0)]), tram, lone, route])
        self.assertEqual(sorted(g.version), [70, 71])

    def test_freight_only_track_stays_only_where_a_train_route_runs(self):
        line = opl_way(70, [(1, 10.0, 50.0), (2, 10.01, 50.0)])
        used = opl_way(71, [(2, 10.01, 50.0), (3, 10.02, 50.0)]).replace("usage=main", "usage=main,railway:traffic_mode=freight")
        bypass = opl_way(72, [(3, 10.02, 50.0), (4, 10.03, 50.0)]).replace("usage=main", "usage=main,railway:traffic_mode=freight")
        mixed = opl_way(73, [(4, 10.03, 50.0), (5, 10.04, 50.0)]).replace("usage=main", "usage=main,railway:traffic_mode=mixed")
        route = "r5 v1 dV c0 t2026-01-01T00:00:00Z i0 u Troute=train Mw71@\n"
        g = G.build([line, used, bypass, mixed, route])
        self.assertEqual(sorted(g.version), [70, 71, 73])


if __name__ == "__main__":
    unittest.main()
