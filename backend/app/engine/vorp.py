"""Value Based Drafting (VORP / VBD).

A player's value = projected points OVER REPLACEMENT at his position, where the
replacement level is the best player at that position who will NOT be a starter
across the whole league. Ranking by VORP (not raw points) is what makes a 250pt
RB worth more than a 260pt QB when QB is deep.

Replacement baselines are computed from YOUR league's starting requirements:
  - dedicated slots (QB/RB/WR/TE/...) contribute num_teams * count starters.
  - flex/superflex slots are allocated by a greedy fill: after dedicated
    starters are removed, the next-best eligible players (by projection) take
    the flex openings, position by position. This handles FLEX (RB/WR/TE),
    WR/TE, and superflex (OP) correctly instead of ignoring them.
"""

from __future__ import annotations

from .models import LeagueSettings, Player
from .slots import eligible_positions


def _group_by_position(players: list[Player]) -> dict[str, list[Player]]:
    by_pos: dict[str, list[Player]] = {}
    for p in players:
        by_pos.setdefault(p.position, []).append(p)
    for plist in by_pos.values():
        plist.sort(key=lambda x: x.proj_points, reverse=True)
    return by_pos


def compute_replacement_levels(
    players: list[Player], league: LeagueSettings
) -> tuple[dict[str, int], dict[str, float]]:
    """Return (replacement_rank, replacement_points) per position.

    replacement_rank[pos] = number of `pos` players who will be league starters
    (dedicated + flex). replacement_points[pos] = projection of the first
    non-starter at that position (the replacement-level player).
    """
    by_pos = _group_by_position(players)

    dedicated: dict[str, int] = {}
    flex_slots: list[tuple[str, int, set[str]]] = []  # (name, openings, eligible)
    for slot, count in league.starter_slots.items():
        elig = eligible_positions(slot)
        if not elig:
            continue
        openings = count * league.num_teams
        if len(elig) == 1:
            pos = next(iter(elig))
            dedicated[pos] = dedicated.get(pos, 0) + openings
        else:
            flex_slots.append((slot, openings, elig))

    # Fill most-restrictive flex slots first so flexible openings aren't wasted.
    flex_slots.sort(key=lambda s: len(s[2]))

    # pointers[pos] = index of the next not-yet-assigned player at that position.
    pointers = {pos: dedicated.get(pos, 0) for pos in by_pos}
    for pos in dedicated:
        pointers.setdefault(pos, dedicated[pos])
    flex_filled: dict[str, int] = {}
    openings_left = [openings for (_, openings, _) in flex_slots]

    total_flex = sum(openings_left)
    for _ in range(total_flex):
        best_pos = None
        best_slot = None
        best_proj = None
        for slot_i, (_, _, elig) in enumerate(flex_slots):
            if openings_left[slot_i] <= 0:
                continue
            for pos in elig:
                plist = by_pos.get(pos)
                if not plist:
                    continue
                idx = pointers.get(pos, 0)
                if idx >= len(plist):
                    continue
                cand_proj = plist[idx].proj_points
                if best_proj is None or cand_proj > best_proj:
                    best_proj, best_pos, best_slot = cand_proj, pos, slot_i
        if best_pos is None:
            break
        pointers[best_pos] += 1
        flex_filled[best_pos] = flex_filled.get(best_pos, 0) + 1
        openings_left[best_slot] -= 1

    replacement_rank: dict[str, int] = {}
    replacement_points: dict[str, float] = {}
    all_positions = set(by_pos) | set(dedicated)
    for pos in all_positions:
        rank = dedicated.get(pos, 0) + flex_filled.get(pos, 0)
        replacement_rank[pos] = rank
        plist = by_pos.get(pos, [])
        if not plist:
            replacement_points[pos] = 0.0
        elif rank < len(plist):
            replacement_points[pos] = plist[rank].proj_points
        else:
            replacement_points[pos] = plist[-1].proj_points
    return replacement_rank, replacement_points


def assign_vorp(players: list[Player], league: LeagueSettings) -> dict[str, float]:
    """Compute and set .vorp on every player. Returns replacement_points used.

    NOTE: baselines must be computed over the FULL player pool (drafted +
    available), not just what's left, so replacement levels stay stable as the
    draft depletes the board.
    """
    _, replacement_points = compute_replacement_levels(players, league)
    for p in players:
        baseline = replacement_points.get(p.position, 0.0)
        p.vorp = round(p.proj_points - baseline, 2)
    return replacement_points
