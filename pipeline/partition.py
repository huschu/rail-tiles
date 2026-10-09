"""
Keep every border way in exactly one region.

Geofabrik extracts overlap at borders: an extract holds every way with a node
inside its boundary, so a way crossing a border, or lying in the strip the
boundaries share, is built by both regions and drawn twice. Each region now
keeps a way unless a region earlier in name order also holds it, judged by
that region's boundary containing one of the way's nodes. Every region holding
the way reaches the same verdict from the same boundaries, and the region that
keeps it surely holds it, so nothing is lost or doubled.

A node counts as inside another region only when it lies at least MARGIN_M
inside that region's boundary. The boundaries in Geofabrik's index match the
.poly files Geofabrik cuts with, but a node a fraction of a metre inside the
line can fall outside the cut. Within the margin the worst case is a rare
duplicate, never a gap.

The boundaries come from pipeline/region_shapes.json (gen_regions.py --shapes),
so all regions of one build use the same ones.
"""
import json
import math

MARGIN_M = 10.0
# No region legitimately gives away this share of its ways; more means a
# neighbour's boundary is wrong (an antimeridian polygon, say).
MAX_DROP = 0.5


class Partition:
    def __init__(self, shapes_path, region):
        import numpy as np
        import shapely
        from shapely.geometry import shape

        self._np, self._shapely = np, shapely
        self.region = region
        shapes = json.load(open(shapes_path))
        self.candidates = []          # (name, bbox, shrunk boundary), earlier names only
        if region not in shapes:
            return
        own = shape(shapes[region])
        for name in sorted(shapes):
            if name >= region:
                break
            other = shape(shapes[name])
            if not other.intersects(own):
                continue
            lat = (other.bounds[1] + other.bounds[3]) / 2
            # Degrees per MARGIN_M; longitude degrees shrink with latitude, so
            # take the larger to stay on the duplicate side of the line.
            margin = MARGIN_M / (111320.0 * max(math.cos(math.radians(lat)), 0.2))
            inner = other.buffer(-margin)
            shapely.prepare(inner)
            self.candidates.append((name, inner.bounds, inner))
        self.given = {}               # neighbour -> ways left to it

    def keeps(self, coords):
        """False when an earlier region holds this way: one of its nodes lies
        inside that region's boundary by more than the margin."""
        if not self.candidates:
            return True
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
        for name, (x0, y0, x1, y1), inner in self.candidates:
            if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
                continue
            if self._shapely.intersects_xy(inner, self._np.array(xs), self._np.array(ys)).any():
                self.given[name] = self.given.get(name, 0) + 1
                return False
        return True
