#!/usr/bin/env python3
"""
Expected search results for the app's port of search.py.

Picks legs between consecutive stops, whole routes searched first stop to
last, and whole routes steered onto their own service, from the route=train
relations of an extract, and writes the cost search.py finds for each. The
app's tests search the same legs on the file graph.py wrote from the same
extract and compare costs; equal-cost paths may differ in their ways.

Usage: graph_fixture.py IN-rail.osm.pbf OUT.json [--cache GRAPH.pickle]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graph_check as GC
import search as S

LEGS = 200
WHOLE = 30
SERVICE = 30


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("out")
    ap.add_argument("--cache")
    args = ap.parse_args()

    g = GC.load(args.pbf, args.cache)
    train = S.train_factor(g)
    routes = [(r, stops) for r, stops in GC.train_routes(g) if len(stops) >= 2]
    legs = [(s, t) for _, stops in routes for s, t in zip(stops, stops[1:])]

    def case(sources, target, factor, service=None):
        path = S.find(g, sources, target, factor)
        out = {"from": g.node_id[sources[0]], "to": g.node_id[target],
               "cost": None, "km": None}
        if path is not None:
            cost = sum(factor(d >> 1) * g.length[d >> 1] for d in path)
            cost += S.REVERSE_M * sum(1 for a, b in zip(path, path[1:]) if b == a ^ 1)
            out["cost"] = round(cost, 1)
            out["km"] = round(sum(g.length[d >> 1] for d in path) / 1000, 3)
        if service is not None:
            out["service"] = sorted(service)
        return out

    step = max(1, len(legs) // LEGS)
    cases = {"legs": [case([s], t, train) for s, t in legs[::step][:LEGS]]}
    step = max(1, len(routes) // WHOLE)
    picked = routes[::step][:WHOLE]
    cases["whole"] = [case([stops[0]], stops[-1], train) for _, stops in picked]
    step = max(1, len(routes) // SERVICE)
    cases["service"] = []
    for r, stops in routes[step // 2::step][:SERVICE]:
        ways = {int(w[1:]) for w in r["ways"]}
        cases["service"].append(case([stops[0]], stops[-1], S.service_factor(g, ways), ways))
    with open(args.out, "w") as f:
        json.dump(cases, f, separators=(",", ":"))
    print({k: len(v) for k, v in cases.items()})


if __name__ == "__main__":
    main()
