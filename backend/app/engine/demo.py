"""Render the full on-the-clock recommendation for the sample draft state.

    python -m app.engine.demo

Shows the Phase-2 output in the spec's format: every team's roster + needs,
live tendencies, the ranked top-5 with signals, and one primary recommendation
with an opponent-aware rationale. Pure engine output, no LLM (this is exactly
what 'no-LLM mode' surfaces).
"""

from __future__ import annotations

from .profiler import tendency_label
from .recommend import build_recommendation
from .sample import build_sample_state


def _roster_str(state, team_id) -> str:
    roster = state.roster(team_id)
    return ", ".join(f"{p.name}" for p in roster) or "(empty)"


def main() -> int:
    state = build_sample_state()
    rec = build_recommendation(state)

    print("=" * 72)
    print(f"ON THE CLOCK: overall pick {rec.current_overall} "
          f"(you are team {state.my_team_id}). "
          f"Your next pick: {rec.my_next_overall} "
          f"({rec.picks_until_next} picks away).")
    print("=" * 72)

    print("\nALL ROSTERS / NEEDS / TENDENCIES")
    print("-" * 72)
    for tid in state.draft_order:
        prof = rec.profiles[tid]
        me = "  <-- YOU" if tid == state.my_team_id else ""
        needs = ", ".join(f"{s}x{c}" for s, c in prof.unfilled_starter_slots.items())
        print(f"Team {tid:>2} [{prof.archetype:<12}] {tendency_label(prof):<12} "
              f"ADPdev {prof.adp_deviation:+.0f}{me}")
        print(f"        roster: {_roster_str(state, tid)}")
        print(f"        needs:  {needs or '(starters full)'}")
        if prof.stacks:
            print(f"        stack:  {'; '.join(prof.stacks)}")
        if prof.bye_conflicts:
            print(f"        byes!:  {'; '.join(prof.bye_conflicts)}")

    print("\nTOP-5 CANDIDATES")
    print("-" * 72)
    print(f"{'PLAYER':<8} {'POS':<4} {'VORP':>6} {'TIER':>4} {'LEFT':>4} "
          f"{'P(avail)':>9} {'SCORE':>7}")
    for c in rec.shortlist:
        print(f"{c.player.name:<8} {c.player.position:<4} {c.vorp:>6.0f} "
              f"{str(c.tier):>4} {c.players_left_in_tier:>4} "
              f"{c.p_available_next:>8.0%} {c.pick_score:>7.1f}")

    print("\nRECOMMENDATION")
    print("-" * 72)
    print(rec.rationale)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
