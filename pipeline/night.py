#!/usr/bin/env python3
"""
Night trains: the route=train relations the app shows as night trains.

A relation is a night train when it carries sleepers (sleeping_car=yes or
couchette=yes), at any length, or when its service includes night
(service=night, night;motorail) and the route is at least MIN_KM long. The
length check keeps out local trains that only run at night, such as NS
Nachtnet, which map with service=night too.

The length must be measured over the whole relation, and night trains cross
borders, so no region can decide alone. Each region build writes a sidecar of
its candidates with the length of every member way it holds; the join reads
all sidecars and decides. Geofabrik extracts overlap at borders and a way can
sit in two of them, so lengths are united by way id, never summed per region.

The tiles do not change: the app matches the published member ways against
the src and absorbed ways the tiles already carry.

  night.py join REGIONS_DIR OUT.json.gz     decide and write the asset
"""
import glob
import gzip
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes

MIN_KM = 300


def _values(tags, key):
    return {v.strip() for v in (tags.get(key) or "").split(";")}


def sleeper(tags):
    return tags.get("sleeping_car") == "yes" or tags.get("couchette") == "yes"


def night_service(tags):
    return "night" in _values(tags, "service")


def candidate(tags):
    return tags.get("route") == "train" and (sleeper(tags) or night_service(tags))


def way_length_m(coords):
    m = 0.0
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        p0, p1 = math.radians(y0), math.radians(y1)
        a = (math.sin((p1 - p0) / 2) ** 2
             + math.cos(p0) * math.cos(p1) * math.sin(math.radians(x1 - x0) / 2) ** 2)
        m += 2 * 6371008.8 * math.asin(math.sqrt(a))
    return m


def sidecar(opl_lines, way_coords):
    """This region's candidates: each relation's tags, full member list and
    stops, plus the length in metres of every member way the region holds."""
    relations, lengths = [], {}
    for line in opl_lines:
        r = routes.parse_relation(line)
        if r is None or not candidate(r["tags"]):
            continue
        relations.append({
            "id": int(r["rid"]), "version": r["version"], "tags": r["tags"],
            "ways": [int(w[1:]) for w in r["ways"]],
            "stops": [int(n) for n, role in r["nodes"] if role.startswith("stop")],
        })
        for w in r["ways"]:
            c = way_coords.get(w)
            if c is not None and w not in lengths:
                lengths[w] = round(way_length_m(c))
    return {"relations": relations, "lengths": {w[1:]: m for w, m in lengths.items()}}


def decide(sidecars):
    """The night trains across all regions, by relation id. Each relation
    comes from the region with its newest version."""
    rels, lengths = {}, {}
    for sc in sidecars:
        for r in sc["relations"]:
            have = rels.get(r["id"])
            if have is None or r["version"] > have["version"]:
                rels[r["id"]] = r
        for w, m in sc["lengths"].items():
            lengths[w] = max(m, lengths.get(w, 0))
    out = []
    for rid in sorted(rels):
        r = rels[rid]
        km = sum(lengths.get(str(w), 0) for w in set(r["ways"])) / 1000
        if sleeper(r["tags"]) or km >= MIN_KM:
            out.append({"id": rid, "tags": r["tags"], "ways": r["ways"],
                        "stops": r["stops"], "km": round(km)})
    return out


def join(regions_dir, out):
    sidecars, missing = [], []
    for pmt in sorted(glob.glob(os.path.join(regions_dir, "**", "*.pmtiles"), recursive=True)):
        sc = pmt[:-len(".pmtiles")] + ".night.json"
        if os.path.exists(sc):
            with open(sc) as f:
                sidecars.append(json.load(f))
        else:
            missing.append(os.path.basename(pmt)[:-len(".pmtiles")])
    if missing:
        # A tileset restored from before sidecars existed: its stretch of a
        # cross-border route counts as zero until the region rebuilds.
        print(f"::warning::no night sidecar for {len(missing)} regions: "
              + ", ".join(missing), file=sys.stderr)
    trains = decide(sidecars)
    with gzip.open(out, "wt") as f:
        json.dump({"minKm": MIN_KM, "trains": trains}, f, separators=(",", ":"))
    print(f"night trains: {len(trains)} from {len(sidecars)} regions -> "
          f"{os.path.getsize(out) / 1024:.0f} KB", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "join":
        sys.exit("usage: night.py join REGIONS_DIR OUT.json.gz")
    join(sys.argv[2], sys.argv[3])
