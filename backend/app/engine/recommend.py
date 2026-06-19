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
# Raw value (VORP) is the backbone; the urgency term is a bounded adjustment so
# a clearly better player is never passed over for a marginally scarcer one.
URGENCY_WEIGHT = 0.8       # how hard to weight value-at-risk vs raw value
RUN_SCARCITY_BOOST = 0.5   # an active run amplifies that position's urgency
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
    """Pure scoring function (unit-tested). Value-first by design:

      value    = VORP — the backbone, so the best player available leads.
      urgency  = the value you actually lose by WAITING, not the player's whole
                 worth: P(he's gone) x the cliff to your fallback at his
                 position, amplified when his tier is nearly empty. This is what
                 makes "take the scarce guy now" fire only when the drop is real
                 AND he likely won't return — never just because everyone needs
                 the position.
      run_extra = amplifies urgency + a flat nudge when his position is running.

    Then weighted by roster fit (luxury/depth discounted). Because urgency is
    capped by the actual tier dropoff (not VORP), a meaningfully higher-value
    player is not leapfrogged by a lower-value one on survival alone.
    """
    v = vorp
    # The cliff to your realistic fallback, weighted up as the tier empties.
    cliff = max(dropoff, 0.0) * (0.5 + 0.5 * _scarcity_factor(tier_remaining))
    urgency = (1.0 - p_available) * cliff
    run_extra = (RUN_SCARCITY_BOOST * urgency + RUN_FLAT_BONUS * max(v, 0.0)) \
        if run_active else 0.0
    base = v + URGENCY_WEIGHT * urgency + run_extra
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
    is_upgrade: bool = False    # would improve my starting lineup at his pos
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


def _starting_slots_for(position: str, league: LeagueSettings) -> int:
    """How many dedicated starting slots my lineup has for this position
    (FLEX excluded — it's handled by compute_needs' open-slot logic)."""
    return int(league.starter_slots.get(position, 0))


def _roster_fit(player: Player, my_unfilled: dict[str, int],
                my_needed: set[str], my_counts: dict[str, int],
                league: LeagueSettings, current_overall: int,
                my_pos_vorps: dict[str, list[float]]) -> float:
    """How much this player helps MY roster, in (0, 1]. 1.0 = fills an open
    starting slot. Once a position's slots are filled, value depends on the
    SKILL already there: a clear upgrade over my current worst starter keeps
    most of its value, while a backup behind strong starters is discounted.
    K/DEF are deferred to the late rounds so they never crowd out starters."""
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

    # Slots nominally filled: weigh him against the skill I already have there.
    depth = my_counts.get(pos, 0)
    base = _DEPTH_VALUE.get(pos, 0.4) * (0.65 ** max(0, depth - 2))
    cand_v = player.vorp or 0.0
    starters = sorted(my_pos_vorps.get(pos, []), reverse=True)
    n_start = _starting_slots_for(pos, league)
    if starters and n_start >= 1:
        worst_starter = starters[min(n_start, len(starters)) - 1]
        if cand_v > worst_starter:
            # Genuine upgrade — he'd bump my weakest starter to the bench.
            # Scale toward a full-value pick by the size of the improvement.
            boost = min(1.0, (cand_v - worst_starter) / 25.0)
            return base + (1.0 - base) * boost
    return base


def _is_upgrade(player: Player, my_needed: set[str], my_counts: dict[str, int],
                league: LeagueSettings, my_pos_vorps: dict[str, list[float]]) -> bool:
    """True if he'd improve my STARTING lineup at his position (fills an open
    slot, or out-values my current worst starter there)."""
    pos = player.position
    if pos in ("K", "DEF"):
        return False
    if pos in my_needed:
        return True
    starters = sorted(my_pos_vorps.get(pos, []), reverse=True)
    n_start = _starting_slots_for(pos, league)
    if not starters or n_start < 1:
        return False
    worst_starter = starters[min(n_start, len(starters)) - 1]
    return (player.vorp or 0.0) > worst_starter


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
    # Skill already on my roster, per position: the VORPs of the players I hold
    # there (best first). Lets the fit weigh an upgrade vs mere depth.
    my_pos_vorps: dict[str, list[float]] = {}
    for p in my_roster:
        my_counts[p.position] = my_counts.get(p.position, 0) + 1
        my_pos_vorps.setdefault(p.position, []).append(p.vorp or 0.0)

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
                          state.current_overall, my_pos_vorps)
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
            is_upgrade=_is_upgrade(p, my_needed, my_counts, state.league,
                                   my_pos_vorps),
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
    """Plain-language case for the pick: value first, then fit, then timing.
    Built so the reasoning reads like a GM weighing the best player against
    roster need and what's likely to happen before the next pick — not a survival
    readout."""
    p = primary.player
    n_int = len(intervening)
    need_n = primary.needed_by_intervening
    pct = round(primary.p_available_next * 100)
    tag = f"{p.position}-{p.team}" if p.team else p.position
    is_best_value = all(primary.vorp >= c.vorp for c in shortlist)
    parts = [f"Take {p.name} ({tag})."]

    # 1) Value + roster fit — the backbone of the call, weighing what I already
    #    have at the position (open slot vs upgrade vs depth).
    if primary.fills_need:
        lead = (f"Best value on the board and fills a starting {p.position} you "
                f"still need" if is_best_value
                else f"Fills a starting {p.position} you still need")
    elif primary.is_upgrade:
        lead = ("Best value left and an upgrade" if is_best_value
                else "An upgrade")
        lead += (f" at {p.position} — better than your current starter, who "
                 f"slides to depth")
    else:
        lead = ("Best value left on the board" if is_best_value
                else "Strong value here")
        lead += f"; your {p.position} starters are set, so this is depth/upside"
    parts.append(f"{lead} (VORP {primary.vorp:.0f}).")

    # 2) Timing — will he come back to you, given who picks in between?
    if n_int == 0:
        parts.append("You pick back-to-back at the turn, so take the higher-value "
                     "player here and grab the next on the way back.")
    elif primary.p_available_next < 0.45:
        if need_n and need_n >= max(2, n_int - 1):
            thin = f"; nearly every team ahead of you is thin at {p.position}"
        elif need_n:
            thin = (f"; {need_n} of the {n_int} teams ahead of you need "
                    f"{p.position}")
        else:
            thin = ""
        parts.append(f"He won't make it back to your next pick (~{pct}% to "
                     f"survive the {n_int} picks until you're up again{thin}), "
                     f"so lock him in now.")
    elif primary.p_available_next > 0.75:
        parts.append(f"He'd likely still be here at your next pick (~{pct}%), but "
                     f"he's the best value on the board, so there's no edge in "
                     f"waiting.")
    else:
        parts.append(f"Roughly a coin flip to return (~{pct}% over the next "
                     f"{n_int} picks) — the value justifies taking him now.")

    # 3) Scarcity / run — only when the drop is real.
    if primary.run_active:
        parts.append(f"There's a {p.position} run on ({primary.run_count} of the "
                     f"last {RUN_WINDOW} picks), thinning the tier fast.")
    elif primary.players_left_in_tier <= 2 and primary.tier_dropoff > 0:
        parts.append(f"Only {primary.players_left_in_tier} left at {p.position} "
                     f"before a {primary.tier_dropoff:.0f}-pt drop, so the value "
                     f"won't hold.")

    # 4) Point at a strong alternative that can safely wait (preferably a deeper
    #    position), so the user sees the trade-off, not just the pick.
    safe = next((c for c in shortlist[1:]
                 if c.p_available_next > 0.7 and c.player.position != p.position),
                None)
    if safe:
        parts.append(f"{safe.player.name} ({safe.player.position}) should still "
                     f"be there next time, so he can wait.")
    return " ".join(parts)
