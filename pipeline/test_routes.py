"""Line colours from route relations.

Run: python3 -m unittest discover -s pipeline -p 'test_*.py'  (or `make test`)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import classify as C
import routes as R


def rel(ways, **tags):
    return (tags, ways)


class RouteColourTests(unittest.TestCase):
    def test_a_way_with_one_coloured_service_takes_its_colour(self):
        out = R.way_colours([rel(["w1", "w2"], route="subway", network="BVG", ref="U1", colour="#7DAD4C")])
        self.assertEqual(out, {"w1": 0x7DAD4C, "w2": 0x7DAD4C})

    def test_both_directions_count_as_one_service(self):
        out = R.way_colours([
            rel(["w1"], route="subway", network="BVG", ref="U1", colour="#7DAD4C"),
            rel(["w1"], route="subway", network="BVG", ref="U1"),
        ])
        self.assertEqual(out, {"w1": 0x7DAD4C})

    def test_shared_track_stays_uncoloured(self):
        out = R.way_colours([
            rel(["w1", "w2"], route="train", network="VBB", ref="S3", colour="#0066AD"),
            rel(["w2"], route="train", network="VBB", ref="S5", colour="#EB7405"),
        ])
        self.assertEqual(out, {"w1": 0x0066AD, "w2": R.SHARED})

    def test_services_without_a_colour_do_not_count(self):
        out = R.way_colours([
            rel(["w1"], route="light_rail", network="VBB", ref="S2", colour="#007734"),
            rel(["w1"], route="train", network="VBB", ref="RE5"),
        ])
        self.assertEqual(out, {"w1": 0x007734})

    def test_lines_sharing_one_colour_colour_the_track(self):
        out = R.way_colours([
            rel(["w1"], route="tram", network="BVG", ref="M10", colour="#BE1414"),
            rel(["w1"], route="tram", network="BVG", ref="12", colour="#BE1414"),
        ])
        self.assertEqual(out, {"w1": 0xBE1414})

    def test_near_shades_colour_the_track_with_one_of_them(self):
        ring = [
            rel(["w1", "w2"], route="light_rail", network="VBB", ref="S42", colour="#C36939"),
            rel(["w1", "w2"], route="light_rail", network="VBB", ref="S41", colour="#9F4C37"),
        ]
        self.assertEqual(R.way_colours(ring), {"w1": 0x9F4C37, "w2": 0x9F4C37})

    def test_clearly_different_shades_are_marked_shared(self):
        out = R.way_colours([
            rel(["w1"], route="light_rail", network="VBB", ref="S41", colour="#9F4C37"),
            rel(["w1"], route="light_rail", network="VBB", ref="S46", colour="#BA8A4D"),
        ])
        self.assertEqual(out, {"w1": R.SHARED})

    def test_shared_track_is_emitted_as_a_flag_not_a_colour(self):
        rec = C.emit_props({"railway": "rail"}, None, colour=R.SHARED)
        self.assertTrue(rec["colour_shared"])
        self.assertNotIn("colour", rec)

    def test_the_colour_most_services_use_wins(self):
        out = R.way_colours([
            rel(["w1"], route="tram", network="X", ref="1", colour="#BE1414"),
            rel(["w1"], route="tram", network="X", ref="2", colour="#C01818"),
            rel(["w1"], route="tram", network="X", ref="3", colour="#C01818"),
        ])
        self.assertEqual(out, {"w1": 0xC01818})

    def test_same_ref_in_two_networks_is_two_services(self):
        out = R.way_colours([
            rel(["w1"], route="train", network="MVV", ref="S1", colour="#16C0E9"),
            rel(["w2"], route="train", network="RMV", ref="S1", colour="#0080C8"),
        ])
        self.assertEqual(out, {"w1": 0x16C0E9, "w2": 0x0080C8})

    def test_directions_disagreeing_on_colour_leave_it_out(self):
        out = R.way_colours([
            rel(["w1"], route="subway", network="X", ref="A", colour="green"),
            rel(["w1"], route="subway", network="X", ref="A", colour="blue"),
        ])
        self.assertEqual(out, {})

    def test_bus_routes_and_unnamed_routes_are_ignored(self):
        out = R.way_colours([
            rel(["w1"], route="bus", ref="100", colour="red"),
            rel(["w2"], route="tram", colour="red"),
        ])
        self.assertEqual(out, {})

    def test_the_ways_own_colour_tag_wins(self):
        self.assertEqual(C.colour_of({"colour": "#ff0000", C.ROUTE_COLOUR: 0x00FF00}), 0xFF0000)
        self.assertEqual(C.colour_of({C.ROUTE_COLOUR: 0x00FF00}), 0x00FF00)
        self.assertIsNone(C.colour_of({}))

    def test_opl_parsing_unescapes_tags_and_keeps_only_way_members(self):
        line = ("r42 v3 dV c1 t2026-01-01T00:00:00Z i1 uX "
                "Troute=subway,ref=U1,name=U1%20%Uhlandstra%df%e,colour=%23%7DAD4C "
                "Mn5@stop,w10@,w11@forward,r7@")
        [(tags, ways)] = R.parse_opl([line])
        self.assertEqual(tags["name"], "U1 Uhlandstraße")
        self.assertEqual(tags["colour"], "#7DAD4C")
        self.assertEqual(ways, ["w10", "w11"])


if __name__ == "__main__":
    unittest.main()
