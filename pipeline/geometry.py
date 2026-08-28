"""
Geometry operations: Douglas-Peucker, chaining, parallel-track collapse.

Every polyline carries a per-vertex source osm_id (`src`), so that after any
number of simplification or collapse passes drop vertices, the surviving
vertices still map back to real OSM ways. Source spans (rule 4) are read off
this at emit time, per band, rather than stored once against one geometry.
"""
import math
from collections import defaultdict

K = 111320.0  # metres per degree latitude, and per degree longitude at equator


# ---------------------------------------------------------------- Douglas-Peucker
def dp_keep(coords, tol):
    """Return the boolean keep-mask so a caller can subset a parallel payload
    (the per-vertex src) with the same indices."""
    n = len(coords)
    if n <= 2:
        return [True] * n
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        if e - s < 2:
            continue
        ax, ay = coords[s]
        bx, by = coords[e]
        dx, dy = bx - ax, by - ay
        lensq = dx * dx + dy * dy
        maxd, maxi = -1.0, -1
        for i in range(s + 1, e):
            px, py = coords[i]
            if lensq == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / lensq
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > maxd:
                maxd, maxi = d, i
        if maxd > tol and maxi > 0:
            keep[maxi] = True
            stack.append((s, maxi))
            stack.append((maxi, e))
    return keep


def simplify(poly, tol):
    """poly = {'coords': [(x,y)], 'src': [id], ...}. Return a new poly with
    vertices dropped by DP at `tol`, src kept in step."""
    mask = dp_keep(poly["coords"], tol)
    out = dict(poly)
    out["coords"] = [c for c, k in zip(poly["coords"], mask) if k]
    out["src"] = [s for s, k in zip(poly["src"], mask) if k]
    return out


# ---------------------------------------------------------------- chaining
def _r(v):
    return round(v, 7)


def chain(ways, render_key, speed_of, band_of, max_band_span=1):
    """Join ways end-to-end where the render key matches and the speed span
    stays inside one band. `ways` is a list of dicts with 'id', 'props',
    'coords'. Returns chained polylines, each with per-vertex src and the
    fastest member's speed. Topology only: no geometry is simplified here, so
    the same chaining is reused for every low band (rule 3)."""
    n = len(ways)
    keys = [render_key(w["props"]) for w in ways]
    speeds = [speed_of(w["props"]) for w in ways]
    bands = [band_of(s) for s in speeds]

    parent = list(range(n))
    rng = [(bands[i], bands[i]) for i in range(n)]

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    ends = defaultdict(list)
    for i, w in enumerate(ways):
        c = w["coords"]
        ends[(_r(c[0][0]), _r(c[0][1]))].append(i)
        ends[(_r(c[-1][0]), _r(c[-1][1]))].append(i)

    for _, ids in ends.items():
        if len(ids) != 2 or ids[0] == ids[1]:
            continue
        a, b = ids
        if keys[a] != keys[b]:
            continue
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        la, ha = rng[ra]
        lb, hb = rng[rb]
        # never mix known and unknown speed: grey is its own legend category
        if (la is None) != (lb is None):
            continue
        if la is not None:
            lo, hi = min(la, lb), max(ha, hb)
            if hi - lo > max_band_span:
                continue
        else:
            lo = hi = None
        parent[ra] = rb
        rng[rb] = (lo, hi)

    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    out = []
    for members in groups.values():
        coords, src = _stitch(members, ways)
        if len(coords) < 2:
            continue
        known = [speeds[i] for i in members if speeds[i] is not None]
        out.append({
            "coords": coords,
            "src": src,
            "props": ways[members[0]]["props"],
            "key": keys[members[0]],
            "maxspeed": max(known) if known else None,  # fastest, span-capped
        })
    return out


def _stitch(members, ways):
    """Walk a group's ways end-to-end into one ordered polyline, tagging each
    vertex with the osm_id of the way it came from."""
    if len(members) == 1:
        w = ways[members[0]]
        return list(w["coords"]), [w["id"]] * len(w["coords"])
    ends = defaultdict(list)
    for i in members:
        c = ways[i]["coords"]
        ends[(_r(c[0][0]), _r(c[0][1]))].append(i)
        ends[(_r(c[-1][0]), _r(c[-1][1]))].append(i)
    start = None
    for pt, ids in ends.items():
        if len(ids) == 1:
            start = (pt, ids[0])
            break
    if start is None:  # closed ring, start anywhere
        first = next(iter(ends))
        start = (first, ends[first][0])
    used = set()
    pt, cur = start
    coords, src = [], []
    while True:
        used.add(cur)
        seg = ways[cur]["coords"]
        sid = ways[cur]["id"]
        if (_r(seg[0][0]), _r(seg[0][1])) != pt:
            seg = list(reversed(seg))
        add = seg if not coords else seg[1:]
        coords.extend(add)
        src.extend([sid] * len(add))
        tail = (_r(seg[-1][0]), _r(seg[-1][1]))
        nxt = [i for i in ends.get(tail, []) if i not in used and i in members]
        if not nxt:
            break
        pt, cur = tail, nxt[0]
    return coords, src


# ---------------------------------------------------------------- parallel collapse
def _equirect_m(lon1, lat1, lon2, lat2):
    mlat = math.radians((lat1 + lat2) * 0.5)
    dx = (lon1 - lon2) * K * math.cos(mlat)
    dy = (lat1 - lat2) * K
    return math.hypot(dx, dy)


def _dirs(coords, cos_ref):
    """Unit direction (in metre space) of the polyline at each vertex, averaging
    the incoming and outgoing segments. Direction is undirected for comparison."""
    n = len(coords)
    segs = []
    for i in range(n - 1):
        dx = (coords[i + 1][0] - coords[i][0]) * K * cos_ref
        dy = (coords[i + 1][1] - coords[i][1]) * K
        m = math.hypot(dx, dy) or 1.0
        segs.append((dx / m, dy / m))
    if not segs:
        return [(1.0, 0.0)] * n
    out = []
    for i in range(n):
        a = segs[i - 1] if i > 0 else segs[0]
        b = segs[i] if i < n - 1 else segs[-1]
        # average as undirected: flip b to a's half-plane before summing
        if a[0] * b[0] + a[1] * b[1] < 0:
            b = (-b[0], -b[1])
        vx, vy = a[0] + b[0], a[1] + b[1]
        m = math.hypot(vx, vy) or 1.0
        out.append((vx / m, vy / m))
    return out


class _BearingGrid:
    """Survivor vertices bucketed by metre cell, each carrying its track
    direction and the id of the feature it belongs to. A query returns the id of
    the nearest survivor that is both within `dist` and running the same
    direction (undirected) — so a crossing or a perpendicular near-pass never
    shadows, and neither does a line merely sharing a corridor at an angle."""

    def __init__(self, cell_m, cos_ref):
        self.cell = max(cell_m, 1e-6)
        self.cos_ref = cos_ref
        self.buckets = defaultdict(list)

    def _cell(self, lon, lat):
        return (int(lon * K * self.cos_ref // self.cell), int(lat * K // self.cell))

    def add(self, lon, lat, ux, uy, fid):
        self.buckets[self._cell(lon, lat)].append((lon, lat, ux, uy, fid))

    def shadow(self, lon, lat, ux, uy, dist_m, cos_tol):
        cx, cy = self._cell(lon, lat)
        best_id, best_d = None, dist_m
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for (olon, olat, oux, ouy, fid) in self.buckets.get((gx, gy), ()):
                    if abs(ux * oux + uy * ouy) < cos_tol:
                        continue                      # bearings disagree
                    d = _equirect_m(lon, lat, olon, olat)
                    if d < best_d:
                        best_id, best_d = fid, d
        return best_id


def collapse(polys, dist_m, cos_ref, bearing_tol_deg=20.0):
    """Segment-level parallel collapse within one render key.

    Delete a run of a line's vertices only where a *single* already-surviving
    feature runs alongside it, same direction, for the whole run — a genuine
    duplicate track. This must never sever a line that merely crosses, passes
    near, or shares a corridor with other same-key track: those were the gaps
    that the earlier proximity-only version cut into single lines.

    Guards (all required):
      - bearing agreement over the overlap (rejects crossings / near-passes)
      - one survivor for the entire deleted run (rejects station throats and
        corridors, where the shadow is a patchwork of different features)
      - endpoints and junction vertices are never dropped, so no gap opens at a
        connection point; the longest feature is processed first and kept whole,
        so a survivor is never severed.

    Caller must group by render key before calling: only identically-rendered
    track may merge.
    """
    if len(polys) < 2:
        return list(polys)
    cos_tol = math.cos(math.radians(bearing_tol_deg))
    order = sorted(range(len(polys)), key=lambda i: len(polys[i]["coords"]), reverse=True)

    # every feature's own endpoints are junction candidates; never drop them
    junctions = set()
    for p in polys:
        junctions.add((round(p["coords"][0][0], 7), round(p["coords"][0][1], 7)))
        junctions.add((round(p["coords"][-1][0], 7), round(p["coords"][-1][1], 7)))

    grid = _BearingGrid(dist_m, cos_ref)
    out = []
    for fid, idx in enumerate(order):
        p = polys[idx]
        coords, src = p["coords"], p["src"]
        n = len(coords)
        dirs = _dirs(coords, cos_ref)
        shadow = [grid.shadow(coords[i][0], coords[i][1], dirs[i][0], dirs[i][1], dist_m, cos_tol)
                  for i in range(n)]
        for i in range(n):
            if i == 0 or i == n - 1 or (round(coords[i][0], 7), round(coords[i][1], 7)) in junctions:
                shadow[i] = None                      # protect endpoints and junctions

        for c, s in _drop_single_survivor_runs(coords, src, shadow):
            if len(c) >= 2:
                q = dict(p)
                q["coords"], q["src"] = c, s
                out.append(q)

        kept_dirs = _dirs(coords, cos_ref)
        for i in range(n):
            grid.add(coords[i][0], coords[i][1], kept_dirs[i][0], kept_dirs[i][1], fid)
    return out


def _drop_single_survivor_runs(coords, src, shadow):
    """Yield kept (coords, src) segments, deleting only maximal runs of vertices
    all shadowed by the same survivor id. A run shadowed by different ids over
    its length is a corridor, not a duplicate, and is kept."""
    n = len(coords)
    delete = [False] * n
    i = 0
    while i < n:
        if shadow[i] is None:
            i += 1
            continue
        j = i
        while j < n and shadow[j] is not None:
            j += 1
        run = shadow[i:j]                             # maximal shadowed run
        if len(set(run)) == 1:                        # one survivor for the whole run
            for k in range(i, j):
                delete[k] = True
        i = j

    seg_c, seg_s = [], []
    for i in range(n):
        if delete[i]:
            if len(seg_c) >= 2:
                yield seg_c, seg_s
            seg_c, seg_s = [], []
        else:
            seg_c.append(coords[i])
            seg_s.append(src[i])
    if len(seg_c) >= 2:
        yield seg_c, seg_s


def spans_of(src):
    """Collapse a per-vertex src list into [[osm_id, start_idx, end_idx], ...]
    contiguous runs. end_idx is inclusive."""
    if not src:
        return []
    runs = []
    cur = src[0]
    start = 0
    for i in range(1, len(src)):
        if src[i] != cur:
            runs.append([cur, start, i - 1])
            cur, start = src[i], i
    runs.append([cur, start, len(src) - 1])
    return runs
