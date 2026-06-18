"""Snake-draft order math.

Given the current pick and my draft slot, figure out which teams pick before my
next turn. That set of intervening teams is the core input to the survival model
('will he get back to me?').
"""

from __future__ import annotations

from .models import DraftState


def slot_index_for_overall(overall: int, num_teams: int) -> int:
    """0-based draft-slot index that owns a given overall pick in a snake draft."""
    rnd = (overall - 1) // num_teams           # 0-based round
    pos = (overall - 1) % num_teams            # 0-based position within round
    if rnd % 2 == 0:                            # odd rounds (1,3,..): left to right
        return pos
    return num_teams - 1 - pos                  # even rounds: snake back


def team_id_for_overall(state: DraftState, overall: int) -> int:
    idx = slot_index_for_overall(overall, state.num_teams)
    return state.draft_order[idx]


def my_slot_index(state: DraftState) -> int:
    return state.draft_order.index(state.my_team_id)


def next_pick_for_team(state: DraftState, team_id: int, after_overall: int) -> int | None:
    """Smallest overall strictly greater than `after_overall` owned by team_id.
    Bounded by the full draft length (num_teams * roster_size)."""
    max_overall = state.num_teams * state.league.roster_size
    o = after_overall + 1
    while o <= max_overall:
        if team_id_for_overall(state, o) == team_id:
            return o
        o += 1
    return None


def intervening_team_ids(state: DraftState, from_overall: int | None = None) -> list[int]:
    """Team_ids picking between the pick I'm making now and my next pick
    (exclusive of both). Empty if I pick back-to-back at a snake turn."""
    current = from_overall or state.current_overall
    my_next = next_pick_for_team(state, state.my_team_id, current)
    if my_next is None:
        # No more picks after this one; everyone left picks before "never".
        return []
    return [team_id_for_overall(state, o) for o in range(current + 1, my_next)]


def picks_until_my_next(state: DraftState) -> int:
    return len(intervening_team_ids(state))
