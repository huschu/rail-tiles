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


def ids(trains):
    return [t["id"] for t in trains]


class NightTrainTests(unittest.TestCase):
    def test_a_short_sleeper_counts(self):
        out = N.decide([region(rel(1, [10], sleeping_car="yes", service="long_distance"), **{"10": 50 * KM})])
        self.assertEqual(ids(out), [1])

    def test_a_couchette_counts_without_service_night(self):
        self.assertEqual(ids(N.decide([region(rel(1, [10], couchette="yes"), **{"10": KM})])), [1])

    def test_a_short_service_night_is_a_local_night_train(self):
        self.assertEqual(N.decide([region(rel(1, [10], service="night"), **{"10": 80 * KM})]), [])

    def test_a_long_service_night_counts_in_a_semicolon_list(self):
        out = N.decide([region(rel(1, [10], service="night;motorail"), **{"10": 900 * KM})])
        self.assertEqual(out[0]["km"], 900)

    def test_sleeping_car_no_does_not_count(self):
        self.assertFalse(N.candidate({"route": "train", "sleeping_car": "no", "service": "long_distance"}))
        self.assertFalse(N.candidate({"route": "bus", "service": "night"}))

    def test_partials_under_the_limit_add_up_across_borders(self):
        r = rel(1, [10, 20], service="night")
        out = N.decide([region(r, **{"10": 200 * KM}), region(r, **{"20": 200 * KM})])
        self.assertEqual([(t["id"], t["km"]) for t in out], [(1, 400)])

    def test_a_way_in_two_overlapping_extracts_counts_once(self):
        r = rel(1, [10, 20], service="night")
        out = N.decide([region(r, **{"10": 250 * KM, "20": 40 * KM}),
                        region(r, **{"20": 40 * KM})])
        self.assertEqual(out, [])

    def test_the_newest_version_of_a_relation_wins(self):
        old = rel(1, [10], version=3, service="night")
        new = rel(1, [10, 20], version=4, service="night")
        lengths = {"10": 200 * KM, "20": 200 * KM}
        self.assertEqual(ids(N.decide([region(new, **lengths), region(old, **lengths)])), [1])
        self.assertEqual(ids(N.decide([region(old, **lengths), region(new, **lengths)])), [1])

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
