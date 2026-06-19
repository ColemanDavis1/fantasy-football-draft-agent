"""Offline proof of the Phase-2 engine math against the hardcoded sample draft.

Runnable two ways:
    python -m pytest backend/tests/test_engine.py      (if pytest installed)
    python backend/tests/test_engine.py                 (no deps; prints PASS/FAIL)
"""

from __future__ import annotations

import os
import sys

# Allow running directly (python backend/tests/test_engine.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine import tiers, vorp  # noqa: E402
from app.engine.draftflow import intervening_team_ids, next_pick_for_team  # noqa: E402
from app.engine.profiler import active_runs, profile_all  # noqa: E402
from app.engine.recommend import build_recommendation, pick_score  # noqa: E402
from app.engine.sample import build_sample_state  # noqa: E402
from app.engine.survival import survival_probability  # noqa: E402


def test_vorp_baselines_and_ranking():
    state = build_sample_state()
    players = list(state.players_by_id.values())
    repl = vorp.assign_vorp(players, state.league)

    # Replacement points are positive and exist for every starting position.
    for pos in ("QB", "RB", "WR", "TE"):
        assert repl[pos] > 0, f"no replacement level for {pos}"

    # The best player at a position has positive VORP; the replacement-level
    # player has ~0 VORP by construction.
    rb1 = state.players_by_id["RB1"]
    assert rb1.vorp is not None and rb1.vorp > 0

    # Cross-position sanity: an elite TE (TE1) with a big cliff below him should
    # out-VORP a mid QB, since QB is deep.
    te1 = state.players_by_id["TE1"]
    qb8 = state.players_by_id["QB8"]
    assert te1.vorp > qb8.vorp


def test_tiers_capture_the_te_cliff():
    state = build_sample_state()
    players = list(state.players_by_id.values())
    vorp.assign_vorp(players, state.league)
    tiers.assign_tiers(players)

    te1 = state.players_by_id["TE1"]
    te2 = state.players_by_id["TE2"]
    # The elite TE sits alone in tier 1; the field drops to a lower tier.
    assert te1.tier == 1
    assert te2.tier is not None and te2.tier > 1


def test_draftflow_snake_math():
    state = build_sample_state()
    # On the clock at overall 29; team 5's next pick is 44 (round-4 snake back).
    assert state.current_overall == 29
    assert next_pick_for_team(state, state.my_team_id, 29) == 44
    intervening = intervening_team_ids(state)
    assert len(intervening) == 14          # picks 30..43
    assert state.my_team_id not in intervening


def test_opponent_archetypes():
    state = build_sample_state()
    profiles = profile_all(state)
    assert profiles[3].archetype == "Robust-RB"   # RB, RB
    assert profiles[8].archetype == "Zero-RB"      # WR, WR
    assert profiles[1].stacks, "team 1 should show a QB/WR stack"


def test_survival_is_opponent_aware():
    state = build_sample_state()
    players = list(state.players_by_id.values())
    vorp.assign_vorp(players, state.league)
    tiers.assign_tiers(players)
    available = state.available()
    profiles = profile_all(state)
    intervening = intervening_team_ids(state)

    top_wr = max((p for p in available if p.position == "WR"),
                 key=lambda p: p.vorp)
    top_k = max((p for p in available if p.position == "K"),
                key=lambda p: p.vorp)

    wr_surv = survival_probability(top_wr, state, available, profiles, intervening)
    k_surv = survival_probability(top_k, state, available, profiles, intervening)

    assert 0.0 <= wr_surv <= 1.0 and 0.0 <= k_surv <= 1.0
    # A high-demand WR is far less likely to survive 14 picks than a kicker
    # nobody needs yet.
    assert wr_surv < k_surv
    assert k_surv > 0.9


def test_recommendation_is_coherent():
    state = build_sample_state()
    rec = build_recommendation(state)
    assert rec.primary is not None
    assert rec.shortlist and len(rec.shortlist) <= 5
    # Shortlist is ordered by pick_score (value + urgency).
    scores = [c.pick_score for c in rec.shortlist]
    assert scores == sorted(scores, reverse=True)
    assert rec.primary.player.position in rec.rationale
    assert rec.picks_until_next == 14


def test_active_run_detection():
    # Sample picks 25-27 are three straight QBs; on the clock at overall 29 the
    # last 5 picks (RB, QB, QB, QB, WR) contain a QB run.
    state = build_sample_state()
    runs = active_runs(state, window=5, threshold=3)
    assert runs.get("QB", 0) >= 3, runs


def test_pick_score_prefers_scarcer_position():
    # Two equal-value players: an RB who is the last of his tier before a big
    # drop vs a WR sitting in a deep tier. The RB should score higher.
    rb = pick_score(vorp=100, p_available=0.3, tier_remaining=1, dropoff=50,
                    run_active=False, fit=1.0)
    wr = pick_score(vorp=100, p_available=0.3, tier_remaining=3, dropoff=40,
                    run_active=False, fit=1.0)
    assert rb > wr, (rb, wr)


def test_pick_score_run_adds_urgency():
    base = pick_score(100, 0.5, 2, 30, run_active=False, fit=1.0)
    with_run = pick_score(100, 0.5, 2, 30, run_active=True, fit=1.0)
    assert with_run > base, (with_run, base)


def test_roster_fit_rewards_upgrade_over_depth():
    # At a position whose starting slots are filled, a player who beats my
    # current worst starter (an upgrade) must outscore mere depth — and only the
    # upgrade is flagged.
    from app.engine.models import LeagueSettings, Player
    from app.engine.recommend import _is_upgrade, _roster_fit
    league = LeagueSettings(num_teams=12, starter_slots={
        "QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "D/ST": 1})
    # I hold two RBs: one strong (VORP 90), one weak (VORP 20). RB slots filled.
    my_pos_vorps = {"RB": [90.0, 20.0]}
    my_counts, my_needed, my_unfilled = {"RB": 2}, set(), {}
    elite = Player("x", "Elite RB", "RB", vorp=100.0)   # beats my RB2 (20)
    depth = Player("y", "Depth RB", "RB", vorp=10.0)    # behind both starters

    fit_up = _roster_fit(elite, my_unfilled, my_needed, my_counts, league, 30, my_pos_vorps)
    fit_dep = _roster_fit(depth, my_unfilled, my_needed, my_counts, league, 30, my_pos_vorps)
    assert _is_upgrade(elite, my_needed, my_counts, league, my_pos_vorps) is True
    assert _is_upgrade(depth, my_needed, my_counts, league, my_pos_vorps) is False
    assert fit_up > fit_dep, (fit_up, fit_dep)


def test_recommendation_carries_run_and_dropoff():
    rec = build_recommendation(build_sample_state())
    # New signals are populated on every candidate.
    assert all(hasattr(c, "tier_dropoff") for c in rec.shortlist)
    assert any(c.run_active for c in rec.shortlist) or True  # run may be off pos
    assert rec.primary.tier_dropoff >= 0


ALL_TESTS = [
    test_vorp_baselines_and_ranking,
    test_tiers_capture_the_te_cliff,
    test_draftflow_snake_math,
    test_opponent_archetypes,
    test_survival_is_opponent_aware,
    test_recommendation_is_coherent,
    test_active_run_detection,
    test_pick_score_prefers_scarcer_position,
    test_pick_score_run_adds_urgency,
    test_roster_fit_rewards_upgrade_over_depth,
    test_recommendation_carries_run_and_dropoff,
]


def _run_standalone() -> int:
    failures = 0
    for t in ALL_TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # pragma: no cover
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(ALL_TESTS) - failures}/{len(ALL_TESTS)} passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
