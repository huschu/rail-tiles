#!/usr/bin/env python3
"""
Emit manifest.json — the small file the client refetches to decide cache
invalidation (see TILE-PIPELINE.md, "Versioning"). It lives at a stable URL (the
'latest' release tag); the archives it points at are immutable and dated.

The planet ships as per-continent archives (a single file exceeds the 2 GB
Releases limit). Each archive lists its geographic bounds, read straight from the
PMTiles header, so the client loads only the archives overlapping the viewport.

  schemaVersion  bump on any attribute-schema change -> client hard-purges
  buildId        fresh data, same schema -> client soft-purges
  osmTimestamp   the source extracts' own timestamp, shown as "data as of"

Usage: manifest.py --archives-dir DIR --tag TAG --base-url URL \
                   --build-id ISO --osm-timestamp ISO --out manifest.json
"""
import argparse
import glob
import hashlib
import json
import os
import struct
import sys

SCHEMA_VERSION = 1
MIN_ZOOM = 4
MAX_ZOOM = 16


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bounds(path):
    """[west, south, east, north] from the PMTiles v3 header (int32 e7 at
    offsets 102/106/110/114)."""
    with open(path, "rb") as f:
        head = f.read(127)
    if head[:7] != b"PMTiles" or head[7] != 3:
        raise ValueError(f"{path}: not a PMTiles v3 file")
    w, s, e, n = struct.unpack_from("<iiii", head, 102)
    return [w / 1e7, s / 1e7, e / 1e7, n / 1e7]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archives-dir", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--base-url", required=True)   # .../releases/download
    ap.add_argument("--build-id", required=True)
    ap.add_argument("--osm-timestamp", required=True)
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
            "bounds": bounds(path),
        })
    if not archives:
        sys.exit(f"no archives matching *-{args.tag}.pmtiles in {args.archives_dir}")

    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "buildId": args.build_id,
        "osmTimestamp": args.osm_timestamp,
        "minZoom": MIN_ZOOM,
        "maxZoom": MAX_ZOOM,
        "archives": archives,
    }
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(json.dumps(manifest, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
