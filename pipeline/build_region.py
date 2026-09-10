#!/usr/bin/env python3
"""
Build one region's PMTiles pyramid (steps 2-5 of the pipeline).

  load    osmium export the filtered .pbf to GeoJSON-seq linestrings
  chain   join non-service ways end-to-end for z4-z11 (once; rule 3)
  per z   simplify at the band tolerance, collapse parallel track, emit GeoJSON
  tile    one tippecanoe pass per zoom (-Z z -z z), then tile-join

Usage: build_region.py NAME OUT.pmtiles IN-rail.osm.pbf [--keep-tmp DIR]

Every tippecanoe call passes --no-feature-limit --no-tile-size-limit: nothing
is ever dropped (rule 1). Verified locally that these keep feature counts whole.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import classify as C
import geometry as G
import zoomparams as Z


def osmium_export(pbf, out):
    subprocess.run(
        ["osmium", "export", pbf, "-f", "geojsonseq",
         "--geometry-types=linestring", "--add-unique-id=type_id",
         "-o", out, "--overwrite"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def load(pbf):
    """Return list of ways {id, props, coords} and the mean latitude."""
    with tempfile.NamedTemporaryFile(suffix=".geojsonseq", delete=False) as t:
        tmp = t.name
    osmium_export(pbf, tmp)
    ways = []
    lat_sum = lat_n = 0.0
    with open(tmp) as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            g = o.get("geometry") or {}
            if g.get("type") != "LineString":
                continue
            c = g.get("coordinates") or []
            if len(c) < 2:
                continue
            p = o.get("properties") or {}
            if not C.keep(p):
                continue
            ways.append({"id": o.get("id") or "", "props": p,
                         "coords": [(pt[0], pt[1]) for pt in c]})
            lat_sum += c[0][1]
            lat_n += 1
    os.unlink(tmp)
    return ways, (lat_sum / lat_n if lat_n else 0.0)


def feature(coords, props_rec):
    return {"type": "Feature",
            "properties": props_rec,
            "geometry": {"type": "LineString",
                         "coordinates": [[round(x, 6), round(y, 6)] for x, y in coords]}}


def seg_attrs(ch, cos_lat, id2props):
    """Per-segment attributes of a chain (segment i lies between vertex i and
    i+1). Computed once per chain and reused across zooms; the protection match
    is the expensive part, so it must not run per zoom."""
    coords, src = ch["coords"], ch["src"]
    K = 111320.0
    a = {"speed_raw": [], "band": [], "tunnel": [], "bridge": [],
         "protection": [], "radio": [], "traffic": [], "length": []}
    for i in range(len(coords) - 1):
        p = id2props.get(src[i], {})
        sp = C.speed_of(p)
        a["speed_raw"].append(sp)
        a["band"].append(C.band_of(sp))
        a["tunnel"].append(C.is_tunnel(p))
        a["bridge"].append(C.is_bridge(p))
        a["protection"].append(C.protection_of(p))
        a["radio"].append(C.radio_of(p))
        a["traffic"].append(C.traffic_mode_of(p))
        dx = (coords[i + 1][0] - coords[i][0]) * K * cos_lat
        dy = (coords[i + 1][1] - coords[i][1]) * K
        a["length"].append((dx * dx + dy * dy) ** 0.5)
    return a


def segment_chain(ch, z, mean_lat):
    """Split a chain into sub-chains homogeneous in every along-line attribute
    at this zoom, after LOD-merging runs shorter than a pixel. Each sub-chain
    carries the attribute values it is uniform in."""
    a = ch["seg_attrs"]
    ln = a["length"]
    n_seg = len(ln)
    if n_seg == 0:
        return []
    ml = Z.attr_min_len_m(z, mean_lat)
    band = G.attribute_lod(a["band"], ln, ml)
    tun = G.attribute_lod(a["tunnel"], ln, ml)
    brg = G.attribute_lod(a["bridge"], ln, ml)
    prot = G.attribute_lod(a["protection"], ln, ml)
    rad = G.attribute_lod(a["radio"], ln, ml)
    traf = G.attribute_lod(a["traffic"], ln, ml)
    coords, src, raw = ch["coords"], ch["src"], a["speed_raw"]

    subs = []

    def cut(lo, hi):                                     # segments lo..hi -> vertices lo..hi+1
        c = coords[lo:hi + 2]
        if len(c) < 2:
            return
        w = {}
        for i in range(lo, hi + 1):
            if raw[i] is not None:
                w[raw[i]] = w.get(raw[i], 0.0) + ln[i]
        ms = max(w, key=w.get) if w else None           # dominant raw speed by length
        subs.append({
            "coords": c, "src": src[lo:hi + 2], "props": ch["props"], "maxspeed": ms,
            "tunnel": tun[lo], "bridge": brg[lo], "protection": prot[lo],
            "radio": rad[lo], "traffic_mode": traf[lo],
            "key": (ch["key"], band[lo], tun[lo], brg[lo],
                    prot[lo] and prot[lo][0], rad[lo], traf[lo]),
        })

    lo = 0
    for i in range(1, n_seg):
        if (band[i], tun[i], brg[i], prot[i], rad[i], traf[i]) != \
           (band[i - 1], tun[i - 1], brg[i - 1], prot[i - 1], rad[i - 1], traf[i - 1]):
            cut(lo, i - 1)
            lo = i
    cut(lo, n_seg - 1)
    return subs


def emit_zoom(polys, z, mean_lat, is_chain):
    """One zoom -> GeoJSON features. Chains are split by attribute LOD into
    homogeneous sub-chains first; raw ways (z12+) are already per-way segments."""
    import math
    tol = Z.tol_deg(z)
    cos_ref = math.cos(math.radians(mean_lat))

    if is_chain:
        parts = []
        for ch in polys:
            parts.extend(segment_chain(ch, z, mean_lat))
    else:
        parts = polys

    simp = [G.simplify(p, tol) for p in parts]
    simp = [p for p in simp if len(p["coords"]) >= 2]

    if z <= Z.COLLAPSE_MAX_ZOOM:
        dist = Z.collapse_dist_m(z, mean_lat)
        by_key = {}
        for p in simp:
            by_key.setdefault(p["key"], []).append(p)
        simp = []
        for group in by_key.values():
            simp.extend(G.collapse(group, dist, cos_ref))

    feats = []
    for p in simp:
        if len(p["coords"]) < 2:
            continue
        spans = G.spans_of(p["src"])
        agg = dict(tunnel=p.get("tunnel", False), bridge=p.get("bridge", False),
                   protection=p.get("protection"), radio=p.get("radio"),
                   traffic_mode=p.get("traffic_mode"))
        if is_chain:
            rep = spans[0][0] if spans else None
            rec = C.emit_props(p["props"], p.get("maxspeed"),
                               osm_id=rep, src=spans if len(spans) > 1 else None, **agg)
            if len(spans) == 1:
                rec["osm_id"] = spans[0][0]
        else:
            rec = C.emit_props(p["props"], p.get("maxspeed"),
                               osm_id=p["src"][0] if p["src"] else None, **agg)
        feats.append(feature(p["coords"], rec))
    return feats


def tippecanoe(geojson, out, z):
    """Tile one zoom. Returns tippecanoe's own reported feature count so the
    caller can assert conservation (gate 1) against the input GeoJSON."""
    r = subprocess.run(
        ["tippecanoe", "-o", out, "-Z", str(z), "-z", str(z), "-l", "rail",
         "--no-feature-limit", "--no-tile-size-limit", "-f", geojson],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for tok in r.stderr.split("\n"):
        if " features, " in tok:
            try:
                return int(tok.strip().split(" features,")[0].split()[-1])
            except (ValueError, IndexError):
                pass
    return None


def tile_join(parts, out):
    subprocess.run(
        ["tile-join", "-o", out, "--no-tile-size-limit", "--force", "-q", *parts],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("out")
    ap.add_argument("pbf")
    ap.add_argument("--keep-tmp")
    args = ap.parse_args()

    ways, mean_lat = load(args.pbf)
    nonservice = [w for w in ways if not C.is_service(w["props"])]
    print(f"[{args.name}] {len(ways):,} ways ({len(nonservice):,} non-service), "
          f"mean lat {mean_lat:.1f}", file=sys.stderr)

    # chain once for the low bands; raw ways carry a self-referential src
    chains = G.chain(nonservice, C.render_key, C.speed_of, C.band_of)
    print(f"[{args.name}] chained -> {len(chains):,} polylines", file=sys.stderr)

    # Per-segment attributes for the chained low zooms, computed once and reused
    # across zooms; each zoom LOD-merges short runs and splits into homogeneous
    # sub-chains (segment_chain). On raw ways (z12+) attributes are the way's own.
    import math
    id2props = {w["id"]: w["props"] for w in nonservice}
    cos_lat = math.cos(math.radians(mean_lat))
    for ch in chains:
        ch["seg_attrs"] = seg_attrs(ch, cos_lat, id2props)

    raw = []
    for w in ways:
        raw.append({"coords": w["coords"], "src": [w["id"]] * len(w["coords"]),
                    "props": w["props"], "key": C.render_key(w["props"]),
                    "maxspeed": C.speed_of(w["props"]),
                    "tunnel": C.is_tunnel(w["props"]), "bridge": C.is_bridge(w["props"]),
                    "protection": C.protection_of(w["props"]),
                    "radio": C.radio_of(w["props"]), "traffic_mode": C.traffic_mode_of(w["props"])})

    if not ways:
        # Some regions have no railways at all (Andorra, Malta, Iceland, ...).
        # Produce no tileset; the join step's glob simply skips this region.
        print(f"[{args.name}] no railway ways; no tileset produced", file=sys.stderr)
        return

    tmp = args.keep_tmp or tempfile.mkdtemp(prefix=f"{args.name}-")
    os.makedirs(tmp, exist_ok=True)
    parts = []
    for z in range(Z.MIN_ZOOM, Z.MAX_ZOOM + 1):
        if z <= Z.CHAIN_MAX_ZOOM:
            feats = emit_zoom(chains, z, mean_lat, is_chain=True)
        else:
            feats = emit_zoom(raw, z, mean_lat, is_chain=False)
        if not feats:
            # e.g. a region with only service track has empty z4-z11; tippecanoe
            # errors on empty input, so skip the band rather than emit a tile.
            print(f"[{args.name}] z{z}: 0 features (skipped)", file=sys.stderr)
            continue
        gj = os.path.join(tmp, f"z{z}.geojsonseq")
        with open(gj, "w") as f:
            for ft in feats:
                f.write(json.dumps(ft, separators=(",", ":")) + "\n")
        part = os.path.join(tmp, f"z{z}.pmtiles")
        got = tippecanoe(gj, part, z)
        if got is not None and got != len(feats):
            sys.exit(f"[{args.name}] GATE 1 FAIL z{z}: wrote {len(feats)} "
                     f"features, tippecanoe kept {got}. Silent drop.")
        parts.append(part)
        print(f"[{args.name}] z{z}: {len(feats):,} features (conserved)", file=sys.stderr)

    if not parts:
        print(f"[{args.name}] no tileable features; no tileset produced", file=sys.stderr)
        return
    tile_join(parts, args.out)
    size = os.path.getsize(args.out)
    print(f"[{args.name}] wrote {args.out}: {size/1024/1024:.2f} MB", file=sys.stderr)
    if not args.keep_tmp:
        for p in parts:
            os.unlink(p)


if __name__ == "__main__":
    main()
