#!/usr/bin/env python3
"""
Generate regions.json (the world build matrix) from Geofabrik's index-v1.json.

Granularity: country level everywhere, because a country extract fits a 14 GB
runner (raw + filtered). Two exceptions:
  - the US combined file (~10 GB) is too big, so descend to its 51 state/territory
    extracts; the us-* regional aggregates that overlap the states are excluded.
  - Canada and Russia are kept whole (they fit).
Antarctica is skipped (no railways).

Each entry is {name, path}: name is a filesystem-safe slug, path is the extract's
location relative to a mirror root (Geofabrik and the OSM-France mirror share it).

Usage: gen_regions.py index-v1.json > pipeline/regions.json
"""
import json
import sys

CONTINENTS = ["africa", "asia", "australia-oceania", "central-america",
              "europe", "north-america", "south-america"]
# combined US (too big) and its regional aggregates (overlap the state files)
EXCLUDE = {"us", "us-midwest", "us-northeast", "us-pacific", "us-south", "us-west"}
GEOFABRIK = "https://download.geofabrik.de/"


def main():
    d = json.load(open(sys.argv[1]))
    byid = {f["properties"]["id"]: f["properties"] for f in d["features"]}
    kids = {}
    for f in d["features"]:
        p = f["properties"]
        kids.setdefault(p.get("parent"), []).append(p["id"])

    def rel(pbf):
        return pbf[len(GEOFABRIK):] if pbf.startswith(GEOFABRIK) else pbf

    regions = []
    seen = set()
    for c in CONTINENTS:
        for cid in sorted(kids.get(c, [])):
            if cid in EXCLUDE:
                continue
            pbf = (byid[cid].get("urls") or {}).get("pbf")
            if not pbf:
                continue
            name = cid.replace("/", "-")
            if name in seen:
                continue
            seen.add(name)
            regions.append({"name": name, "path": rel(pbf)})
    # Russia is top level and fits whole.
    rp = (byid["russia"].get("urls") or {}).get("pbf")
    if rp:
        regions.append({"name": "russia", "path": rel(rp)})

    json.dump(regions, sys.stdout, indent=1)
    print(f"\n{len(regions)} regions", file=sys.stderr)


if __name__ == "__main__":
    main()
