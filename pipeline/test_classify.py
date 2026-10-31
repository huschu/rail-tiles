"""Lifecycle-prefixed tags on construction and proposed lines.

Run: python3 -m unittest discover -s pipeline -p 'test_*.py'  (or `make test`)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import classify as C

# OSM way 1466149512, the East Coast Rail Link: under construction, with its
# planned 25 kV 50 Hz system tagged only under proposed:.
ECRL = {"railway": "construction", "construction:railway": "rail", "usage": "main",
        "gauge": "1435", "proposed:electrified": "contact_line",
        "proposed:voltage": "25000", "proposed:frequency": "50"}


class LifecyclePrefixes(unittest.TestCase):
    def test_construction_line_reads_planned_system_from_proposed_tags(self):
        self.assertEqual(C.lifecycle(ECRL), "construction")
        self.assertEqual(C.electrification_state(ECRL), "electrified")
        self.assertEqual(C.elec_system(ECRL), "ac25000@50.0")
        rec = C.emit_props(ECRL, None)
        self.assertEqual((rec["lifecycle"], rec["elec"], rec["voltage"], rec["frequency"]),
                         ("construction", "electrified", 25000.0, 50.0))

    def test_construction_prefix_wins_over_proposed_and_plain(self):
        p = dict(ECRL, **{"construction:voltage": "15000", "voltage": "750"})
        self.assertEqual(C.first_num(C._get(p, "voltage")), 15000.0)

    def test_proposed_prefix_wins_over_plain_tag(self):
        p = dict(ECRL, electrified="no")
        self.assertEqual(C.electrification_state(p), "electrified")

    def test_proposed_line_ignores_construction_prefix(self):
        p = {"railway": "proposed", "proposed:railway": "rail",
             "construction:voltage": "750", "proposed:voltage": "3000"}
        self.assertEqual(C.first_num(C._get(p, "voltage")), 3000.0)

    def test_line_in_service_ignores_prefixed_tags(self):
        p = {"railway": "rail", "electrified": "no", "proposed:electrified": "contact_line"}
        self.assertEqual(C.electrification_state(p), "non_electrified")


# Orbe-Chavornay: in service, its wire being replaced by 15 kV AC.
ORBE = {"railway": "rail", "usage": "branch", "electrified": "construction",
        "proposed:electrified": "contact_line", "proposed:voltage": "15000",
        "proposed:frequency": "16.7"}


class WireUnderConstruction(unittest.TestCase):
    def test_line_in_service_shows_the_system_being_put_up(self):
        self.assertEqual(C.lifecycle(ORBE), "present")
        self.assertEqual(C.electrification_state(ORBE), "construction")
        self.assertEqual(C.elec_system(ORBE), "ac15000@16.7")
        rec = C.emit_props(ORBE, None)
        self.assertEqual((rec["elec"], rec["voltage"], rec["frequency"]), ("construction", 15000.0, 16.7))

    def test_future_system_wins_over_the_current_one(self):
        self.assertEqual(C.supply(dict(ORBE, voltage="750", frequency="0")), (15000.0, 16.7))

    def test_plain_tags_still_count_without_a_prefixed_system(self):
        p = {"railway": "rail", "electrified": "construction", "voltage": "25000", "frequency": "50"}
        self.assertEqual(C.supply(p), (25000.0, 50.0))

    def test_planned_electrification_alone_leaves_the_line_unelectrified(self):
        p = {"railway": "rail", "electrified": "no", "proposed:electrified": "contact_line",
             "proposed:voltage": "15000"}
        self.assertEqual(C.supply(p), (None, None))


if __name__ == "__main__":
    unittest.main()
