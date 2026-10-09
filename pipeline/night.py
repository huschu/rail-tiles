#!/usr/bin/env python3
"""
Night trains: the route=train relations the app shows as night trains.

A relation is a night train when it carries sleepers (sleeping_car=yes or
couchette=yes), at any length, or when its service includes night
(service=night, night;motorail) and the route is at least MIN_KM long. The
length check keeps out local trains that only run at night, such as NS
Nachtnet, which map with service=night too.

The length must be measured over the whole relation, and night trains cross
borders, so no region can decide alone. Each region build writes a sidecar of
its candidates with the length of every member way it holds; the join reads
all sidecars and decides. Geofabrik extracts overlap at borders and a way can
sit in two of them, so lengths are united by way id, never summed per region.

A train's service is the route_master=train relation that lists it, such as
"NJ 294/NJ 295: München <=> Rom" for both directions or a EuroNight whose
branches split on the way. A train no route master lists is its own service.

The tiles do not change: the app matches the published member ways against
the src and absorbed ways the tiles already carry.

  night.py join REGIONS_DIR OUT.json.gz     decide and write the asset
"""
import glob
import gzip
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routes

MIN_KM = 300


def _values(tags, key):
    return {v.strip() for v in (tags.get(key) or "").split(";")}


def sleeper(tags):
    return tags.get("sleeping_car") == "yes" or tags.get("couchette") == "yes"


def night_service(tags):
    return "night" in _values(tags, "service")


def candidate(tags):
    return tags.get("route") == "train" and (sleeper(tags) or night_service(tags))


def way_length_m(coords):
    m = 0.0
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        p0, p1 = math.radians(y0), math.radians(y1)
        a = (math.sin((p1 - p0) / 2) ** 2
             + math.cos(p0) * math.cos(p1) * math.sin(math.radians(x1 - x0) / 2) ** 2)
        m += 2 * 6371008.8 * math.asin(math.sqrt(a))
    return m


def sidecar(opl_lines, way_coords):
    """This region's candidates: each relation's tags, full member list and
    stops, plus the length in metres of every member way the region holds,
    and the route masters that list a candidate."""
    relations, lengths, masters = [], {}, []
    for line in opl_lines:
        r = routes.parse_relation(line)
        if r is None:
            continue
        if r["tags"].get("type") == "route_master" and r["tags"].get("route_master") == "train":
            masters.append({"id": int(r["rid"]), "version": r["version"], "tags": r["tags"],
                            "routes": [int(m[1:]) for m, _ in r["members"] if m.startswith("r")]})
            continue
        if not candidate(r["tags"]):
            continue
        relations.append({
            "id": int(r["rid"]), "version": r["version"], "tags": r["tags"],
            "ways": [int(w[1:]) for w in r["ways"]],
            "stops": [int(n) for n, role in r["nodes"] if role.startswith("stop")],
        })
        for w in r["ways"]:
            c = way_coords.get(w)
            if c is not None and w not in lengths:
                lengths[w] = round(way_length_m(c))
    ids = {r["id"] for r in relations}
    masters = [m for m in masters if ids.intersection(m["routes"])]
    return {"relations": relations, "masters": masters,
            "lengths": {w[1:]: m for w, m in lengths.items()}}


def _newest(records, into):
    for r in records:
        have = into.get(r["id"])
        if have is None or r["version"] > have["version"]:
            into[r["id"]] = r


def decide(sidecars):
    """{trains, services}: the night trains across all regions by relation
    id, and the route masters they belong to. Each relation comes from the
    region with its newest version."""
    rels, masters, lengths = {}, {}, {}
    for sc in sidecars:
        _newest(sc["relations"], rels)
        _newest(sc.get("masters", []), masters)
        for w, m in sc["lengths"].items():
            lengths[w] = max(m, lengths.get(w, 0))
    # A train two route masters list goes to the lower id, so builds agree.
    service_of = {}
    for mid in sorted(masters, reverse=True):
        for rid in masters[mid]["routes"]:
            service_of[rid] = mid
    trains = []
    for rid in sorted(rels):
        r = rels[rid]
        km = sum(lengths.get(str(w), 0) for w in set(r["ways"])) / 1000
        if sleeper(r["tags"]) or km >= MIN_KM:
            train = {"id": rid, "tags": r["tags"], "ways": r["ways"],
                     "stops": r["stops"], "km": round(km)}
            if rid in service_of:
                train["service"] = service_of[rid]
            trains.append(train)
    used = sorted({t["service"] for t in trains if "service" in t})
    services = [{"id": mid, "tags": masters[mid]["tags"]} for mid in used]
    return {"trains": trains, "services": services}


def join(regions_dir, out):
    sidecars, missing = [], []
    for pmt in sorted(glob.glob(os.path.join(regions_dir, "**", "*.pmtiles"), recursive=True)):
        sc = pmt[:-len(".pmtiles")] + ".night.json"
        if os.path.exists(sc):
            with open(sc) as f:
                sidecars.append(json.load(f))
        else:
            missing.append(os.path.basename(pmt)[:-len(".pmtiles")])
    if missing:
        # A tileset restored from before sidecars existed: its stretch of a
        # cross-border route counts as zero until the region rebuilds.
        print(f"::warning::no night sidecar for {len(missing)} regions: "
              + ", ".join(missing), file=sys.stderr)
    decided = decide(sidecars)
    trains = decided["trains"]
    with gzip.open(out, "wt") as f:
        json.dump({"minKm": MIN_KM, **decided}, f, separators=(",", ":"))
    print(f"night trains: {len(trains)} in {len(decided['services'])} route masters "
          f"from {len(sidecars)} regions -> "
          f"{os.path.getsize(out) / 1024:.0f} KB", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "join":
        sys.exit("usage: night.py join REGIONS_DIR OUT.json.gz")
    join(sys.argv[2], sys.argv[3])
