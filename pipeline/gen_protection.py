#!/usr/bin/env python3
"""
Generate train_protection.json: which OSM tags name a train protection system,
and which system a way shows when it is tagged with several.

Usage: gen_protection.py pipeline/train_protection.json

Writes the table to the given path through a temporary file, so a failed run
leaves the previous table in place. The previous table also steadies the order
(see Order). A run that changes no rank, tag or key leaves the file as it is:
the region cache key hashes it, so a rewrite would rebuild every region.

Rules. Each SYSTEMS entry maps OSM tags to a system id, the id the app colours.
A tag's values are the ones taginfo shows on 10 or more ways, without "no",
lifecycle words (proposed, construction, disused) and typos. An entry is kept
when it is tagged on MIN_WAYS or more ways, counting construction: and
proposed: lines; the run names any entry it drops. LEFT_OUT lists the system
keys known to us that have no entry, with the reason. The table's "keys" holds
every key of SYSTEMS and LEFT_OUT, dropped entries included, so a way that
carries one of them with a value other than NO_VALUES never reads as "none". A
way matches an entry when any of the entry's tags has one of its values. One
id can have several entries when its tag is used for different things in
different regions; each entry is counted and ranked on its own.

Order. A way tagged with several systems shows one, and the app draws higher
ranks on top. Two steps decide the order:
  1. Tier. OVERLAY entries are systems built to run on top of a line's existing
     signalling while it migrates: ETCS, CTCS-2/3, KTCS, the North American PTC
     systems, and CBTC, which resignalling runs over the old signalling until
     that is removed. They come first. METHOD entries are operating methods,
     not equipment on the track, so they come last. NATIONAL is the rest.
  2. Within a tier, the entry tagged on fewer ways comes first. Where two
     systems share track, the rarer one is the addition: LZB on PZB lines, TVM
     on KVB lines, ACSES on PTC lines.
An entry's "order_ways" is the count the order uses. A rerun keeps the previous
one unless the entry's tagging moved by more than STEADY, so systems whose
counts are close (AWS and TPWS are within 2%) do not swap places on every run.

Rank. The first entry gets the highest rank and the last gets 2. Rank 1 is
"none", a way whose protection keys all say "no"; the app gives untagged
track 0, so "none" draws above it and below every system.
"""
import functools
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

TAGINFO = "https://taginfo.openstreetmap.org/api/4"
MIN_WAYS = 10
STEADY = 0.25
NONE_RANK = 1
NO_VALUES = ["no", "none"]

OVERLAY, NATIONAL, METHOD = 1, 2, 3

# (id, tier, {key: [values]}). Keys omit the "railway:" prefix.
SYSTEMS = [
    # ETCS: a way listing levels 1 and 2 runs level 2 where trains have it.
    # Level 3 is radio-based like level 2, and the app has no level 3 colour.
    ("etcs_2", OVERLAY, {"etcs": ["2", "1;2", "3"]}),
    ("etcs_1", OVERLAY, {"etcs": ["1"]}),
    ("etcs", OVERLAY, {"etcs": ["yes"]}),
    ("ctcs_3", OVERLAY, {"ctcs": ["3"]}),
    ("ctcs_2", OVERLAY, {"ctcs": ["2"]}),
    ("ktcs", OVERLAY, {"ktcs": ["1", "2", "M"]}),
    ("ptc", OVERLAY, {"ptc": ["yes"]}),
    ("acses", OVERLAY, {"acses": ["yes"]}),
    ("etms", OVERLAY, {"etms": ["yes"]}),
    ("itcs", OVERLAY, {"itcs": ["yes"]}),
    ("eatc", OVERLAY, {"eatc": ["yes"]}),
    # Values name the grade of automation; Urbalis is a CBTC product. SACEM,
    # SAET, OURAGAN, OCTYS, NExTEO and ATACS are CBTC-type systems too.
    ("cbtc", OVERLAY, {"cbtc": ["yes", "sto", "RF", "uto", "dto", "IL"], "urbalis": ["200"]}),
    ("sacem", OVERLAY, {"sacem": ["yes"]}),
    ("saet", OVERLAY, {"saet": ["yes"]}),
    ("ouragan", OVERLAY, {"ouragan": ["yes"]}),
    ("octys", OVERLAY, {"octys": ["1", "2", "3", "yes"]}),
    ("nexteo", OVERLAY, {"nexteo": ["yes"]}),
    ("atacs", OVERLAY, {"atacs": ["yes"]}),
    # CTCS level 0 is LKJ, China's base system, which the app names "ctcs".
    ("ctcs", NATIONAL, {"ctcs": ["0"]}),
    ("pzb", NATIONAL, {"pzb": ["yes"]}),
    ("lzb", NATIONAL, {"lzb": ["yes"]}),
    # KVBP, KCVB and KCVP are variants of KVB; the app has one KVB colour.
    ("kvb", NATIONAL, {"kvb": ["yes"], "kvbp": ["yes"], "kcvb": ["yes"], "kcvp": ["yes"]}),
    ("tvm", NATIONAL, {"tvm": ["300", "430"]}),
    ("aws", NATIONAL, {"aws": ["yes"]}),
    ("tpws", NATIONAL, {"tpws": ["yes"]}),
    ("caws", NATIONAL, {"caws": ["yes"]}),
    ("atb", NATIONAL, {"atb": ["yes"], "atb-eg": ["yes"], "atb-ng": ["yes"]}),
    ("tbl", NATIONAL, {"tbl": ["1", "1+", "2"]}),
    ("asfa", NATIONAL, {"asfa": ["yes"], "asfa_200": ["yes"]}),
    ("scmt", NATIONAL, {"scmt": ["yes"]}),
    ("ssc", NATIONAL, {"ssc": ["yes"]}),
    ("shp", NATIONAL, {"shp": ["yes"]}),
    ("ls", NATIONAL, {"ls": ["yes"]}),
    ("evm", NATIONAL, {"evm": ["yes"]}),
    ("jkv", NATIONAL, {"jkv": ["yes"]}),
    ("ebicab", NATIONAL, {"ebicab": ["700"]}),
    # railway:atc=yes is Scandinavian ATC-2 and North American cab-signal ATC;
    # Japanese lines name their ATC variant. Counted apart, the Scandinavian
    # track does not outweigh Japanese ATC where it meets ATS.
    ("atc", NATIONAL, {"atc": ["yes"]}),
    ("atc", NATIONAL, {"atc": ["NS", "DS", "KS", "P", "TD", "D", "CS", "new-CS", "ATC-9"]}),
    ("atp", NATIONAL, {"atp": ["yes"]}),
    ("zsi127", NATIONAL, {"zsi127": ["yes"]}),
    ("zub", NATIONAL, {"zub": ["yes"], "zub122": ["yes"], "zub123": ["yes"], "zub222c": ["yes"]}),
    ("zbs", NATIONAL, {"zbs": ["yes"]}),
    ("zsl90", NATIONAL, {"zsl90": ["yes"]}),
    ("zst90", NATIONAL, {"zst90": ["yes"]}),
    # ALS-N and ALS-ARS are forms of ALS cab signalling. Russian Railways lines
    # around Moscow tag ALS as railway:integra_signum:ALS (ALSO, ALSN); the
    # Swiss Integra-Signum system is not used there.
    ("als", NATIONAL, {"als": ["yes"], "alsn": ["yes"], "als-ars": ["yes"], "alsn-en": ["yes"],
                       "integra_signum:ALS": ["yes"], "integra_signum:als": ["yes"],
                       "integra_signum:ALSO": ["yes"], "integra_signum:ALSN": ["yes"]}),
    ("ats", NATIONAL, {"ats": ["yes", "S1;S2", "S1", "S2"]}),
    ("tmacs", NATIONAL, {"tmacs": ["yes"]}),
    ("atms", NATIONAL, {"atms": ["yes"]}),
    ("twc", METHOD, {"twc": ["yes"]}),
    ("tcb", METHOD, {"tcb": ["yes"]}),
]

LEFT_OUT = {
    "gnt": "supervises tilting trains only, on lines that also carry PZB",
    "dtc": "an operating method like track warrant control; the app has no id yet",
    "zsi90": "the app has no id yet",
    "memor": "the app has no id yet",
    "crocodile": "the app has no id yet",
    "pa-135": "the app has no id yet",
    "avr": "the app has no id yet",
    "atp3b": "on Russian and Ukrainian lines that also carry ALS-N; which system it names is unconfirmed",
    "ari": "almost every way says no",
    "train_protection": "one key whose values name systems by country (FI:ATP-VR/RHK); not parsed",
}


def get(path, **params):
    url = f"{TAGINFO}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "rail-tiles gen_protection.py"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def railway_keys():
    """Every key containing "railway:" that is on at least one way, and the
    date of taginfo's data. Keys come by way count, most first, so paging
    stops at the first page that reaches keys on no ways."""
    found, page = set(), 1
    while True:
        d = get("keys/all", query="railway:", sortname="count_ways", sortorder="desc", rp=999, page=page)
        found |= {k["key"] for k in d["data"] if k["count_ways"]}
        if not d["data"] or not d["data"][-1]["count_ways"] or page * 999 >= d["total"]:
            return found, d.get("data_until")
        page += 1


@functools.cache
def value_counts(key):
    """{value: ways} for one key; several entries share a key."""
    d = get("key/values", key=key, filter="ways", sortname="count_ways", sortorder="desc", rp=100, page=1)
    return {v["value"]: v["count"] for v in d["data"]}


def tagged_ways(tags, known):
    """Ways carrying one of the entry's tag values, on lines in use, being
    built or planned."""
    return sum(value_counts(full).get(v, 0)
               for t in tags
               for full in (t["tag"], "construction:" + t["tag"], "proposed:" + t["tag"]) if full in known
               for v in t["values"])


def entry_key(entry):
    return json.dumps([entry["id"], entry["tags"]], sort_keys=True)


def steady(ways, prev):
    """The count to order by: the previous one unless the tagging moved more
    than STEADY away from it."""
    return prev if prev and abs(ways - prev) <= STEADY * prev else ways


def matcher_view(table):
    """The part of a table protection.py reads."""
    return ({k: table[k] for k in ("none_rank", "no_values", "keys")},
            [{k: s[k] for k in ("id", "rank", "tags")} for s in table["systems"]])


def main():
    out_path = sys.argv[1]
    try:
        with open(out_path) as f:
            before = json.load(f)
    except OSError:
        before = None
    held = {entry_key(s): s["order_ways"] for s in before["systems"]} if before else {}
    known, data_until = railway_keys()

    kept = []
    for sid, tier, tags in SYSTEMS:
        entry = {"id": sid, "tags": [{"tag": f"railway:{k}", "values": v} for k, v in tags.items()]}
        ways = tagged_ways(entry["tags"], known)
        if ways < MIN_WAYS:
            print(f"dropped {sid} {sorted(tags)}: tagged on {ways} ways, under {MIN_WAYS}", file=sys.stderr)
            continue
        entry["order_ways"] = steady(ways, held.get(entry_key(entry)))
        kept.append((tier, entry))

    kept.sort(key=lambda te: (te[0], te[1]["order_ways"], te[1]["id"]))
    systems = [entry for _, entry in kept]
    for i, entry in enumerate(systems):
        entry["rank"] = NONE_RANK + len(systems) - i

    table = {
        "none_rank": NONE_RANK,
        "no_values": NO_VALUES,
        "keys": sorted({f"railway:{k}" for _, _, tags in SYSTEMS for k in tags} | {f"railway:{k}" for k in LEFT_OUT}),
        "systems": systems,
    }
    print(f"taginfo data until {data_until}", file=sys.stderr)
    if before and matcher_view(before) == matcher_view(table):
        print("no rank, tag or key changed; table left as is", file=sys.stderr)
        return
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(out_path)), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(table, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, out_path)


if __name__ == "__main__":
    main()
