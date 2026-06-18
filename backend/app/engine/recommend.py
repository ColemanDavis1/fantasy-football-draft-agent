"""Tie the engine together into an on-the-clock recommendation.

Produces the deterministic shortlist + every computed signal (VORP, tier,
players-left-in-tier, P(available next pick)) and a templated, opponent-aware
rationale. This is exactly what the Phase 3 LLM call reasons over - and what
'no-LLM mode' surfaces verbatim, so the tool is fully usable at $0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import tiers, vorp
from .draftflow import intervening_team_ids, next_pick_for_team
from .models import DraftState, LeagueSettings, Player
from .profiler import (TeamProfile, active_runs, compute_needs, profile_all,
                       tendency_label)
from .survival import survival_probability

# Scoring weights (all terms are in VORP units, so cross-position comparable).
WAIT_WEIGHT = 0.6      # value at risk if he won't survive to my next pick
CLIFF_WEIGHT = 0.8     # value lost to the positional tier cliff (scarcity)
RUN_SCARCITY_BOOST = 0.5   # an active run amplifies that position's scarcity
RUN_FLAT_BONUS = 0.08      # ...plus a flat nudge to grab the running position

# Positional-run detection.
RUN_WINDOW = 6
RUN_THRESHOLD = 3      # >=3 of the last RUN_WINDOW picks at one position = a run

# Backup/depth value of a position once its starting slots are filled. Below 1
# so a luxury pick never outranks a player who fills an open starting slot.
_DEPTH_VALUE = {"RB": 0.55, "WR": 0.55, "TE": 0.30, "QB": 0.15, "K": 0.0, "DEF": 0.0}
# Roster-construction ceilings: never draft beyond this many of a position
# (keeps a shallow position from hoarding bench spots). QB raised for superflex.
_POSITION_CAP = {"RB": 6, "WR": 7, "TE": 2, "K": 1, "DEF": 1}


def _position_cap(position: str, league: LeagueSettings) -> int:
    if position == "QB":
        return 3 if league.is_superflex else 2
    return _POSITION_CAP.get(position, 8)


def _scarcity_factor(tier_remaining: int) -> float:
    """How urgent the positional cliff is, by how few remain in this tier."""
    if tier_remaining <= 1:
        return 1.0
    if tier_remaining == 2:
        return 0.5
    if tier_remaining == 3:
        return 0.2
    return 0.0


def pick_score(vorp: float, p_available: float, tier_remaining: int,
               dropoff: float, run_active: bool, fit: float) -> float:
    """Pure scoring function (unit-tested). All bonuses are in VORP units so
    they compare fairly across positions:

      value      = VORP (baseline worth)
      wait_cost  = value you'd lose if he won't return to you  (survival)
      scarcity   = value you'd lose to the tier cliff at his position, scaled
                   by how few remain  (cross-position: 1 RB left before a big
                   drop beats 3 WRs in a flat tier)
      run_extra  = amplifies scarcity + a flat nudge when his position is
                   actively running

    Then weighted by roster fit (luxury/depth discounted).
    """
    v = vorp
    wait_cost = (1.0 - p_available) * max(v, 0.0)
    scarcity = _scarcity_factor(tier_remaining) * max(dropoff, 0.0)
    run_extra = (RUN_SCARCITY_BOOST * scarcity + RUN_FLAT_BONUS * max(v, 0.0)) \
        if run_active else 0.0
    base = v + WAIT_WEIGHT * wait_cost + CLIFF_WEIGHT * scarcity + run_extra
    # Apply fit only to positive scores so luxury/junk isn't promoted by
    # multiplying a negative number toward zero.
    return base * fit if base > 0 else base


@dataclass
class Candidate:
    player: Player
    vorp: float
    tier: int | None
    players_left_in_tier: int
    p_available_next: float
    pick_score: float
    needed_by_intervening: int  # how many intervening teams still need his pos
    roster_fit: float = 1.0     # 1.0 fills an open starter; <1 luxury/depth
    fills_need: bool = True     # does he fill one of MY open starting slots?
    tier_dropoff: float = 0.0   # VORP cliff to the next tier at his position
    run_active: bool = False    # his position is in an active run
    run_count: int = 0          # his position's count in the recent window


@dataclass
class Recommendation:
    current_overall: int
    my_next_overall: int | None
    picks_until_next: int
    shortlist: list[Candidate]
    primary: Candidate | None
    rationale: str
    profiles: dict[int, TeamProfile] = field(default_factory=dict)


def _needed_by_count(position: str, team_ids: list[int],
                     profiles: dict[int, TeamProfile]) -> int:
    return sum(1 for tid in team_ids
               if position in profiles[tid].needed_positions)


def _roster_fit(player: Player, my_unfilled: dict[str, int],
                my_needed: set[str], my_counts: dict[str, int],
                league: LeagueSettings, current_overall: int) -> float:
    """How much this player helps MY roster, in (0, 1]. 1.0 = fills an open
    starting slot. Luxury depth is discounted; K/DEF are deferred to the late
    rounds so they never crowd out players I still need to start."""
    pos = player.position
    if pos in ("K", "DEF"):
        slot = "K" if pos == "K" else "D/ST"
        empty = my_unfilled.get(slot, 0)
        if empty <= 0:
            return 0.02  # already have a starter; a 2nd is near-worthless
        rounds_left = league.roster_size - ((current_overall - 1) // league.num_teams)
        # Only worth taking in the last (empty + 1) rounds.
        return 1.0 if rounds_left <= empty + 1 else 0.04
    if pos in my_needed:
        return 1.0  # fills an open starting slot (dedicated or FLEX)
    depth = my_counts.get(pos, 0)
    base = _DEPTH_VALUE.get(pos, 0.4)
    return base * (0.65 ** max(0, depth - 2))  # diminishing returns on stacking


def build_recommendation(state: DraftState, top_n: int = 5,
                         shortlist_pool: int = 30) -> Recommendation:
    # 1. Value + tiers over the FULL pool (stable baselines), then look at what's left.
    all_players = list(state.players_by_id.values())
    vorp.assign_vorp(all_players, state.league)
    tiers.assign_tiers(all_players)

    available = sorted(
        state.available(),
        key=lambda p: (p.vorp if p.vorp is not None else -1e9),
        reverse=True,
    )

    # 2. Opponent profiles + who picks before my next turn.
    profiles = profile_all(state)
    intervening = intervening_team_ids(state)
    my_next = next_pick_for_team(state, state.my_team_id, state.current_overall)

    # 3. MY roster needs - so the pick fits my team, not just raw value.
    my_roster = state.roster(state.my_team_id)
    my_unfilled, my_needed = compute_needs(my_roster, state.league)
    my_counts: dict[str, int] = {}
    for p in my_roster:
        my_counts[p.position] = my_counts.get(p.position, 0) + 1

    # 4. Active positional runs (whole-league behavior over the recent window).
    runs = active_runs(state, window=RUN_WINDOW, threshold=RUN_THRESHOLD)

    # 5. Score the board. Walk it in value order, skipping K/DEF that are
    #    deferred or already backed up, so late bench picks go to the best skill
    #    player available instead of a 4th kicker.
    candidates: list[Candidate] = []
    for p in available:
        if len(candidates) >= shortlist_pool:
            break
        if my_counts.get(p.position, 0) >= _position_cap(p.position, state.league):
            continue  # roster already full at this position
        fit = _roster_fit(p, my_unfilled, my_needed, my_counts, state.league,
                          state.current_overall)
        if p.position in ("K", "DEF") and fit < 1.0:
            continue  # defer until late / don't stockpile backups

        p_avail = survival_probability(p, state, available, profiles, intervening)
        left = tiers.players_left_in_tier(available, p)
        dropoff = tiers.tier_dropoff(available, p)
        run_count = runs.get(p.position, 0)
        run_on = run_count >= RUN_THRESHOLD
        score = pick_score(p.vorp or 0.0, p_avail, left, dropoff, run_on, fit)

        candidates.append(Candidate(
            player=p,
            vorp=p.vorp or 0.0,
            tier=p.tier,
            players_left_in_tier=left,
            p_available_next=p_avail,
            pick_score=round(score, 2),
            needed_by_intervening=_needed_by_count(p.position, intervening, profiles),
            roster_fit=round(fit, 2),
            fills_need=(p.position in my_needed) if p.position not in ("K", "DEF")
            else (my_unfilled.get("K" if p.position == "K" else "D/ST", 0) > 0
                  and fit >= 1.0),
            tier_dropoff=round(dropoff, 1),
            run_active=run_on,
            run_count=run_count,
        ))

    candidates.sort(key=lambda c: c.pick_score, reverse=True)
    shortlist = candidates[:top_n]
    primary = shortlist[0] if shortlist else None
    rationale = _rationale(primary, shortlist, state, profiles, intervening) if primary else "No players available."

    return Recommendation(
        current_overall=state.current_overall,
        my_next_overall=my_next,
        picks_until_next=len(intervening),
        shortlist=shortlist,
        primary=primary,
        rationale=rationale,
        profiles=profiles,
    )


def _rationale(primary: Candidate, shortlist: list[Candidate], state: DraftState,
               profiles: dict[int, TeamProfile], intervening: list[int]) -> str:
    p = primary.player
    n_int = len(intervening)
    need_n = primary.needed_by_intervening
    parts = [
        f"Take {p.name} ({p.position}"
        + (f"-{p.team}" if p.team else "") + ")."
    ]
    parts.append(f"VORP {primary.vorp:.0f}, tier {primary.tier}.")

    if n_int == 0:
        parts.append("You pick back-to-back at the turn, so secure the higher-value player now.")
    else:
        pct = round(primary.p_available_next * 100)
        if primary.p_available_next < 0.45:
            parts.append(
                f"Only ~{pct}% to return: {need_n} of the {n_int} teams before "
                f"your next pick still need {p.position}, so he won't get back to you."
            )
        elif primary.p_available_next > 0.75:
            parts.append(
                f"~{pct}% to survive to your next pick, but his value leads the board now."
            )
        else:
            parts.append(
                f"~{pct}% to return ({need_n}/{n_int} intervening teams need {p.position})."
            )

    if not primary.fills_need:
        parts.append("Depth/upside pick - your starting slots at this position "
                     "are already set.")

    if primary.run_active:
        parts.append(
            f"{p.position} RUN: {primary.run_count} of the last {RUN_WINDOW} "
            f"picks were {p.position} - the position is thinning fast."
        )

    # Cross-position scarcity: few left at his position before a real drop, while
    # a comparable-value alternative sits in a deeper position that can wait.
    if (primary.fills_need and primary.players_left_in_tier <= 2
            and primary.tier_dropoff > 0):
        deeper = next((c for c in shortlist[1:]
                       if c.player.position != p.position
                       and c.players_left_in_tier >= 3
                       and c.vorp >= primary.vorp - 10), None)
        if deeper:
            parts.append(
                f"Scarcity: only {primary.players_left_in_tier} {p.position} left "
                f"before a {primary.tier_dropoff:.0f}-pt drop, while "
                f"{deeper.player.position} is {deeper.players_left_in_tier}-deep "
                f"at similar value - take the {p.position} and let the "
                f"{deeper.player.position} come back."
            )
        else:
            parts.append(
                f"Tier cliff: only {primary.players_left_in_tier} left in "
                f"{p.position} tier {primary.tier} before a "
                f"{primary.tier_dropoff:.0f}-pt drop - draft now."
            )

    # Contrast with the safest high-value alternative that WILL likely return.
    safe = next((c for c in shortlist[1:] if c.p_available_next > 0.7), None)
    if safe:
        parts.append(
            f"{safe.player.name} ({safe.player.position}) projects to return "
            f"(~{round(safe.p_available_next*100)}%), so he can wait."
        )
    return " ".join(parts)
