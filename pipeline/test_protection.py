"""Train protection: which system a way shows when it carries several.

Run: python3 -m unittest discover -s pipeline -p 'test_*.py'  (or `make test`)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_protection as G
import protection as P

FIXTURE = P.index({
    "none_rank": 1,
    "no_values": ["no", "none"],
    "keys": ["railway:top", "railway:mid", "railway:mid-variant", "railway:split", "railway:low",
             "railway:unlisted"],
    "systems": [
        {"id": "top", "rank": 5, "tags": [{"tag": "railway:top", "values": ["yes"]}]},
        {"id": "mid", "rank": 4, "tags": [{"tag": "railway:mid", "values": ["1", "2"]},
                                          {"tag": "railway:mid-variant", "values": ["yes"]}]},
        {"id": "split", "rank": 3, "tags": [{"tag": "railway:split", "values": ["a"]}]},
        {"id": "low", "rank": 2, "tags": [{"tag": "railway:low", "values": ["yes"]}]},
        {"id": "split", "rank": 6, "tags": [{"tag": "railway:split", "values": ["b"]}]},
    ],
})


def tp(tags, idx=P._INDEX):
    """(system, rank) for railway:<key>=<value> tags."""
    return P.train_protection({f"railway:{k}": v for k, v in tags.items()}, "", idx)


class Mechanism(unittest.TestCase):
    def test_the_highest_rank_wins(self):
        self.assertEqual(tp({"low": "yes", "top": "yes", "mid": "1"}, FIXTURE), ("top", 5))
        self.assertEqual(tp({"low": "yes", "mid": "2"}, FIXTURE), ("mid", 4))

    def test_any_tag_of_an_entry_matches(self):
        self.assertEqual(tp({"mid-variant": "yes"}, FIXTURE), ("mid", 4))

    def test_one_id_ranks_per_entry(self):
        self.assertEqual(tp({"split": "a"}, FIXTURE), ("split", 3))
        self.assertEqual(tp({"split": "b", "top": "yes"}, FIXTURE), ("split", 6))

    def test_unlisted_values_do_not_match(self):
        self.assertEqual(tp({"top": "construction"}, FIXTURE), (None, None))
        self.assertEqual(tp({"low": "yes", "top": "fixme"}, FIXTURE), ("low", 2))

    def test_none_only_when_every_protection_key_says_no(self):
        self.assertEqual(tp({"top": "no", "low": "no"}, FIXTURE), ("none", 1))
        self.assertEqual(tp({"top": "no", "low": "none"}, FIXTURE), ("none", 1))
        self.assertEqual(tp({"top": "no", "low": "construction"}, FIXTURE), (None, None))
        self.assertEqual(tp({}, FIXTURE), (None, None))

    def test_a_left_out_key_keeps_a_way_from_reading_as_none(self):
        self.assertEqual(tp({"top": "no", "unlisted": "yes"}, FIXTURE), (None, None))
        self.assertEqual(tp({"top": "no", "unlisted": "no"}, FIXTURE), ("none", 1))

    def test_lines_being_built_read_the_prefixed_keys(self):
        tags = {"construction:railway:mid": "2", "railway:top": "yes"}
        self.assertEqual(P.train_protection(tags, "construction:", FIXTURE), ("mid", 4))


class Steady(unittest.TestCase):
    def test_small_moves_keep_the_previous_count(self):
        self.assertEqual(G.steady(48721, 47663), 47663)
        self.assertEqual(G.steady(40000, 47663), 47663)

    def test_large_moves_or_new_entries_take_the_new_count(self):
        self.assertEqual(G.steady(70000, 47663), 70000)
        self.assertEqual(G.steady(30000, 47663), 30000)
        self.assertEqual(G.steady(500, None), 500)


class Rules(unittest.TestCase):
    def test_no_key_is_both_listed_and_left_out(self):
        self.assertFalse(set(G.LEFT_OUT) & {k for _, _, tags in G.SYSTEMS for k in tags})


class ShippedTable(unittest.TestCase):
    """What holds for the shipped table whatever the way counts are."""

    def test_tiers_order_the_ranks(self):
        tier = {sid: t for sid, t, _ in G.SYSTEMS}
        by_tier = {}
        for s in P.TABLE["systems"]:
            by_tier.setdefault(tier[s["id"]], []).append(s["rank"])
        self.assertGreater(min(by_tier[G.OVERLAY]), max(by_tier[G.NATIONAL]))
        self.assertGreater(min(by_tier[G.NATIONAL]), max(by_tier[G.METHOD]))
        self.assertGreater(min(by_tier[G.METHOD]), P.TABLE["none_rank"])

    def test_migration_standards_beat_national_systems(self):
        self.assertEqual(tp({"etcs": "2", "pzb": "yes", "lzb": "yes"})[0], "etcs_2")
        self.assertEqual(tp({"etcs": "1", "kvb": "yes", "tvm": "430"})[0], "etcs_1")
        self.assertEqual(tp({"cbtc": "yes", "kvb": "yes"})[0], "cbtc")
        self.assertEqual(tp({"twc": "yes", "ptc": "yes"})[0], "ptc")

    def test_variants_map_to_their_system(self):
        self.assertEqual(tp({"etcs": "1;2"})[0], "etcs_2")
        self.assertEqual(tp({"ctcs": "0"})[0], "ctcs")
        self.assertEqual(tp({"kcvb": "yes"})[0], "kvb")
        self.assertEqual(tp({"atb-eg": "yes"})[0], "atb")
        self.assertEqual(tp({"zub123": "yes"})[0], "zub")
        self.assertEqual(tp({"integra_signum:ALS": "yes", "etcs": "no"})[0], "als")
        self.assertEqual(tp({"atc": "DS"})[0], "atc")

    def test_left_out_keys_block_none(self):
        self.assertIsNone(tp({"crocodile": "yes", "etcs": "no"})[0])
        self.assertEqual(tp({"etcs": "no", "lzb": "no", "pzb": "no"})[0], "none")


if __name__ == "__main__":
    unittest.main()
