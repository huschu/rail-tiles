"""
A way's train protection: the system it shows and that system's rank, from
train_protection.json. gen_protection.py writes that file and explains which
tags count and why one system outranks another.
"""
import json
import os

NO_MATCH = (None, None)


def load():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "train_protection.json")) as f:
        return json.load(f)


def index(table):
    """(best, keys, no_values, none) for a table in train_protection.json's
    shape: best maps (tag, value) to the (id, rank) of the highest entry naming
    it, keys holds every protection key, left-out ones included, and none is
    the result for a way whose keys all say no. Results are shared tuples, so
    the millions of segments a build keeps hold references, not copies."""
    best = {}
    for s in table["systems"]:
        hit = (s["id"], s["rank"])
        for t in s["tags"]:
            for v in t["values"]:
                key = (t["tag"], v)
                if key not in best or hit[1] > best[key][1]:
                    best[key] = hit
    return best, frozenset(table["keys"]), frozenset(table["no_values"]), ("none", table["none_rank"])


TABLE = load()
_INDEX = index(TABLE)


def train_protection(props, prefix="", idx=_INDEX):
    """(system, rank) of the highest-ranked system the way is tagged with,
    ("none", rank) when every protection key on it says "no", else
    (None, None). prefix is '', 'construction:' or 'proposed:'; idx is an
    index() result, the shipped table's unless a test passes its own."""
    best, keys, no, none = idx
    found, tagged, all_no = None, False, True
    n = len(prefix)
    for k, v in props.items():
        if n:
            if not k.startswith(prefix):
                continue
            k = k[n:]
        if k not in keys:
            continue
        tagged = True
        all_no = all_no and v in no
        hit = best.get((k, v))
        if hit and (found is None or hit[1] > found[1]):
            found = hit
    if found:
        return found
    if tagged and all_no:
        return none
    return NO_MATCH
