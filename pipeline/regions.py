#!/usr/bin/env python3
"""
The build matrix: one Geofabrik sub-region per row. Regions are independent, so
the matrix is the parallelism and a failed region does not poison the run
(fail-fast: false). Extending worldwide is adding rows, no redesign.

Country-level granularity keeps peak disk to one raw extract: the largest here
(france, germany, great-britain, russia ~3-4 GB raw) fit a free runner's ~14 GB
after toolchain cleanup, filtered down to a small -rail.pbf and the raw deleted.

Run with no args to print the matrix as JSON (the workflow reads this).
"""
import json
import sys

# Geofabrik europe/ sub-regions. Slug is the filename stem: the extract is
# <slug>-latest.osm.pbf under both mirrors.
EUROPE = [
    "albania", "andorra", "austria", "azores", "belarus", "belgium",
    "bosnia-herzegovina", "bulgaria", "croatia", "cyprus", "czech-republic",
    "denmark", "estonia", "faroe-islands", "finland", "france", "georgia",
    "germany", "great-britain", "greece", "hungary", "iceland",
    "ireland-and-northern-ireland", "isle-of-man", "italy", "kosovo", "latvia",
    "liechtenstein", "lithuania", "luxembourg", "macedonia", "malta", "moldova",
    "monaco", "montenegro", "netherlands", "norway", "poland", "portugal",
    "romania", "serbia", "slovakia", "slovenia", "spain", "sweden",
    "switzerland", "turkey", "ukraine",
]

# Primary then fallback. Geofabrik returned 502s and refused connections during
# development; the OSM France mirror carries the same tree (rule: fallback is
# not optional).
MIRRORS = [
    "https://download.geofabrik.de/europe",
    "https://download.openstreetmap.fr/extracts/europe",
]


def urls(slug):
    return [f"{m}/{slug}-latest.osm.pbf" for m in MIRRORS]


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "europe"
    regions = {"europe": EUROPE}[which]
    json.dump(regions, sys.stdout)
