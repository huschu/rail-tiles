import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stations as S


def station(sid, lon, lat, kind="train", name=None):
    return {"id": sid, "name": name, "kind": kind, "halt": False, "lon": lon, "lat": lat}


def rel(rid, tags, members):
    return {"rid": rid, "tags": tags, "members": members}


class Calls(unittest.TestCase):
    def setUp(self):
        self.stations = {
            "n1": station("n1", -3.6823, 40.4721),                      # main-line station
            "n2": station("n2", -3.6827, 40.4740, kind="subway"),       # metro beside it
            "n3": station("n3", -3.7000, 40.5000),                      # no stop area
        }
        self.stops = {"n10": (-3.6824, 40.4722), "n30": (-3.7001, 40.5001), "n99": (-3.0, 41.0)}

    def test_stop_area_names_the_station(self):
        rels = [
            rel("100", {"public_transport": "stop_area"}, [("n1", ""), ("n10", "stop"), ("w5", "platform")]),
            rel("200", {"route": "train", "ref": "C-3"}, [("n10", "stop"), ("w9", "")]),
            rel("201", {"route": "train", "ref": "C-3"}, [("w5", "platform_entry_only")]),
        ]
        got = S.calls(rels, self.stations, self.stops)
        self.assertEqual(got["n1"], {"200", "201"})

    def test_mixed_stop_area_keeps_modes_apart(self):
        rels = [
            rel("100", {"public_transport": "stop_area"}, [("n1", ""), ("n2", ""), ("n10", "stop")]),
            rel("300", {"route": "subway", "ref": "L1"}, [("n10", "stop")]),
            rel("200", {"route": "train", "ref": "C-3"}, [("n10", "stop")]),
        ]
        got = S.calls(rels, self.stations, self.stops)
        self.assertEqual(got["n1"], {"200"})
        self.assertEqual(got["n2"], {"300"})

    def test_unplaced_stop_goes_to_nearest_station_in_reach(self):
        rels = [
            rel("400", {"route": "train", "ref": "R-1"}, [("n30", "stop"), ("n99", "stop")]),
        ]
        got = S.calls(rels, self.stations, self.stops)
        self.assertEqual(got, {"n3": {"400"}})

    def test_a_tram_calls_at_its_tram_stop_not_the_station_beside_it(self):
        self.stations["n4"] = station("n4", -3.6822, 40.4720, kind="tram")
        rels = [
            rel("100", {"public_transport": "stop_area"}, [("n1", ""), ("n4", "stop"), ("n10", "stop")]),
            rel("600", {"route": "tram", "ref": "T1"}, [("n4", "stop")]),
        ]
        got = S.calls(rels, self.stations, self.stops)
        self.assertEqual(got, {"n4": {"600"}})

    def test_a_route_without_stop_roles_calls_nowhere(self):
        rels = [rel("500", {"route": "train", "ref": "X"}, [("n30", ""), ("w1", "")])]
        self.assertEqual(S.calls(rels, self.stations, self.stops), {})


class Services(unittest.TestCase):
    def test_directions_group_into_one_service(self):
        rels = {
            "2": {"tags": {"route": "train", "ref": "C-10", "network": "Cercanías Madrid", "colour": "#9ACD32"}},
            "1": {"tags": {"route": "train", "ref": "C-10", "network": "Cercanías Madrid"}},
            "3": {"tags": {"route": "train", "ref": "C-2", "network": "Cercanías Madrid"}},
            "4": {"tags": {"route": "subway", "ref": "L1", "network": "Metro"}},
            "5": {"tags": {"route": "train", "name": "Iryo"}},
            "6": {"tags": {"route": "train"}},                       # no title: left out
        }
        got = S.services(rels.keys(), rels)
        self.assertEqual(got, [
            ["train", "C-2", None, "Cercanías Madrid", [3]],
            ["train", "C-10", 0x9ACD32, "Cercanías Madrid", [1, 2]],
            ["train", "Iryo", None, None, [5]],
            ["subway", "L1", None, "Metro", [4]],
        ])


class Merge(unittest.TestCase):
    def feature(self, sid, minzoom, routes):
        return {"type": "Feature", "tippecanoe": {"minzoom": minzoom},
                "properties": {"osm_id": sid, "kind": "train", "name": "Basel SBB",
                               "routes": json.dumps(routes)},
                "geometry": {"type": "Point", "coordinates": [7.59, 47.55]}}

    def test_a_border_station_lists_the_services_of_both_regions(self):
        swiss = [self.feature("n1", 9, [["train", "IC 3", None, "SBB", [1, 2]]])]
        german = [self.feature("n1", 12, [["train", "IC 3", 0xE4013A, "SBB", [2, 3]],
                                          ["train", "ICE 20", None, "DB", [7]]]),
                  self.feature("n2", 12, [])]
        got = {f["properties"]["osm_id"]: f for f in S.merge([swiss, german])}
        self.assertEqual(set(got), {"n1", "n2"})
        self.assertEqual(got["n1"]["tippecanoe"]["minzoom"], 9)
        self.assertEqual(json.loads(got["n1"]["properties"]["routes"]), [
            ["train", "ICE 20", None, "DB", [7]],
            ["train", "IC 3", 0xE4013A, "SBB", [1, 2, 3]],
        ])


class AreaDuplicates(unittest.TestCase):
    def test_an_area_around_its_own_node_is_dropped(self):
        got = S._drop_area_duplicates({
            "n1": station("n1", -3.6823, 40.4721, name="Chamartín"),
            "w2": station("w2", -3.6825, 40.4722, name="Chamartín"),
        })
        self.assertEqual(set(got), {"n1"})

    def test_a_differently_named_station_nearby_is_kept(self):
        got = S._drop_area_duplicates({
            "n1": station("n1", -3.6823, 40.4721, name="Norte"),
            "w2": station("w2", -3.6830, 40.4725, name="Sur"),
        })
        self.assertEqual(set(got), {"n1", "w2"})


class Kind(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(S.station_kind({"railway": "station"}), "train")
        self.assertEqual(S.station_kind({"railway": "station", "station": "subway"}), "subway")
        self.assertEqual(S.station_kind({"railway": "halt", "light_rail": "yes"}), "light_rail")
        self.assertIsNone(S.station_kind({"public_transport": "station", "bus": "yes"}))
        self.assertEqual(S.station_kind({"public_transport": "station", "train": "yes"}), "train")
        self.assertEqual(S.station_kind({"railway": "tram_stop"}), "tram")


if __name__ == "__main__":
    unittest.main()
