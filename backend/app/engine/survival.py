"""Opponent-aware survival probability: P(player still available at my next pick).

Deliberately NOT a naive ADP model. For each opponent picking between now and my
next turn we estimate the chance THEY take the player, combining:
  - adp_pressure   how 'in range' the player is for that pick slot (logistic on ADP)
  - need_factor    does that opponent still need the player's position to start?
  - tendency       reachers grab early (raises risk); value-waiters let him fall
  - rank_factor    opponents take the BEST available at a position, not the 5th;
                   deeper players at a position are correspondingly safer

P(survives) = product over intervening opponents of (1 - P(they take him)).
"""

from __future__ import annotations

import math

from .models import DraftState, Player
from .profiler import TeamProfile

# rank among available at his position -> realistic chance he's the one taken.
RANK_FACTORS = [1.0, 0.55, 0.32, 0.20, 0.12, 0.07]


def _rank_factor(rank: int) -> float:
    if rank < len(RANK_FACTORS):
        return RANK_FACTORS[rank]
    return 0.04


def adp_pressure(adp: float, pick_no: int, scale: float) -> float:
    """~1 when the pick is at/after the player's ADP (overdue), ~0 when well
    before it. Logistic so it's smooth."""
    return 1.0 / (1.0 + math.exp((adp - pick_no) / scale))


def _need_factor(prof: TeamProfile, position: str) -> float:
    if position in prof.needed_positions:
        # Distinguish a hard single-position need from a flex-only need.
        hard = any(position in {s} for s in prof.unfilled_starter_slots
                   if s == position)
        return 1.6 if hard else 1.15
    # Position already covered as a starter: only bench/upside interest.
    return 0.35


def _tendency_multiplier(prof: TeamProfile) -> float:
    # +1 ADP point of reaching ~ +1% grab likelihood; clamp to a sane band.
    m = 1.0 + 0.012 * prof.adp_deviation
    return max(0.6, min(1.6, m))


def _position_rank(player: Player, available: list[Player]) -> int:
    same = sorted(
        [p for p in available if p.position == player.position],
        key=lambda x: (x.vorp if x.vorp is not None else -1e9),
        reverse=True,
    )
    for i, p in enumerate(same):
        if p.player_id == player.player_id:
            return i
    return len(same)


def survival_probability(
    player: Player,
    state: DraftState,
    available: list[Player],
    profiles: dict[int, TeamProfile],
    intervening_team_ids: list[int],
) -> float:
    """Return P(player available at my next pick), in [0, 1]."""
    if not intervening_team_ids:
        return 1.0

    scale = max(3.0, state.num_teams / 2.0)
    rank_factor = _rank_factor(_position_rank(player, available))

    p_survive = 1.0
    pick_no = state.current_overall
    for tid in intervening_team_ids:
        pick_no += 1
        prof = profiles.get(tid)
        if prof is None:
            continue
        pressure = adp_pressure(player.adp, pick_no, scale)
        need = _need_factor(prof, player.position)
        tend = _tendency_multiplier(prof)
        p_take = pressure * need * tend * rank_factor
        p_take = max(0.0, min(0.97, p_take))
        p_survive *= (1.0 - p_take)
    return round(p_survive, 3)
