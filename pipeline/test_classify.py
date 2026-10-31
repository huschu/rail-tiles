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
        self.assertEqual(C.elec_system(ORBE), "construction:ac15000@16.7")
        rec = C.emit_props(ORBE, None)
        self.assertEqual((rec["elec"], rec["voltage"], rec["frequency"]), ("construction", 15000.0, 16.7))

    def test_future_system_wins_over_the_current_one(self):
        self.assertEqual(C.supply(dict(ORBE, voltage="750", frequency="0")), (15000.0, 16.7))

    def test_plain_tags_still_count_without_a_prefixed_system(self):
        p = {"railway": "rail", "electrified": "construction", "voltage": "25000", "frequency": "50"}
        self.assertEqual(C.supply(p), (25000.0, 50.0))

    def test_construction_prefix_marks_wire_going_up_on_a_line_without_it(self):
        p = {"railway": "rail", "electrified": "no", "construction:electrified": "contact_line",
             "construction:voltage": "25000", "construction:frequency": "50",
             "proposed:electrified": "contact_line"}
        rec = C.emit_props(p, None)
        self.assertEqual((rec["elec"], rec["voltage"], rec["frequency"]), ("construction", 25000.0, 50.0))
        self.assertNotIn("elec_proposed", rec)

    def test_wired_line_keeps_its_system_while_a_new_one_is_built(self):
        p = {"railway": "rail", "electrified": "contact_line", "voltage": "750", "frequency": "0",
             "construction:electrified": "contact_line", "construction:voltage": "15000"}
        self.assertEqual((C.electrification_state(p), C.supply(p)), ("electrified", (750.0, 0.0)))

    def test_wire_going_up_does_not_chain_with_wire_in_place(self):
        wired = {"railway": "rail", "usage": "main", "electrified": "contact_line",
                 "voltage": "15000", "frequency": "16.7"}
        building = dict(wired, electrified="construction")
        self.assertNotEqual(C.render_key(wired), C.render_key(building))
        self.assertNotEqual(C.render_key({"railway": "rail", "electrified": "contact_line"}),
                            C.render_key({"railway": "rail", "electrified": "construction"}))

    def test_planned_electrification_alone_leaves_the_line_unelectrified(self):
        p = {"railway": "rail", "electrified": "no", "proposed:electrified": "contact_line",
             "proposed:voltage": "15000"}
        self.assertEqual(C.supply(p), (None, None))


class ProposedElectrification(unittest.TestCase):
    DIESEL = {"railway": "rail", "usage": "main", "electrified": "no",
              "proposed:electrified": "contact_line", "proposed:voltage": "15000",
              "proposed:frequency": "16.7"}

    def test_line_in_service_carries_its_proposed_system_beside_its_own(self):
        rec = C.emit_props(self.DIESEL, None)
        self.assertEqual(rec["elec"], "non_electrified")
        self.assertNotIn("voltage", rec)
        self.assertEqual((rec["elec_proposed"], rec["proposed_voltage"], rec["proposed_frequency"]),
                         (True, 15000.0, 16.7))

    def test_proposal_without_a_voltage_is_still_marked(self):
        p = {"railway": "rail", "proposed:electrified": "contact_line"}
        rec = C.emit_props(p, None)
        self.assertEqual((rec["elec"], rec.get("elec_proposed")), ("unknown", True))
        self.assertNotIn("proposed_voltage", rec)

    def test_chains_do_not_merge_across_a_proposal(self):
        plain = dict(self.DIESEL)
        del plain["proposed:electrified"]
        self.assertNotEqual(C.render_key(self.DIESEL), C.render_key(plain))
        self.assertNotEqual(C.render_key(self.DIESEL), C.render_key(dict(self.DIESEL, **{"proposed:voltage": "25000"})))

    def test_only_lines_in_service_without_wire_get_a_proposal(self):
        for extra in ({"electrified": "contact_line", "voltage": "750"},
                      {"electrified": "construction"},
                      {"railway": "disused"},
                      {"proposed:electrified": "no"}):
            rec = C.emit_props(dict(self.DIESEL, **extra), None)
            self.assertNotIn("elec_proposed", rec, extra)


if __name__ == "__main__":
    unittest.main()


class ChangesOnLinesInService(unittest.TestCase):
    LINE = {"railway": "rail", "usage": "main", "maxspeed": "160", "gauge": "1668", "railway:pzb": "yes"}

    def rec(self, **tags):
        p = dict(self.LINE, **tags)
        return C.emit_props(p, C.speed_of(p), protection=C.protection_of(p))

    def test_upgrade_under_construction_carries_the_future_speed(self):
        rec = self.rec(**{"construction:maxspeed": "250", "proposed:maxspeed": "300"})
        self.assertEqual((rec["maxspeed"], rec["maxspeed_construction"]), (160, 250))
        self.assertNotIn("maxspeed_proposed", rec)

    def test_proposed_speed_reads_the_directional_tags_too(self):
        rec = self.rec(**{"proposed:maxspeed:forward": "200", "proposed:maxspeed:backward": "120"})
        self.assertEqual(rec["maxspeed_proposed"], 200)

    def test_gauge_conversion(self):
        rec = self.rec(**{"proposed:gauge": "1435"})
        self.assertEqual((rec["gauge_mm"], rec["gauge_proposed"]), (1668, "1435"))
        self.assertEqual(self.rec(**{"construction:gauge": "1435;1668"})["gauge_construction"], "1435;1668")

    def test_train_protection_being_installed(self):
        rec = self.rec(**{"construction:railway:etcs": "2"})
        self.assertEqual((rec["train_protection"], rec["tp_construction"], rec["tp_change_rank"]),
                         ("pzb", "etcs_2", 34))

    def test_no_change_when_the_future_value_is_the_current_one(self):
        rec = self.rec(**{"construction:maxspeed": "160", "proposed:gauge": "1668",
                          "construction:railway:pzb": "yes"})
        self.assertFalse({"maxspeed_construction", "gauge_proposed", "tp_construction"} & rec.keys())

    def test_only_lines_in_service_carry_changes(self):
        p = dict(self.LINE, railway="construction", **{"construction:railway": "rail",
                                                        "construction:maxspeed": "250"})
        rec = C.emit_props(p, C.speed_of(p))
        self.assertEqual(rec["maxspeed"], 250)
        self.assertNotIn("maxspeed_construction", rec)

    def test_chains_do_not_run_across_a_change(self):
        def key(**tags):
            return C.render_key(dict(self.LINE, **tags))
        self.assertNotEqual(key(), key(**{"proposed:maxspeed": "250"}))
        self.assertNotEqual(key(**{"proposed:maxspeed": "250"}), key(**{"construction:maxspeed": "250"}))
        self.assertEqual(key(**{"proposed:maxspeed": "230"}), key(**{"proposed:maxspeed": "250"}),
                         "same band merges")

    def test_an_unchanged_construction_value_lets_the_proposed_one_through(self):
        rec = self.rec(**{"construction:maxspeed": "160", "proposed:maxspeed": "250",
                          "construction:railway:pzb": "yes", "proposed:railway:etcs": "2"})
        self.assertEqual((rec["maxspeed_proposed"], rec["tp_proposed"]), (250, "etcs_2"))

    def test_new_line_reads_protection_like_its_other_tags(self):
        new = {"railway": "construction", "construction:railway": "rail"}
        for tags in ({"proposed:railway:etcs": "2"}, {"railway:etcs": "2"}):
            self.assertEqual(C.protection_of(dict(new, **tags))[0], "etcs_2", tags)
        both = dict(new, **{"construction:railway:etcs": "1", "proposed:railway:etcs": "2"})
        self.assertEqual(C.protection_of(both)[0], "etcs_1")
        self.assertEqual(C.protection_of({"railway": "rail", "proposed:railway:etcs": "2"}), (None, None),
                         "a line in service shows its proposal as a change, not its system")
