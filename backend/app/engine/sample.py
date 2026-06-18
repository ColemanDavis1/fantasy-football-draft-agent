"""A hardcoded, realistic sample draft state for testing the engine offline.

12-team PPR snake. Roster: QB, 2RB, 2WR, TE, FLEX(RB/WR/TE), D/ST, K + bench.
Draft order is team_ids 1..12; I am team 5 (draft slot 5). The board is built
with deliberate tier cliffs (notably an elite TE1) and the first 28 picks are
authored to create recognizable opponent archetypes:

  - team 3  drafts RB, RB         -> Robust-RB
  - team 8  drafts WR, WR         -> Zero-RB
  - team 1  drafts WR1 then QB1 (same NFL team) -> a stack
  - picks 25-27 are three straight QBs -> an active QB run when I'm on the clock

I am on the clock at overall pick 29 (my round-3 pick); my next pick is 44.
"""

from __future__ import annotations

from .draftflow import slot_index_for_overall
from .models import DraftState, LeagueSettings, Pick, Player

# Top-of-position projections with intentional gaps, then a smooth decay tail.
_POS_SPEC = {
    # position: (top_values, total_count, tail_slope, floor)
    "QB": ([385, 360, 348, 318, 312, 306, 300, 288, 284, 280, 276, 272], 30, 4.0, 150),
    "RB": ([322, 305, 292, 268, 258, 250, 242, 235, 212, 206, 200, 195, 190, 185, 180], 60, 3.5, 40),
    "WR": ([312, 305, 298, 291, 272, 266, 260, 254, 248, 242, 228, 223, 219, 215, 211, 207], 70, 3.0, 40),
    "TE": ([248, 196, 188, 168, 160, 154], 30, 4.0, 50),
    "K": ([150, 145, 141, 138, 135, 132, 130, 128, 126, 124], 20, 2.0, 90),
    "DEF": ([135, 128, 123, 119, 116, 113, 111, 109, 107, 105], 20, 1.5, 80),
}

# Rough replacement-points guess per position, used only to derive a market ADP
# for the sample (independent of the engine's own VORP baselines).
_ADP_REPL = {"QB": 272, "RB": 185, "WR": 211, "TE": 154, "K": 124, "DEF": 105}

_BYES = [5, 6, 7, 9, 10, 11, 12, 13, 14]


def _projection(spec, rank0: int) -> float:
    tops, n, slope, floor = spec
    if rank0 < len(tops):
        return float(tops[rank0])
    extra = rank0 - (len(tops) - 1)
    return float(max(floor, tops[-1] - slope * extra))


def _build_pool() -> dict[str, Player]:
    players: dict[str, Player] = {}
    for pos, spec in _POS_SPEC.items():
        _, n, _, _ = spec
        for i in range(n):
            pid = f"{pos}{i + 1}"
            players[pid] = Player(
                player_id=pid,
                name=pid,
                position=pos,
                team=None,
                bye_week=_BYES[i % len(_BYES)],
                proj_points=_projection(spec, i),
            )
    # One real-looking stack: WR1 and QB1 share an NFL team.
    players["WR1"].team = "BUF"
    players["QB1"].team = "BUF"

    # Derive a consensus ADP from market value (proj over a rough replacement).
    # Skill positions are drafted first; K and D/ST realistically go last, so
    # rank them after every skill player rather than by raw market value.
    skill = [p for p in players.values() if p.position not in ("K", "DEF")]
    streamers = [p for p in players.values() if p.position in ("K", "DEF")]
    skill.sort(key=lambda p: p.proj_points - _ADP_REPL.get(p.position, 0), reverse=True)
    streamers.sort(key=lambda p: p.proj_points - _ADP_REPL.get(p.position, 0), reverse=True)
    for adp, p in enumerate(skill + streamers, start=1):
        p.adp = float(adp)
    return players


# The first 28 picks, in overall order. Team for each is derived from the snake.
_DRAFT_SO_FAR = [
    "WR1", "RB1", "RB2", "WR2", "RB3", "WR3", "RB4", "WR4", "RB5", "WR5", "TE1", "RB6",   # R1
    "WR6", "WR7", "RB7", "WR8", "WR9", "WR10", "RB8", "WR11", "RB9", "RB10", "WR12", "RB11",  # R2
    "QB1", "QB2", "QB3", "WR13",                                                          # R3 (partial)
]

NUM_TEAMS = 12
MY_TEAM_ID = 5


def build_sample_state() -> DraftState:
    players = _build_pool()
    league = LeagueSettings(
        num_teams=NUM_TEAMS,
        starter_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1},
        roster_size=16,
        scoring_type="ppr",
        is_superflex=False,
    )
    draft_order = list(range(1, NUM_TEAMS + 1))  # team_ids == draft slots here

    picks: list[Pick] = []
    for i, pid in enumerate(_DRAFT_SO_FAR):
        overall = i + 1
        slot = slot_index_for_overall(overall, NUM_TEAMS)  # 0-based
        team_id = draft_order[slot]
        picks.append(Pick(overall=overall, team_id=team_id, player_id=pid))

    return DraftState(
        league=league,
        draft_order=draft_order,
        picks=picks,
        my_team_id=MY_TEAM_ID,
        players_by_id=players,
    )
