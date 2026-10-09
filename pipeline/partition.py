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
        self.neighbours = []          # (name, bbox, boundary), every overlapping region
        self.given = {}               # neighbour -> ways left to it
        self.given_ways = {}          # way id -> the neighbour it was left to
        self.border_kept = []         # kept ways inside some neighbour's boundary
        if region not in shapes:
            return
        own = shape(shapes[region])
        for name in sorted(shapes):
            if name == region:
                continue
            other = shape(shapes[name])
            if not other.intersects(own):
                continue
            lat = (other.bounds[1] + other.bounds[3]) / 2
            # Degrees per MARGIN_M; longitude degrees shrink with latitude, so
            # take the larger to stay on the safe side of the line.
            margin = MARGIN_M / (111320.0 * max(math.cos(math.radians(lat)), 0.2))
            # Grown by the margin, so a way a neighbour leaves to this region
            # counts as a border way here even if it only grazes that boundary.
            outer = other.buffer(margin)
            shapely.prepare(outer)
            self.neighbours.append((name, outer.bounds, outer))
            if name > region:
                continue
            inner = other.buffer(-margin)
            shapely.prepare(inner)
            self.candidates.append((name, inner.bounds, inner))

    def _hits(self, regions, xs, ys):
        bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
        for name, (x0, y0, x1, y1), boundary in regions:
            if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
                continue
            if self._shapely.intersects_xy(boundary, self._np.array(xs), self._np.array(ys)).any():
                yield name

    def keeps(self, way_id, coords):
        """False when an earlier region holds this way: one of its nodes lies
        inside that region's boundary by more than the margin. Records the
        verdict for the join's cross-check (border_record)."""
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        owner = next(self._hits(self.candidates, xs, ys), None)
        if owner is not None:
            self.given[owner] = self.given.get(owner, 0) + 1
            self.given_ways[way_id] = owner
            return False
        if next(self._hits(self.neighbours, xs, ys), None) is not None:
            self.border_kept.append(way_id)
        return True

    def border_record(self):
        """The ways this region left to others and the border ways it kept,
        which check() compares across regions."""
        return {"given": self.given_ways, "kept": sorted(self.border_kept)}


def check(records):
    """Gaps and duplicates across regions' border records, {region: record}.
    A gap is a way one region left to another that the other did not keep;
    a duplicate is a way two regions kept."""
    kept_by = {}
    for region, rec in records.items():
        for w in rec["kept"]:
            kept_by.setdefault(w, []).append(region)
    gaps, missing = {}, {}
    for region, rec in records.items():
        for w, owner in rec["given"].items():
            if owner not in records:
                missing[owner] = missing.get(owner, 0) + 1
            elif owner not in kept_by.get(w, ()):
                key = f"{region} -> {owner}"
                gaps[key] = gaps.get(key, 0) + 1
    dups = {}
    for w, regions in kept_by.items():
        if len(regions) > 1:
            key = " + ".join(sorted(regions))
            dups[key] = dups.get(key, 0) + 1
    return gaps, dups, missing
