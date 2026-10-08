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


class Kind(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(S.station_kind({"railway": "station"}), "train")
        self.assertEqual(S.station_kind({"railway": "station", "station": "subway"}), "subway")
        self.assertEqual(S.station_kind({"railway": "halt", "light_rail": "yes"}), "light_rail")
        self.assertIsNone(S.station_kind({"public_transport": "station", "bus": "yes"}))
        self.assertEqual(S.station_kind({"public_transport": "station", "train": "yes"}), "train")
        self.assertIsNone(S.station_kind({"railway": "tram_stop"}))


if __name__ == "__main__":
    unittest.main()
