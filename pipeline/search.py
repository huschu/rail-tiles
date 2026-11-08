"""
Shortest plausible path over the track graph (graph.py).

The search runs over directed edges, so it knows which way a train faces at a
junction. Leaving a junction on an edge whose heading differs from the arrival
heading by more than MAX_TURN is not allowed: at a switch a train can go from
the trunk to either leg, never from one leg to the other. Turning back on the
same edge is allowed only at a stop position or at the end of the track, and
costs REVERSE_M, since trains reverse only where they stop.

The cost of an edge is its length times (V_MAX / line speed) ** SPEED_WEIGHT
times a factor for how unlikely a passenger train runs over it; None means it
never does. Speed counts because trains take the fast line over the short
one: Hamburg to Hannover runs the 178 km main line, not a 165 km branch. A*
with the straight-line distance stays exact because no factor is below 1.
"""
import heapq

from graph import metres

MAX_TURN = 60.0
# About five minutes at V_MAX.
REVERSE_M = 25000.0

V_MAX = 300.0
# Between the shortest path (0) and the fastest (1). The fastest matched the
# mapped services best stop to stop, but sent regional trains round on main
# lines when searched from first stop to last.
SPEED_WEIGHT = 0.5
# Track off a picked service costs this much more, so the search follows the
# service and only bridges gaps in its relation.
OFF_SERVICE = 10.0

TRAIN_KINDS = {"rail": 1.0, "narrow_gauge": 1.0, "light_rail": 1.5}
USAGE = {"industrial": 2.0, "military": 3.0, "test": 3.0}
SERVICE = {"crossover": 1.0, "siding": 1.5, "yard": 3.0, "spur": 3.0}
LIFECYCLE = {"present": 1.0, "preserved": 1.3, "disused": 4.0, "construction": 8.0}

# Line speed where the way has no maxspeed.
DEFAULT_SPEED = {"main": 120, "branch": 80}
SERVICE_SPEED = 40
OTHER_SPEED = 60


def pieces(g, e):
    """[(way, length, class)] of the ways edge e covers."""
    return g.pieces.get(e) or [(g.way[e], g.length[e], g.cls[e])]


def speed(cls):
    _, _, usage, service, v = cls
    if v is None:
        v = SERVICE_SPEED if service else DEFAULT_SPEED.get(usage, OTHER_SPEED)
    return min(max(v, 10), V_MAX)


def _per_edge(g, piece_factor):
    """An edge's factor from its pieces': their mean by length, so the edge
    costs what its pieces do one after another; None if any is."""
    def f(e):
        ps = pieces(g, e)
        if len(ps) == 1:
            return piece_factor(*ps[0][::2])
        total = cost = 0.0
        for way, length, cls in ps:
            pf = piece_factor(way, cls)
            if pf is None:
                return None
            total += length
            cost += length * pf
        return cost / total if total else piece_factor(*ps[0][::2])
    return f


def _train(cls, speed_weight):
    kind, lifecycle, usage, service, _ = cls
    k = TRAIN_KINDS.get(kind)
    lc = LIFECYCLE.get(lifecycle)
    if k is None or lc is None:
        return None
    u = USAGE.get(usage, 1.0)
    s = SERVICE.get(service, 2.0) if service else 1.0
    return (V_MAX / speed(cls)) ** speed_weight * k * lc * u * s


def train_factor(g, speed_weight=SPEED_WEIGHT):
    return _per_edge(g, lambda way, cls: _train(cls, speed_weight))


def service_factor(g, ways):
    """train_factor steered onto a service's member ways. A member way the
    train factor rules out, such as tram track a train route runs over,
    counts as track in service."""
    def f(way, cls):
        b = _train(cls, SPEED_WEIGHT)
        if way in ways:
            return b if b is not None else (V_MAX / speed(cls)) ** SPEED_WEIGHT
        return None if b is None else b * OFF_SERVICE
    return _per_edge(g, f)


def only_ways(g, ways):
    return _per_edge(g, lambda way, cls: 1.0 if way in ways else None)


def turn(h_in, h_out):
    d = abs(h_in - h_out) % 360.0
    return 360.0 - d if d > 180.0 else d


def find(g, sources, target, factor, max_turn=MAX_TURN):
    """(cost, directed edges) from one of `sources` (junctions) to
    `target`, or None. The train may leave a source either way."""
    tlon, tlat = g.lon[target], g.lat[target]

    def h(v):
        return metres(g.lon[v], g.lat[v], tlon, tlat)

    best, prev, heap = {}, {}, []
    for s in sources:
        if s == target:
            return 0.0, []
        for d in g.out[s]:
            f = factor(d >> 1)
            if f is None:
                continue
            c = g.length[d >> 1] * f
            if c < best.get(d, float("inf")):
                best[d], prev[d] = c, -1
                heapq.heappush(heap, (c + h(g.head(d)), c, d))
    while heap:
        _, c, d = heapq.heappop(heap)
        if c > best[d]:
            continue
        v = g.head(d)
        if v == target:
            path = []
            while d != -1:
                path.append(d)
                d = prev[d]
            return c, path[::-1]
        h_in, back = g.head_in(d), d ^ 1
        for d2 in g.out[v]:
            if d2 == back:
                if not (g.stop[v] or len(g.out[v]) == 1):
                    continue
                extra = REVERSE_M
            elif turn(h_in, g.head_out[d2]) > max_turn:
                continue
            else:
                extra = 0.0
            f = factor(d2 >> 1)
            if f is None:
                continue
            c2 = c + extra + g.length[d2 >> 1] * f
            if c2 < best.get(d2, float("inf")):
                best[d2], prev[d2] = c2, d
                heapq.heappush(heap, (c2 + h(g.head(d2)), c2, d2))
    return None
