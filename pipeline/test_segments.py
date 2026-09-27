"""Chain segment attributes and the structure level-of-detail.

Run: python3 -m unittest discover -s pipeline -p 'test_*.py'  (or `make test`)
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import classify as C
import geometry as G
from build_region import seg_attrs, segment_chain

LAT = 48.0
M = 1 / 111320.0  # degrees of latitude per metre
BASE = {"railway": "rail", "usage": "main", "electrified": "contact_line",
        "voltage": "15000", "frequency": "16.7", "gauge": "1435", "maxspeed": "160"}


def way(wid, start_m, end_m, **tags):
    """A straight way along a meridian from start_m to end_m metres north."""
    return {"id": wid, "props": dict(BASE, **tags),
            "coords": [(10.8, LAT + start_m * M), (10.8, LAT + end_m * M)]}


def chained(ways):
    chains = G.chain(ways, C.render_key, C.speed_of, C.band_of)
    assert len(chains) == 1, chains
    ch = chains[0]
    ch["seg_attrs"] = seg_attrs(ch, math.cos(math.radians(LAT)), {w["id"]: w["props"] for w in ways})
    return ch


def extent_m(sub):
    ys = [round((c[1] - LAT) / M) for c in sub["coords"]]
    return min(ys), max(ys)


class SegmentAttributes(unittest.TestCase):
    """A culvert between two long at-grade ways: the bridge tag must stay on
    the culvert's own 40 m and not move to the following way's first segment."""

    def setUp(self):
        self.ways = [way("wA", 0, 3000), way("wB", 3000, 3040, bridge="yes"),
                     way("wC", 3040, 6040)]

    def test_each_segment_carries_its_own_ways_tags(self):
        ch = chained(self.ways)
        self.assertEqual(ch["seg_attrs"]["bridge"], [False, True, False])

    def test_bridge_keeps_its_extent_and_identity_where_it_resolves(self):
        # z11 at 48° N: 1.5 px is ~38 m, so the 40 m culvert is kept.
        subs = segment_chain(chained(self.ways), 11, LAT)
        bridges = [s for s in subs if s["bridge"]]
        self.assertEqual([extent_m(s) for s in bridges], [(3000, 3040)])
        self.assertEqual(set(bridges[0]["src"]), {"wB"})

    def test_speed_follows_its_own_way(self):
        ways = [way("wA", 0, 3000, maxspeed="100"), way("wB", 3000, 6000, maxspeed="120")]
        self.assertEqual(chained(ways)["seg_attrs"]["speed_raw"], [100, 120])


class StructureLevelOfDetail(unittest.TestCase):
    def test_gap_between_bridges_stays_at_grade(self):
        out = G.attribute_lod([True, False, True], [500, 30, 500], 100, no_grow={True})
        self.assertEqual(out, [True, False, True])

    def test_short_bridge_still_dissolves(self):
        out = G.attribute_lod([False, True, False], [500, 30, 500], 100, no_grow={True})
        self.assertEqual(out, [False, False, False])

    def test_short_bridge_at_the_end_of_a_line_dissolves(self):
        out = G.attribute_lod([False, True], [500, 30], 100, no_grow={True})
        self.assertEqual(out, [False, False])

    def test_other_attributes_merge_both_ways(self):
        self.assertEqual(G.attribute_lod([2, 3, 2], [500, 30, 500], 100), [2, 2, 2])
        self.assertEqual(G.attribute_lod([True, False, True], [500, 30, 500], 100),
                         [True, True, True])

    def test_culverts_vanish_below_their_zoom_without_joining(self):
        # z10 at 48° N: 1.5 px is ~77 m. Two 40 m culverts 500 m apart dissolve
        # into at-grade track rather than growing into one bridge.
        ways = [way("wA", 0, 3000), way("wB", 3000, 3040, bridge="yes"),
                way("wC", 3040, 3540), way("wD", 3540, 3580, bridge="yes"),
                way("wE", 3580, 6000)]
        subs = segment_chain(chained(ways), 10, LAT)
        self.assertFalse(any(s["bridge"] for s in subs))


if __name__ == "__main__":
    unittest.main()
