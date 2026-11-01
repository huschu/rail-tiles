import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph as G
import graph_join as J

RAIL = ["rail", None, "main", None, 160]


def way_line(wid, lons, lat, cuts, version=1):
    t = G.tile_of(lons[0], lat)
    return json.dumps([list(t), wid, version, RAIL, [round(x * 1e7) for x in lons],
                       [round(lat * 1e7)] * len(lons), cuts]) + "\n"


def chunk(lines, t, short=None):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.writelines(lines)
    try:
        return J.chunk(t, f.name, short)
    finally:
        os.remove(f.name)


def pieces(g):
    return sorted((g.way[e], g.i0[e], g.i1[e]) for e in range(g.edge_count))


class Chunk(unittest.TestCase):
    def test_a_way_is_cut_wherever_any_region_cut_it(self):
        lons = [10.0, 10.01, 10.02, 10.03, 10.04]
        here = [[0, 1, 0], [4, 5, 0]]
        neighbour = [[0, 1, 0], [2, 3, 1], [4, 5, 0]]  # its branch meets the way at node 3
        g, conflicts, _ = chunk([way_line(7, lons, 50.0, here), way_line(7, lons, 50.0, neighbour)],
                                G.tile_of(10.0, 50.0))
        self.assertEqual(pieces(g), [(7, 0, 2), (7, 2, 4)])
        self.assertTrue(g.stop[g.node_index[3]])
        self.assertEqual(conflicts, 0)

    def test_an_edge_between_chunks_is_in_both(self):
        # 8.4375 degrees east is a z7 tile edge.
        line = way_line(8, [8.40, 8.47], 50.0, [[0, 1, 0], [1, 2, 0]])
        west, east = G.tile_of(8.40, 50.0), G.tile_of(8.47, 50.0)
        self.assertNotEqual(west, east)
        self.assertEqual(pieces(chunk([line], west)[0]), [(8, 0, 1)])
        self.assertEqual(pieces(chunk([line], east)[0]), [(8, 0, 1)])

    def test_regions_naming_different_nodes_at_one_cut_are_counted(self):
        lons = [10.0, 10.01]
        _, conflicts, _ = chunk([way_line(9, lons, 50.0, [[0, 1, 0], [1, 2, 0]]),
                                 way_line(9, lons, 50.0, [[0, 1, 0], [1, 3, 0]])], G.tile_of(10.0, 50.0))
        self.assertEqual(conflicts, 1)

    def test_an_older_version_of_a_way_is_ignored(self):
        # The older version had no node at index 1; its cut there named another node.
        new = way_line(5, [10.0, 10.01, 10.02], 50.0, [[0, 1, 0], [2, 3, 0]], version=4)
        old = way_line(5, [10.0, 10.02], 50.0, [[0, 1, 0], [1, 3, 0]], version=3)
        g, conflicts, stale = chunk([new, old], G.tile_of(10.0, 50.0))
        self.assertEqual(pieces(g), [(5, 0, 2)])
        self.assertEqual((conflicts, stale), (0, 1))

    def test_a_way_cut_short_still_adds_its_cuts_and_not_its_short_end(self):
        lons = [10.0, 10.01, 10.02, 10.03, 10.04]
        whole = way_line(6, lons, 50.0, [[0, 1, 0], [4, 5, 0]])
        # Another region holds the way up to index 2 only, with a branch at index 1.
        short = {6: [(1, [[0, 1, 0], [1, 2, 0], [2, 9, 2]])]}
        g, _, _ = chunk([whole], G.tile_of(10.0, 50.0), short)
        self.assertEqual(pieces(g), [(6, 0, 1), (6, 1, 4)])


class Stations(unittest.TestCase):
    def test_a_border_station_gets_both_regions_stops_and_services(self):
        ch = [{"id": "n1", "name": "Basel SBB", "kind": "train", "lon": 0, "lat": 0,
               "routes": [["train", "IC 5", None, "SBB", [1]]], "stops": [10]}]
        de = [{"id": "n1", "name": "Basel SBB", "kind": "train", "lon": 0, "lat": 0,
               "routes": [["train", "IC 5", 0xFF0000, "SBB", [2]], ["train", "RE 7", None, "DB", [3]]],
               "stops": [11]}]
        st = J.merge_stations([ch, de])["n1"]
        self.assertEqual(st["stops"], [10, 11])
        self.assertEqual(sorted(st["routes"]), [["train", "IC 5", 0xFF0000, "SBB", [1, 2]],
                                                ["train", "RE 7", None, "DB", [3]]])


def opl_way(wid, nodes, version=1):
    refs = ",".join(f"n{nid}x{lon}y{lat}" for nid, lon, lat in nodes)
    return f"w{wid} v{version} dV c0 t2026-01-01T00:00:00Z i0 u Trailway=rail,usage=main N{refs}\n"


@unittest.skipUnless(shutil.which("zstd"), "needs the zstd command")
class Sidecar(unittest.TestCase):
    def test_a_regions_sidecar_joins_into_the_graph_it_was_built_from(self):
        line = [(1, 10.0, 50.0), (2, 10.01, 50.0), (3, 10.02, 50.0)]
        branch = [(2, 10.01, 50.0), (4, 10.01, 50.01)]
        g = G.build([opl_way(70, line), opl_way(71, branch)])
        with tempfile.TemporaryDirectory() as tmp:
            region = os.path.join(tmp, "regions", "x")
            os.makedirs(region)
            G.sidecar(g, {}, {}, os.path.join(region, "x.graph.jsonl.zst"))
            files, short, _ = J.split([os.path.join(region, "x.graph.jsonl.zst")], tmp)
            joined, conflicts, stale = J.chunk(G.tile_of(10.0, 50.0), files[G.tile_of(10.0, 50.0)], short)
        self.assertEqual(pieces(joined), pieces(g))
        self.assertEqual((conflicts, stale), (0, 0))


class Archive(unittest.TestCase):
    def test_chunks_read_back_as_one_graph(self):
        from compression import zstd
        lines = [way_line(8, [8.40, 8.47], 50.0, [[0, 1, 1], [1, 2, 0]]),
                 way_line(9, [8.47, 8.50], 50.0, [[0, 2, 0], [1, 3, 0]])]
        ents, bounds = [], []
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "graph.pmtiles")
            with tempfile.TemporaryFile() as data:
                for tid, t in sorted((J.zxy_to_tileid(G.CHUNK_ZOOM, *t), t)
                                     for t in {G.tile_of(8.40, 50.0), G.tile_of(8.47, 50.0)}):
                    blob = zstd.compress(G.encode(chunk(lines, t)[0], {}, {}))
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
