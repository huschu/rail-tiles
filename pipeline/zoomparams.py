"""
Per-zoom simplification tolerance and parallel-collapse distance.

The whole pyramid is 512 px tiles (see the vertex-budget gate). Tolerance
tracks one pixel of longitude; collapse distance tracks ~1.5 px of ground but
is capped so it can never weld a genuinely separate line.
"""
import math

TILE_PX = 512
MIN_ZOOM = 4
MAX_ZOOM = 16

CHAIN_MAX_ZOOM = 11      # chain z4-z11, raw ways from z12 (rule 3)
SERVICE_MIN_ZOOM = 12    # service track only from z12 (rule 2)
COLLAPSE_MAX_ZOOM = 14   # collapse off from z15 up (double track resolves)

_TOL_PX = 0.8            # DP tolerance in pixels
_COLLAPSE_PX = 1.5       # collapse reach in pixels of ground
_COLLAPSE_CAP_M = 10.0   # never collapse track more than this far apart
                         # (measured sweet spot; 50 m only adds 6 pts and risks
                         # welding separate corridors)


def tol_deg(z):
    """DP tolerance in degrees: _TOL_PX pixels of longitude at zoom z."""
    return _TOL_PX * 360.0 / (TILE_PX * (2 ** z))


def _ground_m_per_px(z, lat):
    """Web Mercator ground metres per pixel at latitude, 512 px tiles."""
    return (156543.03392 * math.cos(math.radians(lat)) / (2 ** z)) / 2.0


def collapse_dist_m(z, lat):
    """Collapse reach in ground metres, capped. Below z12 the pixel figure is
    huge so the cap dominates (≈10 m everywhere), which is where the 36.6 %
    saving was measured."""
    return min(_COLLAPSE_PX * _ground_m_per_px(z, lat), _COLLAPSE_CAP_M)
