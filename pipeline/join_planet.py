#!/usr/bin/env python3
"""
Group the per-region tilesets by continent and tile-join each into one archive.

The planet as a single file is 2.59 GB, over the 2 GB per-asset Releases limit,
so it ships as per-continent archives instead (each well under the limit, room to
grow). The client loads every archive whose bounds overlap the viewport, so the
map is seamless across continent boundaries.

Continent is the first path segment of a region's Geofabrik path (russia has no
segment and is its own archive).

Usage: join_planet.py REGIONS_DIR OUT_DIR TAG
Prints JSON {archives: [...], osm_ts: "..."} for the workflow to consume.
"""
import glob
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def continent(path):
    return path.split("/")[0] if "/" in path else path.split("-latest")[0]


def main():
    regions_dir, out_dir, tag = sys.argv[1], sys.argv[2], sys.argv[3]
    regions = json.load(open(os.path.join(HERE, "regions.json")))
    name2cont = {r["name"]: continent(r["path"]) for r in regions}

    groups = {}
    for pmt in glob.glob(os.path.join(regions_dir, "**", "*.pmtiles"), recursive=True):
        name = os.path.basename(pmt)[:-len(".pmtiles")]
        cont = name2cont.get(name)
        if not cont:
            print(f"WARN: no continent mapping for tileset {name}", file=sys.stderr)
            continue
        groups.setdefault(cont, []).append(pmt)

    os.makedirs(out_dir, exist_ok=True)
    archives = []
    for cont, parts in sorted(groups.items()):
        out = os.path.join(out_dir, f"{cont}-{tag}.pmtiles")
        subprocess.run(["tile-join", "-o", out, "--no-tile-size-limit", "--force", *parts],
                       check=True)
        mb = os.path.getsize(out) / 1e6
        print(f"{cont}: joined {len(parts)} regions -> {mb:.0f} MB", file=sys.stderr)
        archives.append(cont)

    ts = []
    for tf in glob.glob(os.path.join(regions_dir, "**", "*.timestamp"), recursive=True):
        v = open(tf).read().strip()
        if v:
            ts.append(v)

    print(json.dumps({"archives": archives, "osm_ts": max(ts) if ts else ""}))


if __name__ == "__main__":
    main()
