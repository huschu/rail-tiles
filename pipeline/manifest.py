#!/usr/bin/env python3
"""
Emit manifest.json — the small file the client refetches to decide cache
invalidation (see TILE-PIPELINE.md, "Versioning"). It lives at a stable URL (the
'latest' release tag); the archives it points at are immutable and dated.

The planet ships as per-continent archives (a single file exceeds the 2 GB
Releases limit). Each archive lists its geographic bounds, read straight from the
PMTiles header, so the client loads only the archives overlapping the viewport.

  schemaVersion  bump on any attribute-schema or tile-format change -> client hard-purges
  buildId        fresh data, same schema -> client soft-purges
  osmTimestamp   the source extracts' own timestamp, shown as "data as of"
  nightTrains    {url, bytes} of the gzipped night-train list (night.py),
                 when the build produced one
  trackGraph     {url, bytes, sha256, zoom, bounds} of the track graph's
                 chunk archive (graph_join.py), when the build produced one;
                 not a map archive, so not under archives
  trackStations  {url, bytes} of the stations file the app searches by name
  builds         recent dated tags, oldest->newest, that carry a per-tile change
                 list. A client soft-purge walks the tags after its last-seen one
                 and applies each <region>-<tag>.changed instead of wiping the
                 cache. A client older than builds[0] falls back to a full purge.

The change list for tag T lives next to its archives at
  {baseUrl}/{T}/{region}-{T}.changed   (gzipped JSON)
and the client derives that URL from baseUrl, the builds tags, and each region.

Usage: manifest.py --archives-dir DIR --tag TAG --base-url URL \
                   --build-id ISO --osm-timestamp ISO \
                   [--recent-builds t1,t2,...] [--night-trains FILE] \
                   [--track-graph FILE --track-stations FILE] --out manifest.json
"""
import argparse
import glob
import hashlib
import json
import os
import struct
import sys

SCHEMA_VERSION = 4               # 4: zstd tiles
MIN_ZOOM = 2
MAX_ZOOM = 16


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def header(path):
    with open(path, "rb") as f:
        head = f.read(127)
    if head[:7] != b"PMTiles" or head[7] != 3:
        raise ValueError(f"{path}: not a PMTiles v3 file")
    return head


def bounds(head):
    """[west, south, east, north] from a PMTiles v3 header (int32 e7 at
    offsets 102/106/110/114)."""
    w, s, e, n = struct.unpack_from("<iiii", head, 102)
    return [w / 1e7, s / 1e7, e / 1e7, n / 1e7]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archives-dir", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--base-url", required=True)   # .../releases/download
    ap.add_argument("--build-id", required=True)
    ap.add_argument("--osm-timestamp", required=True)
    ap.add_argument("--recent-builds", default="",
                    help="comma-separated dated tags, oldest->newest, that carry "
                         "a change list (the last 26 builds incl. this one)")
    ap.add_argument("--night-trains")
    ap.add_argument("--track-graph")
    ap.add_argument("--track-stations")
    ap.add_argument("--out", default="manifest.json")
    args = ap.parse_args()

    archives = []
    for path in sorted(glob.glob(os.path.join(args.archives_dir, f"*-{args.tag}.pmtiles"))):
        fn = os.path.basename(path)
        region = fn[:-len(f"-{args.tag}.pmtiles")]
        archives.append({
            "region": region,
            "url": f"{args.base_url}/{args.tag}/{fn}",
            "bytes": os.path.getsize(path),
            "sha256": sha256(path),
            "bounds": bounds(header(path)),
        })
    if not archives:
        sys.exit(f"no archives matching *-{args.tag}.pmtiles in {args.archives_dir}")

    builds = [t for t in args.recent_builds.split(",") if t]

    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "buildId": args.build_id,
        "osmTimestamp": args.osm_timestamp,
        "minZoom": MIN_ZOOM,
        "maxZoom": MAX_ZOOM,
        "tag": args.tag,
        "baseUrl": args.base_url,
        "builds": builds,
        "archives": archives,
    }
    if args.night_trains and os.path.exists(args.night_trains):
        fn = os.path.basename(args.night_trains)
        manifest["nightTrains"] = {"url": f"{args.base_url}/{args.tag}/{fn}",
                                   "bytes": os.path.getsize(args.night_trains)}
    if args.track_graph and os.path.exists(args.track_graph) \
            and args.track_stations and os.path.exists(args.track_stations):
        fn = os.path.basename(args.track_graph)
        head = header(args.track_graph)
        manifest["trackGraph"] = {"url": f"{args.base_url}/{args.tag}/{fn}",
                                  "bytes": os.path.getsize(args.track_graph),
                                  "sha256": sha256(args.track_graph),
                                  "zoom": head[100],
                                  "bounds": bounds(head)}
        fn = os.path.basename(args.track_stations)
        manifest["trackStations"] = {"url": f"{args.base_url}/{args.tag}/{fn}",
                                     "bytes": os.path.getsize(args.track_stations)}
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(json.dumps(manifest, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
