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
class _Grid:
    """Bucket surviving vertices by metre cell so a proximity query touches
    only a 3x3 neighbourhood. Bucketing uses a fixed reference cos(lat); the
    actual accept/reject distance is the precise mean-latitude one."""

    def __init__(self, cell_m, cos_ref):
        self.cell = cell_m
        self.cos_ref = cos_ref
        self.buckets = defaultdict(list)

    def _cell(self, lon, lat):
        x = lon * K * self.cos_ref
        y = lat * K
        return (int(x // self.cell), int(y // self.cell))

    def add(self, lon, lat):
        self.buckets[self._cell(lon, lat)].append((lon, lat))

    def near(self, lon, lat, dist_m):
        cx, cy = self._cell(lon, lat)
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for (olon, olat) in self.buckets.get((gx, gy), ()):
                    if _equirect_m(lon, lat, olon, olat) < dist_m:
                        return True
        return False


def _equirect_m(lon1, lat1, lon2, lat2):
    mlat = math.radians((lat1 + lat2) * 0.5)
    dx = (lon1 - lon2) * K * math.cos(mlat)
    dy = (lat1 - lat2) * K
    return math.hypot(dx, dy)


def collapse(polys, dist_m, cos_ref):
    """Segment-level parallel collapse within one render key.

    Process longest first so a continuous main track survives whole; each later
    polyline's vertices that fall within `dist_m` of an already-surviving vertex
    are marked shadowed, and maximal shadowed runs are dropped. The divergent
    remainder is kept as its own feature. No threshold, no grid-snapping, so
    nothing is welded and no geometry is invented.

    Caller must group by render key before calling: only identically-rendered
    track may merge.
    """
    grid = _Grid(max(dist_m, 1e-6), cos_ref)
    order = sorted(range(len(polys)), key=lambda i: len(polys[i]["coords"]), reverse=True)
    out = []
    for idx in order:
        p = polys[idx]
        coords, src = p["coords"], p["src"]
        shadow = [grid.near(x, y, dist_m) for (x, y) in coords]
        # split into maximal unshadowed runs
        run_c, run_s = [], []
        for (x, y), s, sh in zip(coords, src, shadow):
            if sh:
                if len(run_c) >= 2:
                    out.append(_sub(p, run_c, run_s))
                run_c, run_s = [], []
            else:
                run_c.append((x, y))
                run_s.append(s)
        if len(run_c) >= 2:
            out.append(_sub(p, run_c, run_s))
        # everything this polyline kept now shadows later ones
        for (x, y), sh in zip(coords, shadow):
            if not sh:
                grid.add(x, y)
    return out


def _sub(p, coords, src):
    out = dict(p)
    out["coords"] = coords
    out["src"] = src
    return out


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
