import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph as G
import graph_join as J

RAIL = ["rail", None, "main", None, 160]


def way_line(wid, lons, lat, cuts):
    t = G.tile_of(lons[0], lat)
    return json.dumps([list(t), wid, RAIL, [round(x * 1e7) for x in lons],
                       [round(lat * 1e7)] * len(lons), cuts]) + "\n"


def chunk(lines, t):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.writelines(lines)
    try:
        return J.chunk(t, f.name)
    finally:
        os.remove(f.name)


def pieces(g):
    return sorted((g.way[e], g.i0[e], g.i1[e]) for e in range(g.edge_count))


class Chunk(unittest.TestCase):
    def test_a_way_is_cut_wherever_any_region_cut_it(self):
        lons = [10.0, 10.01, 10.02, 10.03, 10.04]
        here = [[0, 1, 0], [4, 5, 0]]
        neighbour = [[0, 1, 0], [2, 3, 1], [4, 5, 0]]  # its branch meets the way at node 3
        g, conflicts = chunk([way_line(7, lons, 50.0, here), way_line(7, lons, 50.0, neighbour)],
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
        _, conflicts = chunk([way_line(9, lons, 50.0, [[0, 1, 0], [1, 2, 0]]),
                              way_line(9, lons, 50.0, [[0, 1, 0], [1, 3, 0]])], G.tile_of(10.0, 50.0))
        self.assertEqual(conflicts, 1)


class Stations(unittest.TestCase):
    def test_a_border_station_gets_both_regions_stops_and_services(self):
        merged = {}
        J._merge_station(merged, {"id": "n1", "name": "Basel SBB", "kind": "train", "lon": 0, "lat": 0,
                                  "routes": [["train", "IC 5", None, "SBB", [1]]], "stops": [10]})
        J._merge_station(merged, {"id": "n1", "name": "Basel SBB", "kind": "train", "lon": 0, "lat": 0,
                                  "routes": [["train", "IC 5", 0xFF0000, "SBB", [2]],
                                             ["train", "RE 7", None, "DB", [3]]], "stops": [11]})
        st = merged["n1"]
        self.assertEqual(st["stops"], [10, 11])
        self.assertEqual(st["routes"], [["train", "RE 7", None, "DB", [3]],
                                        ["train", "IC 5", 0xFF0000, "SBB", [1, 2]]])


class Archive(unittest.TestCase):
    def test_chunks_read_back_as_one_graph(self):
        from compression import zstd
        lines = [way_line(8, [8.40, 8.47], 50.0, [[0, 1, 1], [1, 2, 0]]),
                 way_line(9, [8.47, 8.50], 50.0, [[0, 2, 0], [1, 3, 0]])]
        tiles = []
        for t in {G.tile_of(8.40, 50.0), G.tile_of(8.47, 50.0)}:
            g, _ = chunk(lines, t)
            tiles.append((J.zxy_to_tileid(G.CHUNK_ZOOM, *t), t, zstd.compress(G.encode(g, {}, {}))))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "graph.pmtiles")
            J.write_archive(tiles, path)
            g = J.load(path)
        self.assertEqual(pieces(g), [(8, 0, 1), (9, 0, 1)])
        self.assertEqual(sorted(g.node_id), [1, 2, 3])
        self.assertTrue(g.stop[g.node_index[1]])


if __name__ == "__main__":
    unittest.main()
