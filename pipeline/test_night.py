"""Night trains from route relations.

Run: python3 -m unittest discover -s pipeline -p 'test_*.py'  (or `make test`)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import night as N

KM = 1000


def rel(rid, ways, version=1, **tags):
    return {"id": rid, "version": version, "tags": {"route": "train", **tags},
            "ways": ways, "stops": []}


def region(*relations, **lengths):
    return {"relations": list(relations), "lengths": lengths}


def decide(sidecars):
    return N.decide(sidecars)["trains"]


def master(mid, routes, version=1, **tags):
    return {"id": mid, "version": version,
            "tags": {"type": "route_master", "route_master": "train", **tags}, "routes": routes}


def ids(trains):
    return [t["id"] for t in trains]


class NightTrainTests(unittest.TestCase):
    def test_a_short_sleeper_counts(self):
        out = decide([region(rel(1, [10], sleeping_car="yes", service="long_distance"), **{"10": 50 * KM})])
        self.assertEqual(ids(out), [1])

    def test_a_couchette_counts_without_service_night(self):
        self.assertEqual(ids(decide([region(rel(1, [10], couchette="yes"), **{"10": KM})])), [1])

    def test_a_short_service_night_is_a_local_night_train(self):
        self.assertEqual(decide([region(rel(1, [10], service="night"), **{"10": 80 * KM})]), [])

    def test_a_long_service_night_counts_in_a_semicolon_list(self):
        out = decide([region(rel(1, [10], service="night;motorail"), **{"10": 900 * KM})])
        self.assertEqual(out[0]["km"], 900)

    def test_sleeping_car_no_does_not_count(self):
        self.assertFalse(N.candidate({"route": "train", "sleeping_car": "no", "service": "long_distance"}))
        self.assertFalse(N.candidate({"route": "bus", "service": "night"}))

    def test_partials_under_the_limit_add_up_across_borders(self):
        r = rel(1, [10, 20], service="night")
        out = decide([region(r, **{"10": 200 * KM}), region(r, **{"20": 200 * KM})])
        self.assertEqual([(t["id"], t["km"]) for t in out], [(1, 400)])

    def test_a_way_in_two_overlapping_extracts_counts_once(self):
        r = rel(1, [10, 20], service="night")
        out = decide([region(r, **{"10": 250 * KM, "20": 40 * KM}),
                        region(r, **{"20": 40 * KM})])
        self.assertEqual(out, [])

    def test_the_newest_version_of_a_relation_wins(self):
        old = rel(1, [10], version=3, service="night")
        new = rel(1, [10, 20], version=4, service="night")
        lengths = {"10": 200 * KM, "20": 200 * KM}
        self.assertEqual(ids(decide([region(new, **lengths), region(old, **lengths)])), [1])
        self.assertEqual(ids(decide([region(old, **lengths), region(new, **lengths)])), [1])

    def test_a_route_master_groups_trains_into_one_service(self):
        lengths = {"10": 900 * KM, "20": 900 * KM}
        sc = region(rel(1, [10], service="night"), rel(2, [20], service="night"),
                    rel(3, [10], sleeping_car="yes"), **lengths)
        sc["masters"] = [master(9, [1, 2, 77], ref="NJ 294;NJ 295")]
        out = N.decide([sc])
        self.assertEqual([t.get("service") for t in out["trains"]], [9, 9, None])
        self.assertEqual(out["services"], [{"id": 9, "tags": sc["masters"][0]["tags"]}])

    def test_a_train_two_masters_list_goes_to_the_lower_id(self):
        sc = region(rel(1, [10], sleeping_car="yes"), **{"10": KM})
        sc["masters"] = [master(9, [1]), master(4, [1])]
        self.assertEqual(decide([sc])[0]["service"], 4)

    def test_sidecar_keeps_route_masters_that_list_a_candidate(self):
        lines = ["r7 v2 dV c1 t2026-01-01T00:00:00Z i1 uX Troute=train,service=night Mw10@",
                 "r9 v1 dV c1 t2026-01-01T00:00:00Z i1 uX Ttype=route_master,route_master=train Mr7@,r8@",
                 "r5 v1 dV c1 t2026-01-01T00:00:00Z i1 uX Ttype=route_master,route_master=train Mr8@"]
        sc = N.sidecar(lines, {})
        self.assertEqual([(m["id"], m["routes"]) for m in sc["masters"]], [(9, [7, 8])])

    def test_sidecar_keeps_full_members_and_lengths_of_held_ways(self):
        line = ("r7 v2 dV c1 t2026-01-01T00:00:00Z i1 uX "
                "Troute=train,service=night,ref=NJ%20%421 "
                "Mn5@stop,n6@platform,n8@stop_exit_only,w10@,w11@")
        sc = N.sidecar([line], {"w10": [(16.0, 48.0), (16.0, 49.0)]})
        [r] = sc["relations"]
        self.assertEqual((r["id"], r["version"], r["tags"]["ref"]), (7, 2, "NJ 421"))
        self.assertEqual(r["ways"], [10, 11])
        self.assertEqual(r["stops"], [5, 8])
        self.assertAlmostEqual(sc["lengths"]["10"], 111195, delta=1)
        self.assertNotIn("11", sc["lengths"])


if __name__ == "__main__":
    unittest.main()
