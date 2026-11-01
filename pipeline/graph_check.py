#!/usr/bin/env python3
"""
Check the path search against the train routes mapped in OSM.

Each route=train relation lists its stops in order. For every pair of
consecutive stops the check finds a path twice: once on the relation's own
ways (the reference) and once over the whole graph, as the app would for a
user who picked those two stations. A leg matches when each path lies within
TOL_M of the other along at least MATCH of its length; the tolerance lets the
search take the other track of a double-track line.

With --whole it also searches each route from its first stop to its last with
no stops in between, against the legs' references joined.

Usage: graph_check.py IN-rail.osm.pbf [--cache GRAPH.pickle] [--whole N]
                      [--worst N] [--geojson OUT]
"""
import argparse
import json
import math
import os
import pickle
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph as G
import search as S
from geometry import K

TOL_M = 30.0
MATCH = 0.95
STEP_M = 10.0


def _xy(pts, lat0):
    c = math.cos(math.radians(lat0))
    return [(lon * K * c, lat * K) for lon, lat in pts]


def coverage(a, b, lat0):
    """Share of polyline a's length within TOL_M of polyline b."""
    a, b = _xy(a, lat0), _xy(b, lat0)
    cell = TOL_M
    grid = defaultdict(list)
    for i in range(len(b) - 1):
        (x1, y1), (x2, y2) = b[i], b[i + 1]
        for cx in range(int(min(x1, x2) // cell) - 1, int(max(x1, x2) // cell) + 2):
            for cy in range(int(min(y1, y2) // cell) - 1, int(max(y1, y2) // cell) + 2):
                grid[cx, cy].append(i)
    near = total = 0.0
    for i in range(len(a) - 1):
        (x1, y1), (x2, y2) = a[i], a[i + 1]
        seg = math.hypot(x2 - x1, y2 - y1)
        n = max(1, int(seg // STEP_M))
        for k in range(n):
            t = (k + 0.5) / n
            px, py = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
            total += seg / n
            for j in grid.get((int(px // cell), int(py // cell)), ()):
                (qx1, qy1), (qx2, qy2) = b[j], b[j + 1]
                dx, dy = qx2 - qx1, qy2 - qy1
                L = dx * dx + dy * dy
                u = 0.0 if L == 0 else max(0.0, min(1.0, ((px - qx1) * dx + (py - qy1) * dy) / L))
                if math.hypot(px - qx1 - u * dx, py - qy1 - u * dy) <= TOL_M:
                    near += seg / n
                    break
    return near / total if total else 1.0


def length(g, path):
    return sum(g.length[d >> 1] for d in path)


def load(pbf, cache):
    if cache and os.path.exists(cache):
        with open(cache, "rb") as f:
            return pickle.load(f)
    t = time.time()
    g = G.build(G.read_opl(pbf))
    print(f"graph: {len(g.node_id)} junctions, {g.edge_count} edges, "
          f"{len(g.relations)} relations in {time.time() - t:.0f} s", flush=True)
    if cache:
        with open(cache, "wb") as f:
            pickle.dump(g, f, protocol=pickle.HIGHEST_PROTOCOL)
    return g


def train_routes(g):
    for r in g.relations:
        if r["tags"].get("route") != "train":
            continue
        stops = []
        for nid, role in r["nodes"]:
            j = g.node_index.get(int(nid))
            if role.startswith("stop") and j is not None and (not stops or stops[-1] != j):
                stops.append(j)
        yield r, stops


def stop_name(g, j):
    t = g.stop_tags.get(g.node_id[j], {})
    return t.get("name") or f"n{g.node_id[j]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("--cache")
    ap.add_argument("--whole", type=int, default=0, help="routes to search end to end")
    ap.add_argument("--worst", type=int, default=25)
    ap.add_argument("--geojson", help="write the worst legs for a map")
    ap.add_argument("--speed-weight", type=float, default=S.SPEED_WEIGHT,
                    help="0 finds the shortest path, 1 the fastest")
    args = ap.parse_args()

    g = load(args.pbf, args.cache)
    train = S.train_factor(g, args.speed_weight)
    tally = Counter()

    def path(sources, target, factor, **kw):
        found = S.find(g, sources, target, factor, **kw)
        return None if found is None else found[1]

    def row(r, s, t, ref, got):
        if got is None:
            return 0.0, 0.0, r, s, t, ref, None
        a, b = g.path_coords(got), g.path_coords(ref)
        return coverage(a, b, g.lat[s]), coverage(b, a, g.lat[s]), r, s, t, ref, got

    legs, whole = [], []
    t0 = time.time()
    for r, stops in train_routes(g):
        tally["routes"] += 1
        if len(stops) < 2:
            tally["routes with < 2 stops on track"] += 1
            continue
        members = {int(w[1:]) for w in r["ways"]}
        on_members = S.only_ways(g, members)
        refs = []
        for s, t in zip(stops, stops[1:]):
            tally["legs"] += 1
            ref = path([s], t, on_members)
            if ref is None:
                loose = path([s], t, on_members, max_turn=180.0)
                tally["no reference: " + ("relation has a gap" if loose is None else "needs a sharp turn")] += 1
                refs.append(None)
                continue
            refs.append(ref)
            got = path([s], t, train)
            if got is None:
                tally["no path found"] += 1
            legs.append(row(r, s, t, ref, got))
        if len(whole) < args.whole and all(refs):
            ref = [d for leg in refs for d in leg]
            whole.append(row(r, stops[0], stops[-1], ref, path([stops[0]], stops[-1], train)))
    print(f"searched in {time.time() - t0:.0f} s")
    for k, v in sorted(tally.items()):
        print(f"  {k}: {v}")

    def report(title, rows):
        if not rows:
            return
        ok = sum(1 for p, q, *_ in rows if p >= MATCH and q >= MATCH)
        km_ok = sum(length(g, ref) for p, q, _, _, _, ref, _ in rows if p >= MATCH and q >= MATCH) / 1000
        km = sum(length(g, ref) for *_, ref, _ in rows) / 1000
        print(f"\n{title}: {ok}/{len(rows)} match ({100 * ok / len(rows):.1f}%), "
              f"{km_ok:.0f} of {km:.0f} km ({100 * km_ok / km:.1f}%)")
        print("  worst (found on reference, reference on found, reference km, found km):")
        rows = sorted(rows, key=lambda x: (min(x[0], x[1]), -length(g, x[5])))
        for p, q, r, s, t, ref, got in rows[:args.worst]:
            name = r["tags"].get("name") or r["tags"].get("ref") or ""
            km_got = f"{length(g, got) / 1000:.1f}" if got else "-"
            print(f"  {p:.2f} {q:.2f} {length(g, ref) / 1000:6.1f} {km_got:>6}  r{r['rid']} {name[:50]}: "
                  f"{stop_name(g, s)} -> {stop_name(g, t)}")
        return rows

    worst = report("legs between consecutive stops", legs)
    report("whole routes, first stop to last", whole)

    if args.geojson and worst:
        feats = []
        for p, q, r, s, t, ref, got in worst[:args.worst]:
            label = f"r{r['rid']} {stop_name(g, s)} -> {stop_name(g, t)} ({p:.2f}/{q:.2f})"
            for role, path in (("reference", ref), ("found", got)):
                if path:
                    feats.append({"type": "Feature", "properties": {"leg": label, "role": role},
                                  "geometry": {"type": "LineString", "coordinates": g.path_coords(path)}})
        with open(args.geojson, "w") as f:
            json.dump({"type": "FeatureCollection", "features": feats}, f)


if __name__ == "__main__":
    main()
