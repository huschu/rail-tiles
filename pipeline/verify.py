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


def gate2_category_presence(tiles):
    lo = {f["properties"].get("kind") for t in tiles if t["properties"]["zoom"] == Z.MIN_ZOOM
          for f in _features(t)}
    hi = {f["properties"].get("kind") for t in tiles if t["properties"]["zoom"] == Z.MAX_ZOOM
          for f in _features(t)}
    missing = (hi - lo) - {None}
    if missing:
        return f"GATE 2 FAIL: kinds present at z{Z.MAX_ZOOM} but dropped by z{Z.MIN_ZOOM}: {sorted(missing)}"
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
    for fn in (gate3_vertex_budget,):
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
