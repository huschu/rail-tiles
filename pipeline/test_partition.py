import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import partition


class Check(unittest.TestCase):
    def test_a_way_left_to_a_neighbour_that_kept_it_is_fine(self):
        gaps, dups, missing = partition.check({
            "austria": {"given": {}, "kept": ["w1", "w2"]},
            "germany": {"given": {"w1": "austria"}, "kept": ["w3"]},
        })
        self.assertEqual((gaps, dups, missing), ({}, {}, {}))

    def test_a_way_the_neighbour_did_not_keep_is_lost(self):
        gaps, _, _ = partition.check({
            "austria": {"given": {}, "kept": ["w2"]},
            "germany": {"given": {"w1": "austria"}, "kept": []},
        })
        self.assertEqual(gaps, {"germany -> austria": 1})

    def test_a_way_kept_twice_and_a_neighbour_without_a_record(self):
        _, dups, missing = partition.check({
            "austria": {"given": {"w9": "albania"}, "kept": ["w1"]},
            "germany": {"given": {}, "kept": ["w1"]},
        })
        self.assertEqual(dups, {"austria + germany": 1})
        self.assertEqual(missing, {"albania": 1})


if __name__ == "__main__":
    unittest.main()
