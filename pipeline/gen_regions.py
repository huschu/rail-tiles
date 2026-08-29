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
# Cross-border convenience extracts that overlap the country/state files and
# would double-cover. The combined US and its 5 regional groupings (use states),
# plus Geofabrik's spanning aggregates whose constituents all exist separately:
#   alps -> AT/CH/DE/FR/IT/SI/LI     dach -> DE/AT/CH
#   britain-and-ireland -> great-britain + ireland-and-northern-ireland
#   sea -> the South-East-Asia countries
#   south-africa-and-lesotho -> south-africa + lesotho
# Offshore territories (azores, canary-islands, ...) are far from their mainland
# extracts and do not overlap, so they are kept.
EXCLUDE = {"us", "us-midwest", "us-northeast", "us-pacific", "us-south", "us-west",
           "alps", "dach", "britain-and-ireland", "sea", "south-africa-and-lesotho"}
# ISO-less regions known to be genuine (not aggregates); anything else without an
# ISO code triggers a warning as a possible new aggregate to review.
KNOWN_ISOLESS = {"great-britain", "azores", "guernsey-jersey", "isle-of-man",
                 "canary-islands", "comores"}
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

    def has_iso(p):
        return bool(p.get("iso3166-1:alpha2") or p.get("iso3166-2"))

    regions = []
    seen = set()
    for c in CONTINENTS:
        for cid in sorted(kids.get(c, [])):
            if cid in EXCLUDE:
                continue
            p = byid[cid]
            pbf = (p.get("urls") or {}).get("pbf")
            if not pbf:
                continue
            if not has_iso(p) and cid not in KNOWN_ISOLESS:
                print(f"WARNING: {cid} has no ISO code and is not a known region; "
                      f"it may be a cross-border aggregate that double-covers. Review.",
                      file=sys.stderr)
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
