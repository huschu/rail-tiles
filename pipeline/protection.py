"""
Train protection classification, ported from OpenRailwayMap-vector's
train_protection.yaml (vendored here as train_protection.json). A way can carry
several systems (railway:etcs, railway:pzb, ...); we emit the most important one
plus a rank for draw order.

Matching mirrors ORM: a rule matches when all its tags match; a matched rule can
exclude other systems (e.g. ktcs excludes etcs); a system already excluded by an
earlier rule is skipped. "none" means every protection tag present is set to
"no". The `prefix` handles construction:/proposed: lines.
"""
import json
import os

_DATA = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "train_protection.json")))
_ORDER = _DATA["order"]                       # most important first
_FEATURES = _DATA["features"]
_ORDER_INDEX = {s: i for i, s in enumerate(_ORDER)}
_RANK = {s: len(_ORDER) - i for i, s in enumerate(_ORDER)}  # higher = more important
_ALL_TAGS = {t["tag"] for f in _FEATURES for t in f["tags"]}


def train_protection(props, prefix=""):
    """Return (system, rank) for a way's properties, or (None, None) if unknown.
    prefix is '', 'construction:' or 'proposed:'."""
    excluded = set()
    matched = []
    for f in _FEATURES:
        sys = f["system"]
        if sys in excluded:
            continue
        if all(props.get(prefix + t["tag"]) in t["values"] for t in f["tags"]):
            matched.append(sys)
            excluded.update(f["exclude"])
    if matched:
        primary = min(matched, key=lambda s: _ORDER_INDEX.get(s, len(_ORDER)))
        return primary, _RANK.get(primary)
    present = [props.get(prefix + t) for t in _ALL_TAGS]
    present = [v for v in present if v is not None]
    if present and all(v == "no" for v in present):
        return "none", _RANK.get("none")
    return None, None
