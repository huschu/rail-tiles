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

Usage: gen_regions.py index-v1.json [--shapes pipeline/region_shapes.json] > pipeline/regions.json

--shapes writes each matrix region's Geofabrik boundary, which the region
builds use to keep every border way in exactly one region (partition.py).
Regenerate both files together; the tiles cache key hashes both.
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
#   united-kingdom -> great-britain + Northern Ireland, which
#     ireland-and-northern-ireland covers; listed beside great-britain, it
#     built all of Britain twice
# Regions whose whole boundary lies inside a neighbour's, so the neighbour's
# extract already holds everything in them:
#   monaco (inside france), azores (inside portugal),
#   east-timor (inside indonesia)
# Other offshore territories (canary-islands, ...) are far from their mainland
# extracts and do not overlap, so they are kept.
EXCLUDE = {"us", "us-midwest", "us-northeast", "us-pacific", "us-south", "us-west",
           "alps", "dach", "britain-and-ireland", "sea", "south-africa-and-lesotho",
           "united-kingdom", "monaco", "azores", "east-timor"}
# ISO-less regions known to be genuine (not aggregates); anything else without an
# ISO code triggers a warning as a possible new aggregate to review.
KNOWN_ISOLESS = {"great-britain", "guernsey-jersey", "isle-of-man",
                 "canary-islands", "comores"}
GEOFABRIK = "https://download.geofabrik.de/"


def _rings(geom):
    """Every ring, outer and hole, of a GeoJSON Polygon or MultiPolygon."""
    if not geom:
        return []
    if geom["type"] == "Polygon":
        return list(geom["coordinates"])
    return [ring for poly in geom["coordinates"] for ring in poly]


def _inside(x, y, rings):
    """Even-odd rule over all rings, so a hole (an enclave such as Lesotho
    in South Africa) counts as outside."""
    hit = False
    for ring in rings:
        for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
            if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
                hit = not hit
    return hit


def _bbox(rings):
    xs = [x for r in rings for x, _ in r]
    ys = [y for r in rings for _, y in r]
    return min(xs), min(ys), max(xs), max(ys)


def nested(regions, geoms, share=0.9, grid=20):
    """Pairs (inner, outer) where most of inner's area lies inside outer's
    boundary: outer's extract already holds inner, so building both
    duplicates it. Area is sampled on a grid, since a boundary's vertices
    crowd along detailed borders such as rivers."""
    rings = {r: _rings(geoms.get(r)) for r in regions if geoms.get(r)}
    boxes = {r: _bbox(rr) for r, rr in rings.items()}
    out = []
    for a, ra in rings.items():
        x0, y0, x1, y1 = boxes[a]
        pts = [(x0 + (i + 0.5) * (x1 - x0) / grid, y0 + (j + 0.5) * (y1 - y0) / grid)
               for i in range(grid) for j in range(grid)]
        pts = [p for p in pts if _inside(*p, ra)]
        if not pts:
            continue
        for b, rb in rings.items():
            bx0, by0, bx1, by1 = boxes[b]
            if b == a or bx0 > x1 or bx1 < x0 or by0 > y1 or by1 < y0:
                continue
            if sum(_inside(x, y, rb) for x, y in pts) >= share * len(pts):
                out.append((a, b))
    return out


def main():
    d = json.load(open(sys.argv[1]))
    shapes_out = sys.argv[sys.argv.index("--shapes") + 1] if "--shapes" in sys.argv else None
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

    geoms = {f["properties"]["id"].replace("/", "-"): f.get("geometry") for f in d["features"]}
    for inner, outer in nested([r["name"] for r in regions], geoms):
        print(f"WARNING: {inner} lies inside {outer}, so building both duplicates it. "
              f"Add one of them to EXCLUDE.", file=sys.stderr)

    if shapes_out:
        names = {r["name"] for r in regions}
        shapes = {n: g for n, g in geoms.items() if n in names and g}
        with open(shapes_out, "w") as f:
            json.dump(shapes, f, separators=(",", ":"), sort_keys=True)

    json.dump(regions, sys.stdout, indent=1)
    print(f"\n{len(regions)} regions", file=sys.stderr)


if __name__ == "__main__":
    main()
