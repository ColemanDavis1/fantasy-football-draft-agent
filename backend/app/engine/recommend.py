"""Tie the engine together into an on-the-clock recommendation.

Produces the deterministic shortlist + every computed signal (VORP, tier,
players-left-in-tier, P(available next pick)) and a templated, opponent-aware
rationale. This is exactly what the Phase 3 LLM call reasons over — and what
'no-LLM mode' surfaces verbatim, so the tool is fully usable at $0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import tiers, vorp
from .draftflow import intervening_team_ids, next_pick_for_team
from .models import DraftState, Player
from .profiler import TeamProfile, profile_all, tendency_label
from .survival import survival_probability

# How much the cost of waiting (value lost if he's gone) boosts urgency.
URGENCY_WEIGHT = 1.0
# Extra urgency when a player is the last of a strong value tier (a 'cliff').
TIER_CLIFF_BONUS = 0.15


@dataclass
class Candidate:
    player: Player
    vorp: float
    tier: int | None
    players_left_in_tier: int
    p_available_next: float
    pick_score: float
    needed_by_intervening: int  # how many intervening teams still need his pos


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


def build_recommendation(state: DraftState, top_n: int = 5,
                         shortlist_pool: int = 12) -> Recommendation:
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

    # 3. Score the top of the board.
    pool = available[:shortlist_pool]
    candidates: list[Candidate] = []
    for p in pool:
        p_avail = survival_probability(p, state, available, profiles, intervening)
        left = tiers.players_left_in_tier(available, p)
        urgency = 1.0 - p_avail
        # Last-of-tier scarcity: full bonus at <=2 left, half at <=4, none beyond.
        cliff = 1.0 if left <= 2 else (0.5 if left <= 4 else 0.0)
        v = p.vorp or 0.0
        score = v * (1.0 + URGENCY_WEIGHT * urgency) + TIER_CLIFF_BONUS * cliff * max(v, 0.0)
        candidates.append(Candidate(
            player=p,
            vorp=p.vorp or 0.0,
            tier=p.tier,
            players_left_in_tier=left,
            p_available_next=p_avail,
            pick_score=round(score, 2),
            needed_by_intervening=_needed_by_count(p.position, intervening, profiles),
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

    if primary.players_left_in_tier <= 2:
        parts.append(
            f"Tier cliff: only {primary.players_left_in_tier} left in {p.position} "
            f"tier {primary.tier}, so drafting now avoids the drop-off."
        )

    # Contrast with the safest high-value alternative that WILL likely return.
    safe = next((c for c in shortlist[1:] if c.p_available_next > 0.7), None)
    if safe:
        parts.append(
            f"{safe.player.name} ({safe.player.position}) projects to return "
            f"(~{round(safe.p_available_next*100)}%), so he can wait."
        )
    return " ".join(parts)
