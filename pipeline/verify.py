#!/usr/bin/env python3
"""
CI gates on a built tileset. These encode failure modes that are invisible on a
map (see TILE-PIPELINE.md, "CI gates"). Gate 1 (feature conservation) runs
inside build_region.py where both counts are known; this file runs the rest.

  gate 2  category presence at low zoom: every kind visible at max zoom is
          visible at min zoom (the property the adaptive filter depends on)
  gate 3  vertex budget: no screen-sized tile window exceeds 120 k vertices
          among default-filter features
  gate 4  attribute completeness: every feature carries the render/style fields
  gate 5  merge honesty: unit test on chain() — no chain spans >1 speed band,
          none mixes known with unknown speed

Usage: verify.py REGION.pmtiles          run gates 2-4 on a tileset
       verify.py --self-test             run gate 5 (no tileset needed)
"""
import json
import subprocess
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import classify as C
import geometry as G
import zoomparams as Z

VERTEX_BUDGET = 120_000
REQUIRED_FIELDS = ("kind", "lifecycle", "elec", "gauge_class")


def decode(pmtiles):
    r = subprocess.run(["tippecanoe-decode", pmtiles],
                       check=True, stdout=subprocess.PIPE, text=True)
    return json.loads(r.stdout)


def _features(o):
    if isinstance(o, dict):
        if o.get("type") == "Feature" and "geometry" in o:
            yield o
        else:
            for v in o.values():
                yield from _features(v)
    elif isinstance(o, list):
        for v in o:
            yield from _features(v)


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


# Compare at the finest chained zoom, not MIN_ZOOM: tippecanoe legitimately
# drops sub-pixel geometry at the lowest zooms (a 30 m stub cannot be drawn at
# z4). And require a kind at z11 only when it has real presence: a lone stub can
# be sub-pixel even at z11 (India's single light_rail way), which is not the
# category-drop this gate guards. A genuine network is well above the threshold.
GATE2_LOW_ZOOM = Z.CHAIN_MAX_ZOOM
GATE2_MIN_COUNT = 10


def gate2_category_presence(tiles):
    lo = {f["properties"].get("kind") for t in tiles if t["properties"]["zoom"] == GATE2_LOW_ZOOM
          for f in _features(t)}
    # Non-service kinds only (service is z12+ by design), counted at max zoom.
    from collections import Counter
    hi = Counter(f["properties"].get("kind")
                 for t in tiles if t["properties"]["zoom"] == Z.MAX_ZOOM
                 for f in _features(t) if "service" not in f["properties"])
    missing = sorted(k for k, n in hi.items()
                     if k is not None and n >= GATE2_MIN_COUNT and k not in lo)
    if missing:
        return (f"GATE 2 FAIL: non-service kinds with real presence at z{Z.MAX_ZOOM} "
                f"but absent by z{GATE2_LOW_ZOOM}: {missing}")
    return None


def gate3_vertex_budget(tiles):
    per_zoom = {}  # z -> {(x,y): verts}
    for t in tiles:
        z = t["properties"]["zoom"]
        x, y = t["properties"]["x"], t["properties"]["y"]
        v = sum(_nverts(f["geometry"]) for f in _features(t)
                if _default_pass(f["properties"]))
        if v:
            per_zoom.setdefault(z, {})[(x, y)] = v
    worst = 0
    worst_at = None
    for z, grid in per_zoom.items():
        for (x0, y0) in grid:
            for (w, h) in ((3, 4), (4, 3)):  # both screen orientations
                s = sum(grid.get((x0 + dx, y0 + dy), 0)
                        for dx in range(w) for dy in range(h))
                if s > worst:
                    worst, worst_at = s, (z, x0, y0, w, h)
    if worst > VERTEX_BUDGET:
        return (f"GATE 3 FAIL: window {worst_at} holds {worst:,} default-filter "
                f"vertices > {VERTEX_BUDGET:,}")
    return f"  gate 3 ok: worst window {worst:,} vertices at {worst_at} (<= {VERTEX_BUDGET:,})"


def _endpoints(geom):
    t, c = geom.get("type"), geom.get("coordinates") or []
    if t == "LineString" and len(c) >= 2:
        return (tuple(round(v, 6) for v in c[0]), tuple(round(v, 6) for v in c[-1]))
    if t == "MultiLineString" and c and len(c[0]) >= 1 and len(c[-1]) >= 1:
        return (tuple(round(v, 6) for v in c[0][0]), tuple(round(v, 6) for v in c[-1][-1]))
    return None


def _isolated_share(tiles, zoom):
    """Among main-line rail at a zoom, the fraction of features whose both
    endpoints are shared with no other feature. Connected OSM ways share a node
    exactly, so severing a line spikes this."""
    from collections import Counter
    ends = Counter()
    feats = []
    for t in tiles:
        if t["properties"]["zoom"] != zoom:
            continue
        for f in _features(t):
            p = f["properties"]
            if p.get("kind") != "rail" or p.get("usage") not in ("main", "branch"):
                continue
            e = _endpoints(f["geometry"])
            if e:
                feats.append(e)
                ends[e[0]] += 1
                ends[e[1]] += 1
    if not feats:
        return None, 0
    iso = sum(1 for a, b in feats if ends[a] == 1 and ends[b] == 1)
    return iso / len(feats), len(feats)


# Parallel collapse must not sever connected lines. Collapse-on zooms are
# compared to the collapse-off baseline (max zoom, raw ways); severing pushes the
# isolated share far above it (pre-fix Leipzig hit 94-100% at z12-z14).
# Only raw-way zooms. At chained zooms (z4-z11) chaining absorbs shared junction
# nodes into chain interiors, so a connected sequence becomes one chain with
# unshared endpoints — legitimately "isolated" by this metric, and it spikes in
# sparse networks (Guinea, Arizona) with no severing. Severing shows on the raw
# ways (z12-z14), where connected ways still share nodes unless a line is cut.
GATE6_ZOOMS = (Z.SERVICE_MIN_ZOOM, Z.COLLAPSE_MAX_ZOOM)  # 12, 14
GATE6_MARGIN = 0.20
GATE6_MIN_FEATURES = 50


def gate6_connectivity(tiles):
    base, base_n = _isolated_share(tiles, Z.MAX_ZOOM)
    if base is None:
        return "  gate 6 skipped: no main-line rail"
    ceiling = base + GATE6_MARGIN
    worst = []
    for z in GATE6_ZOOMS:
        share, n = _isolated_share(tiles, z)
        if share is None or n < GATE6_MIN_FEATURES:
            continue
        if share > ceiling:
            worst.append(f"z{z} {share:.0%} (n={n})")
    if worst:
        return (f"GATE 6 FAIL: main-line rail isolated share exceeds collapse-off "
                f"baseline {base:.0%}+{GATE6_MARGIN:.0%}={ceiling:.0%} at {', '.join(worst)}. "
                f"Parallel collapse is severing connected lines.")
    return f"  gate 6 ok: connectivity within {ceiling:.0%} of the z{Z.MAX_ZOOM} baseline ({base:.0%})"


def gate4_attribute_completeness(tiles):
    for t in tiles:
        for f in _features(t):
            p = f["properties"]
            for k in REQUIRED_FIELDS:
                if k not in p:
                    return f"GATE 4 FAIL: feature missing {k}: {p}"
            if "osm_id" not in p and "src" not in p:
                return f"GATE 4 FAIL: feature has no identity (osm_id/src): {p}"
    return None


def gate5_merge_honesty():
    """Synthetic ways that share endpoints across speed bands. The chain must
    never straddle more than one band, and never mix known with unknown."""
    def way(i, x0, x1, speed):
        p = {"railway": "rail", "usage": "main"}
        if speed is not None:
            p["maxspeed"] = str(speed)
        return {"id": f"w{i}", "props": p, "coords": [(x0, 0.0), (x1, 0.0)]}

    # 60(band0) - 100(band1) - 140(band2): joining all three would span 2 bands
    ways = [way(1, 0, 1, 60), way(2, 1, 2, 100), way(3, 2, 3, 140),
            way(4, 3, 4, None), way(5, 4, 5, 90)]  # unknown next to known
    chains = G.chain(ways, C.render_key, C.speed_of, C.band_of)
    for ch in chains:
        member_bands = set()
        has_unknown = False
        for sid in set(ch["src"]):
            w = next(w for w in ways if w["id"] == sid)
            sp = C.parse_speed(w["props"].get("maxspeed"))
            if sp is None:
                has_unknown = True
            else:
                member_bands.add(C.band_of(sp))
        if member_bands and has_unknown:
            return f"GATE 5 FAIL: chain mixes known and unknown speed: {sorted(ch['src'])}"
        if member_bands and (max(member_bands) - min(member_bands) > 1):
            return f"GATE 5 FAIL: chain spans {sorted(member_bands)} (>1 band): {sorted(ch['src'])}"
    return None


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        err = gate5_merge_honesty()
        print(err or "  gate 5 ok: merge honest (no cross-band, no known/unknown mix)")
        sys.exit(1 if err else 0)

    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)

    tiles = decode(sys.argv[1])["features"]
    fails = []
    for fn in (gate2_category_presence, gate4_attribute_completeness):
        err = fn(tiles)
        if err:
            fails.append(err)
        else:
            print(f"  {fn.__name__} ok")
    for fn in (gate3_vertex_budget, gate6_connectivity):
        msg = fn(tiles)
        if msg and msg.startswith("GATE"):
            fails.append(msg)
        else:
            print(msg)
    err = gate5_merge_honesty()
    (fails.append if err else print)(err or "  gate 5 ok")

    if fails:
        print("\n".join(fails), file=sys.stderr)
        sys.exit(1)
    print("all gates passed")


if __name__ == "__main__":
    main()
