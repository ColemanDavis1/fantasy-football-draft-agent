"""Value tiers + tier-cliff detection.

Players within a position are grouped into tiers by VORP gaps: a new tier starts
where the drop in VORP from one player to the next is unusually large (a
'cliff'). Tiers matter more than exact ranks during a draft: if only one player
is left in a tier and he won't survive to your next pick, that's urgency to
draft now even over a nominally higher-ranked player in a deep tier.
"""

from __future__ import annotations

import statistics

from .models import Player


def assign_tiers(players: list[Player], gap_k: float = 1.0) -> None:
    """Assign .tier (1 = best) per position based on VORP gaps.

    A new tier begins when the VORP drop to the next player exceeds
    mean(drop) + gap_k * stdev(drop) for that position. Sets tier on every
    player in-place.
    """
    by_pos: dict[str, list[Player]] = {}
    for p in players:
        by_pos.setdefault(p.position, []).append(p)

    for plist in by_pos.values():
        plist.sort(key=lambda x: (x.vorp if x.vorp is not None else -1e9), reverse=True)
        if len(plist) == 1:
            plist[0].tier = 1
            continue
        drops = [
            (plist[i].vorp or 0.0) - (plist[i + 1].vorp or 0.0)
            for i in range(len(plist) - 1)
        ]
        positive = [d for d in drops if d > 0]
        if positive:
            mean_d = statistics.fmean(positive)
            std_d = statistics.pstdev(positive) if len(positive) > 1 else 0.0
            threshold = mean_d + gap_k * std_d
        else:
            threshold = float("inf")

        tier = 1
        plist[0].tier = 1
        for i in range(1, len(plist)):
            if drops[i - 1] > threshold:
                tier += 1
            plist[i].tier = tier


def tier_members_available(available: list[Player], position: str, tier: int) -> list[Player]:
    return [p for p in available if p.position == position and p.tier == tier]


def players_left_in_tier(available: list[Player], player: Player) -> int:
    """How many players (including this one) remain available in his tier."""
    if player.tier is None:
        return 0
    return len(tier_members_available(available, player.position, player.tier))


def tier_dropoff(available: list[Player], player: Player) -> float:
    """VORP gap from this player down to the best available player at the same
    position in a worse tier — i.e. the value cliff you face at his position
    once this tier is gone. Large dropoff + few left = scarce; draft now."""
    if player.tier is None:
        return 0.0
    pv = player.vorp or 0.0
    lower = [p for p in available
             if p.position == player.position and p.tier is not None
             and p.tier > player.tier]
    if not lower:
        return max(pv, 0.0)  # nothing comparable left below him
    best_lower = max(lower, key=lambda p: (p.vorp if p.vorp is not None else -1e9))
    return max(pv - (best_lower.vorp or 0.0), 0.0)


def quality_remaining(available: list[Player], position: str,
                      min_vorp: float = 0.0) -> int:
    """How many startable-quality players (VORP >= min_vorp) remain at a
    position. A blunt cross-position scarcity gauge."""
    return sum(1 for p in available
               if p.position == position and (p.vorp or -1e9) >= min_vorp)
