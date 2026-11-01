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


def speed(g, e):
    v = g.maxspeed[e]
    if v is None:
        v = SERVICE_SPEED if g.service[e] else DEFAULT_SPEED.get(g.usage[e], OTHER_SPEED)
    return min(max(v, 10), V_MAX)


def train_factor(g, speed_weight=SPEED_WEIGHT):
    def f(e):
        k = TRAIN_KINDS.get(g.kind[e])
        lc = LIFECYCLE.get(g.lifecycle[e])
        if k is None or lc is None:
            return None
        u = USAGE.get(g.usage[e], 1.0)
        s = SERVICE.get(g.service[e], 2.0) if g.service[e] else 1.0
        return (V_MAX / speed(g, e)) ** speed_weight * k * lc * u * s
    return f


def service_factor(g, ways, speed_weight=SPEED_WEIGHT):
    """train_factor steered onto a service's member ways. A member way the
    train factor rules out, such as tram track a train route runs over,
    counts as track in service."""
    base = train_factor(g, speed_weight)

    def f(e):
        b = base(e)
        if g.way[e] in ways:
            return b if b is not None else (V_MAX / speed(g, e)) ** speed_weight
        return None if b is None else b * OFF_SERVICE
    return f


def only_ways(g, ways):
    def f(e):
        return 1.0 if g.way[e] in ways else None
    return f


def turn(h_in, h_out):
    d = abs(h_in - h_out) % 360.0
    return 360.0 - d if d > 180.0 else d


def find(g, sources, target, factor, max_turn=MAX_TURN, reverse_m=REVERSE_M):
    """Directed edges from one of `sources` (junctions) to `target`, or None.
    The train may leave a source either way."""
    tlon, tlat = g.lon[target], g.lat[target]

    def h(v):
        return metres(g.lon[v], g.lat[v], tlon, tlat)

    best, prev, heap = {}, {}, []
    for s in sources:
        if s == target:
            return []
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
            return path[::-1]
        h_in, back = g.head_in[d], d ^ 1
        for d2 in g.out[v]:
            if d2 == back:
                if not (g.stop[v] or len(g.out[v]) == 1):
                    continue
                extra = reverse_m
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
