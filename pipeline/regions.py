#!/usr/bin/env python3
"""
The build matrix: one Geofabrik extract per row, worldwide. The list is
generated from Geofabrik's index by gen_regions.py and vendored in regions.json
(reviewable in git, offline-safe in CI). Regenerate when Geofabrik's coverage
changes; see gen_regions.py for the granularity rules.

Each row is {name, path}: a filesystem-safe slug and the extract's location
relative to a mirror root. Regions are independent, so the matrix is the
parallelism and a failed region does not poison the run (fail-fast: false).

  regions.py                      print the whole world matrix as JSON
  regions.py --names a,b,c        print only those rows (smoke test)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Primary then fallback. Geofabrik returned 502s and refused connections during
# development; the OSM France mirror carries the same tree.
MIRRORS = [
    "https://download.geofabrik.de",
    "https://download.openstreetmap.fr/extracts",
]


def load():
    with open(os.path.join(HERE, "regions.json")) as f:
        return json.load(f)


def urls(path):
    return [f"{m}/{path}" for m in MIRRORS]


if __name__ == "__main__":
    regions = load()
    if len(sys.argv) >= 3 and sys.argv[1] == "--names":
        want = {s.strip() for s in sys.argv[2].split(",") if s.strip()}
        regions = [r for r in regions if r["name"] in want]
    json.dump(regions, sys.stdout)
