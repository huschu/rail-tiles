#!/usr/bin/env python3
"""
Group the per-region tilesets by continent and tile-join each into one archive.

The planet as a single file is ~3.6 GB, over the 2 GB per-asset Releases limit,
so it ships as per-continent archives instead (the largest, Europe, is ~1.2 GB).
The client loads every archive whose bounds overlap the viewport, so the
map is seamless across continent boundaries.

Continent is the first path segment of a region's Geofabrik path (russia has no
segment and is its own archive).

Stations come from each region's `<region>.stations.geojsonseq` sidecar. They are
merged across all regions first, so a station in two overlapping extracts lists
the services of both, then tiled once per continent that holds it and joined
beside that continent's rail layer.

Usage: join_planet.py REGIONS_DIR OUT_DIR TAG
Prints JSON {archives: [...], osm_ts: "..."} for the workflow to consume.
"""
import glob
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_region
import stations


def continent(path):
    return path.split("/")[0] if "/" in path else path.split("-latest")[0]


def merged_station_parts(regions_dir, name2cont):
    """{continent: [stations tileset]} from the regions' sidecars, merged by
    station across every region before being split by continent."""
    lists, conts_of = [], {}
    for sc in glob.glob(os.path.join(regions_dir, "**", "*.stations.geojsonseq"), recursive=True):
        name = os.path.basename(sc)[:-len(".stations.geojsonseq")]
        cont = name2cont.get(name)
        if not cont:
            continue
        with open(sc) as f:
            features = [json.loads(line) for line in f if line.strip()]
        lists.append(features)
        for ft in features:
            conts_of.setdefault(ft["properties"]["osm_id"], set()).add(cont)
    merged = stations.merge(lists)
    tmp = tempfile.mkdtemp(prefix="stations-")
    parts = {}
    for cont in sorted({c for cs in conts_of.values() for c in cs}):
        points = [ft for ft in merged if cont in conts_of[ft["properties"]["osm_id"]]]
        parts[cont] = [build_region.tile_stations(points, tmp, cont)]
        print(f"{cont}: {len(points):,} stations", file=sys.stderr)
    return parts


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

    station_parts = merged_station_parts(regions_dir, name2cont)

    os.makedirs(out_dir, exist_ok=True)
    archives = []
    for cont, parts in sorted(groups.items()):
        parts = parts + station_parts.get(cont, [])
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
