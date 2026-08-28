#!/usr/bin/env python3
"""
Emit manifest.json — the small file the client refetches to decide cache
invalidation (see TILE-PIPELINE.md, "Versioning"). It lives at a stable URL (the
'latest' release tag); the archive it points at is immutable and dated.

  schemaVersion  bump on any attribute-schema change -> client hard-purges
  buildId        fresh data, same schema -> client soft-purges
  osmTimestamp   the source extract's own timestamp, shown as "data as of"

Usage: manifest.py --region europe --pmtiles europe-DATE.pmtiles \
                    --url https://.../europe-DATE.pmtiles \
                    --build-id ISO --osm-timestamp ISO --out manifest.json
"""
import argparse
import hashlib
import json
import os
import sys

# Bump whenever the emitted attribute schema changes: a new field, a changed
# enum, different speed-band edges. Old cached tiles decode wrong rather than
# failing loudly, so a mismatch must purge everything.
SCHEMA_VERSION = 1
MIN_ZOOM = 4
MAX_ZOOM = 16


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--pmtiles", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--build-id", required=True)
    ap.add_argument("--osm-timestamp", required=True)
    ap.add_argument("--out", default="manifest.json")
    args = ap.parse_args()

    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "buildId": args.build_id,
        "osmTimestamp": args.osm_timestamp,
        "minZoom": MIN_ZOOM,
        "maxZoom": MAX_ZOOM,
        "archives": [{
            "region": args.region,
            "url": args.url,
            "bytes": os.path.getsize(args.pmtiles),
            "sha256": sha256(args.pmtiles),
        }],
    }
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(json.dumps(manifest, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
