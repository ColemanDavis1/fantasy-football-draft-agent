"""Deterministic opponent profiler (runs on every pick, no LLM).

Per team it computes:
  - position_counts        what they've drafted
  - unfilled_starter_slots roster needs (slot-aware, flex-aware)
  - needed_positions       positions that still fill an empty starting slot
  - adp_deviation          do they reach ahead of value (+) or wait for it (-)?
  - run_participation      do they start/follow positional runs?
  - stacks                 QB + same-NFL-team WR/TE
  - bye_conflicts          starters sharing a bye week
  - archetype              Zero-RB / Robust-RB / Hero-RB / Balanced / Undeclared

These feed the survival model and the on-the-clock LLM so reasoning is
opponent-aware.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import DraftState, LeagueSettings, Player
from .slots import eligible_positions, is_flex_slot

RUN_WINDOW = 5            # picks to look back when detecting a positional run
REACH_THRESHOLD = 8.0     # avg ADP-overall delta to be called a reacher/value
RB_POSITIONS = {"RB"}


@dataclass
class TeamProfile:
    team_id: int
    num_picks: int = 0
    position_counts: dict[str, int] = field(default_factory=dict)
    unfilled_starter_slots: dict[str, int] = field(default_factory=dict)
    needed_positions: set[str] = field(default_factory=set)
    adp_deviation: float = 0.0          # >0 reaches, <0 waits for value
    run_participation: float = 0.0      # fraction of picks made into a run
    stacks: list[str] = field(default_factory=list)
    bye_conflicts: list[str] = field(default_factory=list)
    archetype: str = "Undeclared"


def compute_needs(roster: list[Player], league: LeagueSettings):
    """Greedily assign rostered players to starting slots; return
    (unfilled_slots, needed_positions). Most-restrictive slots fill first."""
    remaining = dict(league.starter_slots)
    # Sort slots so single-position slots are filled before flex slots.
    slot_order = sorted(remaining, key=lambda s: len(eligible_positions(s)))

    # Try to seat each player in the tightest eligible open slot.
    players = sorted(roster, key=lambda p: p.proj_points, reverse=True)
    for p in players:
        for slot in slot_order:
            if remaining.get(slot, 0) > 0 and p.position in eligible_positions(slot):
                remaining[slot] -= 1
                break

    unfilled = {s: c for s, c in remaining.items() if c > 0}
    needed: set[str] = set()
    for slot in unfilled:
        needed |= eligible_positions(slot)
    return unfilled, needed


def _detect_archetype(roster_in_order: list[Player]) -> str:
    if len(roster_in_order) < 2:
        return "Undeclared"
    first3 = roster_in_order[:3]
    first4 = roster_in_order[:4]
    rb_first3 = sum(1 for p in first3 if p.position in RB_POSITIONS)
    rb_first4 = sum(1 for p in first4 if p.position in RB_POSITIONS)
    if rb_first4 == 0:
        return "Zero-RB"
    if rb_first3 >= 2:
        return "Robust-RB"
    if rb_first3 == 1 and roster_in_order[0].position in RB_POSITIONS:
        return "Hero-RB"
    return "Balanced/BPA"


def _detect_stacks(roster: list[Player]) -> list[str]:
    qbs = [p for p in roster if p.position == "QB" and p.team]
    pass_catchers = [p for p in roster if p.position in ("WR", "TE") and p.team]
    stacks = []
    for qb in qbs:
        mates = [pc.name for pc in pass_catchers if pc.team == qb.team]
        if mates:
            stacks.append(f"{qb.name} + {', '.join(mates)}")
    return stacks


def _detect_bye_conflicts(roster: list[Player]) -> list[str]:
    by_bye: dict[int, list[Player]] = {}
    for p in roster:
        if p.bye_week:
            by_bye.setdefault(p.bye_week, []).append(p)
    conflicts = []
    for bye, players in by_bye.items():
        by_pos: dict[str, list[str]] = {}
        for p in players:
            by_pos.setdefault(p.position, []).append(p.name)
        for pos, names in by_pos.items():
            if len(names) >= 2:
                conflicts.append(f"wk{bye}: {pos} {', '.join(names)}")
    return conflicts


def profile_team(state: DraftState, team_id: int) -> TeamProfile:
    league = state.league
    team_picks = [p for p in state.picks if p.team_id == team_id]
    roster = [state.players_by_id[p.player_id] for p in team_picks
              if p.player_id in state.players_by_id]

    prof = TeamProfile(team_id=team_id, num_picks=len(team_picks))
    for p in roster:
        prof.position_counts[p.position] = prof.position_counts.get(p.position, 0) + 1

    prof.unfilled_starter_slots, prof.needed_positions = compute_needs(roster, league)

    # ADP deviation: mean(adp - overall). Positive => took players earlier than
    # the field (reacher); negative => let value fall to them.
    deltas = []
    for pick in team_picks:
        pl = state.players_by_id.get(pick.player_id)
        if pl and pl.adp < 900:
            deltas.append(pl.adp - pick.overall)
    prof.adp_deviation = round(sum(deltas) / len(deltas), 1) if deltas else 0.0

    # Run participation: fraction of this team's picks whose position matched a
    # position taken in the prior RUN_WINDOW league picks.
    if team_picks:
        followed = 0
        for pick in team_picks:
            window = [pp for pp in state.picks
                      if pick.overall - RUN_WINDOW <= pp.overall < pick.overall]
            window_pos = {state.players_by_id[w.player_id].position
                          for w in window if w.player_id in state.players_by_id}
            pl = state.players_by_id.get(pick.player_id)
            if pl and pl.position in window_pos:
                followed += 1
        prof.run_participation = round(followed / len(team_picks), 2)

    prof.stacks = _detect_stacks(roster)
    prof.bye_conflicts = _detect_bye_conflicts(roster)
    prof.archetype = _detect_archetype(roster)
    return prof


def profile_all(state: DraftState) -> dict[int, TeamProfile]:
    return {tid: profile_team(state, tid) for tid in state.draft_order}


def recent_position_counts(state: DraftState, window: int) -> dict[str, int]:
    """Positions taken in the last `window` picks across the whole league.
    The basis for detecting an active positional run."""
    recent = sorted(state.picks, key=lambda p: p.overall)[-window:]
    counts: dict[str, int] = {}
    for pk in recent:
        pl = state.players_by_id.get(pk.player_id)
        if pl:
            counts[pl.position] = counts.get(pl.position, 0) + 1
    return counts


def active_runs(state: DraftState, window: int = 6, threshold: int = 3
                ) -> dict[str, int]:
    """Positions experiencing a run right now: {pos: count} where count of that
    position in the last `window` picks is >= `threshold`."""
    counts = recent_position_counts(state, window)
    return {pos: c for pos, c in counts.items() if c >= threshold}


def tendency_label(prof: TeamProfile) -> str:
    if prof.adp_deviation > REACH_THRESHOLD:
        return "reacher"
    if prof.adp_deviation < -REACH_THRESHOLD:
        return "value-waiter"
    return "ADP-aligned"
