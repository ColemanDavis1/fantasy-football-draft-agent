"""PLACEHOLDER projections from positional rank.

Sleeper exposes no projections, so until Phase 4 (real projections via ESPN
`kona_player_info` + the batched Claude enrichment pass) we approximate season
points from a player's rank within his position using simple decay curves. This
is good enough to exercise VORP/tiers on the live board and sanity-check the
engine, but it is NOT a real projection. Treat the absolute points as rough; the
ORDERING within a position is what's meaningful here.
"""

from __future__ import annotations

import math

# position: (top_points, floor_points, decay_scale) for a PPR season.
_CURVES = {
    "QB": (380, 200, 14),
    "RB": (330, 70, 22),
    "WR": (320, 70, 26),
    "TE": (250, 70, 10),
    "K": (150, 100, 12),
    "DEF": (140, 90, 12),
}


def project(position: str, pos_rank0: int) -> float:
    """Projected season points for the (0-based) Nth-best player at a position."""
    top, floor, scale = _CURVES.get(position, (200, 60, 20))
    return round(floor + (top - floor) * math.exp(-pos_rank0 / scale), 1)
