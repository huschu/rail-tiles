#!/usr/bin/env python3
"""
CI gates on a built tileset. These encode failure modes that are invisible on a
map (see TILE-PIPELINE.md, "CI gates"). Gate 1 (feature conservation) runs
inside build_region.py where both counts are known; this file runs the rest.

  gate 2  category presence: every non-service kind with real presence at max
          zoom is present at the finest chained zoom
  gate 3  vertex budget: no screen-sized tile window exceeds 120 k vertices
          among default-filter features
  gate 4  attribute completeness: every feature carries the render/style fields
  gate 5  merge honesty: unit test on chain() — no chain spans >1 speed band
  gate 6  connectivity: parallel collapse must not sever connected lines

tippecanoe-decode prints one Feature per line and one line per tile header, so
we stream its output line by line and keep only per-gate aggregates. Peak memory
is a single line, not the whole tileset — loading it whole OOM'd the runner on
China (409 MB).

Usage: verify.py REGION.pmtiles          run gates 2-6 on a tileset
       verify.py --self-test             run gate 5 (no tileset needed)
"""
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import classify as C
import geometry as G
import zoomparams as Z

VERTEX_BUDGET = 120_000
REQUIRED_FIELDS = ("kind", "lifecycle", "elec", "gauge_class")
GATE2_LOW_ZOOM = Z.CHAIN_MAX_ZOOM   # sub-pixel drops below here are legitimate
GATE2_MIN_COUNT = 10                # a lone stub can be sub-pixel even at z11
GATE6_TEST_ZOOMS = (Z.SERVICE_MIN_ZOOM, Z.COLLAPSE_MAX_ZOOM)  # 12, 14 (raw ways)
GATE6_BASE_ZOOM = Z.MAX_ZOOM        # collapse-off truth to compare against
GATE6_MARGIN = 0.20
GATE6_MIN_FEATURES = 50

# A tile header line: { ... "properties": { "zoom": N, "x": N, "y": N }, ... }.
# The layer-level FeatureCollection nested inside carries "layer" instead.
_TILE_RE = re.compile(r'"zoom":\s*(\d+),\s*"x":\s*(\d+),\s*"y":\s*(\d+)')


def _nverts(geom):
    t, c = geom.get("type"), geom.get("coordinates") or []
    if t == "LineString":
        return len(c)
    if t == "MultiLineString":
        return sum(len(part) for part in c)
    return 0


def _default_pass(p):
    """The app's default view: in-service track, service track hidden."""
    return p.get("lifecycle") == "present" and "service" not in p


def _endpoints(geom):
    t, c = geom.get("type"), geom.get("coordinates") or []
    if t == "LineString" and len(c) >= 2:
        return (tuple(round(v, 6) for v in c[0]), tuple(round(v, 6) for v in c[-1]))
    if t == "MultiLineString" and c and c[0] and c[-1]:
        return (tuple(round(v, 6) for v in c[0][0]), tuple(round(v, 6) for v in c[-1][-1]))
    return None


def scan(pmtiles):
    """Stream tippecanoe-decode line by line, accumulating only what each gate
    needs. Returns (lo_kinds, hi, grid, g6_ends, g6_feats, field_fail)."""
    proc = subprocess.Popen(
        ["tippecanoe-decode", pmtiles],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    lo_kinds = set()                       # gate 2: kinds present at z11
    hi = Counter()                         # gate 2: non-service kinds at z16
    grid = defaultdict(int)                # gate 3: (z,x,y) -> default vertex sum
    g6_zooms = (*GATE6_TEST_ZOOMS, GATE6_BASE_ZOOM)
    g6_ends = {z: Counter() for z in g6_zooms}   # gate 6: endpoint use counts
    g6_feats = {z: [] for z in g6_zooms}         # gate 6: per-feature endpoints
    field_fail = None                      # gate 4: first missing-field message
    z = x = y = None

    for line in proc.stdout:
        m = _TILE_RE.search(line)
        if m and '"layer"' not in line:
            z, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            continue
        if '"type": "Feature"' not in line or z is None:
            continue
        try:
            f = json.loads(line.strip().rstrip(","))
        except json.JSONDecodeError:
            continue
        p = f.get("properties") or {}
        g = f.get("geometry") or {}

        if field_fail is None:
            miss = next((k for k in REQUIRED_FIELDS if k not in p), None)
            if miss:
                field_fail = f"GATE 4 FAIL: feature missing {miss}: {p}"
            elif "osm_id" not in p and "src" not in p:
                field_fail = f"GATE 4 FAIL: feature has no identity (osm_id/src): {p}"

        if z == GATE2_LOW_ZOOM:
            lo_kinds.add(p.get("kind"))
        elif z == Z.MAX_ZOOM and "service" not in p:
            hi[p.get("kind")] += 1

        if _default_pass(p):
            grid[(z, x, y)] += _nverts(g)

        if z in g6_ends and p.get("kind") == "rail" and p.get("usage") in ("main", "branch"):
            e = _endpoints(g)
            if e:
                g6_feats[z].append(e)
                g6_ends[z][e[0]] += 1
                g6_ends[z][e[1]] += 1

    proc.wait()
    return lo_kinds, hi, grid, g6_ends, g6_feats, field_fail


def _worst_window(grid):
    """Worst 3x4 / 4x3 tile-window default-vertex sum over every zoom."""
    by_zoom = defaultdict(dict)
    for (z, x, y), v in grid.items():
        by_zoom[z][(x, y)] = v
    worst, at = 0, None
    for z, cells in by_zoom.items():
        for (x0, y0) in cells:
            for (w, h) in ((3, 4), (4, 3)):
                s = sum(cells.get((x0 + dx, y0 + dy), 0)
                        for dx in range(w) for dy in range(h))
                if s > worst:
                    worst, at = s, (z, x0, y0, w, h)
    return worst, at


def gate5_merge_honesty():
    """Synthetic ways sharing endpoints across speed bands: a chain must never
    straddle more than one band, nor mix known with unknown speed."""
    def way(i, x0, x1, speed):
        p = {"railway": "rail", "usage": "main"}
        if speed is not None:
            p["maxspeed"] = str(speed)
        return {"id": f"w{i}", "props": p, "coords": [(x0, 0.0), (x1, 0.0)]}

    ways = [way(1, 0, 1, 60), way(2, 1, 2, 100), way(3, 2, 3, 140),
            way(4, 3, 4, None), way(5, 4, 5, 90)]
    for ch in G.chain(ways, C.render_key, C.speed_of, C.band_of):
        bands = set()
        unknown = False
        for sid in set(ch["src"]):
            w = next(w for w in ways if w["id"] == sid)
            sp = C.parse_speed(w["props"].get("maxspeed"))
            if sp is None:
                unknown = True
            else:
                bands.add(C.band_of(sp))
        if bands and unknown:
            return f"GATE 5 FAIL: chain mixes known and unknown speed: {sorted(ch['src'])}"
        if bands and (max(bands) - min(bands) > 1):
            return f"GATE 5 FAIL: chain spans {sorted(bands)} (>1 band): {sorted(ch['src'])}"
    return None


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        err = gate5_merge_honesty()
        print(err or "  gate 5 ok: merge honest (no cross-band, no known/unknown mix)")
        sys.exit(1 if err else 0)
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)

    lo_kinds, hi, grid, g6_ends, g6_feats, field_fail = scan(sys.argv[1])
    fails = []

    missing = sorted(k for k, n in hi.items()
                     if k is not None and n >= GATE2_MIN_COUNT and k not in lo_kinds)
    if missing:
        fails.append(f"GATE 2 FAIL: non-service kinds with real presence at z{Z.MAX_ZOOM} "
                     f"but absent by z{GATE2_LOW_ZOOM}: {missing}")
    else:
        print("  gate 2 ok")

    worst, at = _worst_window(grid)
    if worst > VERTEX_BUDGET:
        fails.append(f"GATE 3 FAIL: window {at} holds {worst:,} default-filter "
                     f"vertices > {VERTEX_BUDGET:,}")
    else:
        print(f"  gate 3 ok: worst window {worst:,} vertices at {at} (<= {VERTEX_BUDGET:,})")

    if field_fail:
        fails.append(field_fail)
    else:
        print("  gate 4 ok")

    def iso(z):
        feats, ends = g6_feats[z], g6_ends[z]
        if not feats:
            return None, 0
        n = sum(1 for a, b in feats if ends[a] == 1 and ends[b] == 1)
        return n / len(feats), len(feats)

    base = iso(GATE6_BASE_ZOOM)[0]
    if base is None:
        print("  gate 6 skipped: no main-line rail")
    else:
        ceiling = base + GATE6_MARGIN
        bad = [f"z{z} {s:.0%} (n={n})" for z in GATE6_TEST_ZOOMS
               for s, n in [iso(z)]
               if s is not None and n >= GATE6_MIN_FEATURES and s > ceiling]
        if bad:
            fails.append(f"GATE 6 FAIL: main-line rail isolated share exceeds collapse-off "
                         f"baseline {base:.0%}+{GATE6_MARGIN:.0%}={ceiling:.0%} at {', '.join(bad)}. "
                         f"Parallel collapse is severing connected lines.")
        else:
            print(f"  gate 6 ok: connectivity within {ceiling:.0%} of the z{Z.MAX_ZOOM} baseline ({base:.0%})")

    err = gate5_merge_honesty()
    (fails.append if err else print)(err or "  gate 5 ok")

    if fails:
        print("\n".join(fails), file=sys.stderr)
        sys.exit(1)
    print("all gates passed")


if __name__ == "__main__":
    main()
