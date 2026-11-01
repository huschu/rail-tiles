import json
import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph as G
import graph_join as J

RAIL = ["rail", None, "main", None, 160]


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
    return json.dumps([wid, *c]) + "\n"


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
        feats = [{"properties": {"osm_id": "n2", "kind": "train", "name": "Basel SBB", "routes": '[["train"]]'},
                  "geometry": {"coordinates": [7.5895, 47.5474]}},
                 {"properties": {"osm_id": "n1", "kind": "tram", "name": "Bankverein"},
                  "geometry": {"coordinates": [7.59, 47.55]}}]
        raw = G.station_file(feats, {"n2": [10, 11], "n1": [12]})
        self.assertEqual(raw[:4], b"RLTS")
        self.assertEqual(struct.unpack_from("<2I", raw, 4), (G.STATIONS_VERSION, 1))
        self.assertEqual(raw[13:15], b"n2")
        pos = 15
        name_len, = struct.unpack_from("<H", raw, pos)
        pos += 2 + name_len
        pos += 1 + raw[pos]
        lon, lat, count = struct.unpack_from("<iiH", raw, pos)
        pos += 10
        self.assertEqual((lon, lat), (75895000, 475474000))
        self.assertEqual(struct.unpack_from(f"<{count}q", raw, pos), (10, 11))
        pos += 8 * count
        routes_len, = struct.unpack_from("<I", raw, pos)
        self.assertEqual(raw[pos + 4:pos + 4 + routes_len], b'[["train"]]')
        self.assertEqual(pos + 4 + routes_len, len(raw))


def opl_way(wid, nodes, version=1):
    refs = ",".join(f"n{nid}x{lon}y{lat}" for nid, lon, lat in nodes)
    return f"w{wid} v{version} dV c0 t2026-01-01T00:00:00Z i0 u Trailway=rail,usage=main N{refs}\n"


def pieces(g):
    return sorted((g.way[e], g.i0[e], g.i1[e]) for e in range(g.edge_count))


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

    def test_a_sidecar_of_another_format_is_skipped(self):
        from compression import zstd
        with tempfile.TemporaryDirectory() as tmp:
            side = os.path.join(tmp, "old.graph.jsonl.zst")
            with zstd.open(side, "wt") as f:
                f.write('[[1,2],8,1,["rail",null,null,null,null],[0,1],[0,0],[[0,1,0],[1,2,0]]]\n')
            self.assertEqual(J.split([side], tmp), ([], {}))


class Archive(unittest.TestCase):
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
            g = J.load(path)
        self.assertEqual(pieces(g), [(8, 0, 1), (9, 0, 1)])
        self.assertEqual(sorted(g.node_id), [1, 2, 3])
        self.assertTrue(g.stop[g.node_index[1]])


if __name__ == "__main__":
    unittest.main()
