"""Practice draft: a full simulated draft against the real board.

11 bot opponents (assigned archetypes) draft by need + ADP with some reaching,
so opponent profiles develop realistically. At each of YOUR turns the script
prints the recommendation exactly as the overlay would show it. Your pick is
auto-made as the engine's primary recommendation (no-LLM mode).

Usage (from backend/, after `python -m app.cli refresh`):
    python practice_draft.py --slot 5 --scoring ppr --teams 12
"""

from __future__ import annotations

import argparse
import random

from app import board, db, ingest
from app.data import espn
from app.engine import tiers, vorp
from app.engine.draftflow import intervening_team_ids, team_id_for_overall
from app.engine.models import DraftState, Pick
from app.engine.profiler import (active_runs, compute_needs, profile_all,
                                  tendency_label)
from app.engine.recommend import RUN_THRESHOLD, RUN_WINDOW, build_recommendation

SCORING_REC = {"ppr": 1.0, "half_ppr": 0.5, "standard": 0.0}
DETAIL_ROUNDS = 8           # show the full overlay for my first N picks
POS_CAPS = {"QB": 2, "RB": 6, "WR": 7, "TE": 2, "K": 1, "DEF": 1}


def setup_league(slot: int, scoring: str, teams: int, superflex: bool):
    conn = db.connect()
    db.init_db(conn)
    slots = {"0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "16": 1, "17": 1, "20": 6}
    if superflex:
        slots["7"] = 1  # OP / superflex
    payload = {
        "id": 777001, "seasonId": 2026,
        "settings": {
            "name": "Practice League", "size": teams,
            "scoringSettings": {"scoringItems": [
                {"statId": 53, "points": SCORING_REC[scoring]}]},
            "rosterSettings": {"lineupSlotCounts": slots},
            "draftSettings": {"type": "SNAKE",
                              "pickOrder": list(range(1, teams + 1))},
        },
        "members": [],
        "teams": [{"id": i, "name": f"Team {i}", "abbrev": f"T{i}"}
                  for i in range(1, teams + 1)],
    }
    cfg = espn.parse_league_config(payload, my_team_id=slot)
    cfg["raw"] = {}
    ingest.save_league_config(conn, cfg)
    conn.execute("DELETE FROM picks WHERE league_id='777001'")
    conn.commit()
    return conn


def assign_archetypes(team_ids, rng):
    pool = (["Robust-RB"] * 3 + ["Zero-RB"] * 2 + ["Hero-RB"] * 3
            + ["Balanced"] * 4)
    rng.shuffle(pool)
    return {tid: pool[i % len(pool)] for i, tid in enumerate(team_ids)}


def bot_pick(state: DraftState, team_id: int, rnd: int, archetype: str,
             available, rng) -> str:
    counts: dict[str, int] = {}
    for p in state.roster(team_id):
        counts[p.position] = counts.get(p.position, 0) + 1
    needs = compute_needs(state.roster(team_id), state.league)[1]

    def allowed(p):
        if p.position in ("K", "DEF") and rnd < 13:
            return False
        if counts.get(p.position, 0) >= POS_CAPS.get(p.position, 8):
            return False
        return True

    pool = [p for p in available if allowed(p)]
    if not pool:
        pool = [p for p in available if p.position not in ("K", "DEF") or rnd >= 13]
    pool.sort(key=lambda p: (p.vorp if p.vorp is not None else -1e9), reverse=True)

    # Archetype steering in the early rounds shapes a recognizable profile.
    def prefer(positions):
        cand = [p for p in pool if p.position in positions]
        return cand or pool

    if rnd <= 3:
        if archetype == "Robust-RB":
            pool = prefer({"RB"}) if rnd <= 2 else prefer({"RB", "WR"})
        elif archetype == "Zero-RB":
            pool = prefer({"WR", "TE"})
        elif archetype == "Hero-RB":
            pool = prefer({"RB"}) if rnd == 1 else prefer({"WR", "TE"})
        else:
            pool = prefer({"RB", "WR"})
    elif rnd <= 8:
        # Fill remaining starting needs first, else best available.
        needy = [p for p in pool if p.position in needs]
        pool = needy or pool

    # Reach noise: usually take the top, sometimes reach 1-3 spots for variety.
    idx = 0
    r = rng.random()
    if r < 0.18 and len(pool) > 3:
        idx = rng.randint(1, 3)
    elif r > 0.92 and len(pool) > 1:
        idx = 0  # value-waiter takes the obvious best (creates negative deviation)
    return pool[idx].player_id


def overlay(state: DraftState, rec, profiles, full: bool):
    rnd = (rec.current_overall - 1) // state.num_teams + 1
    pir = (rec.current_overall - 1) % state.num_teams + 1
    print("\n" + "=" * 70)
    print(f"  ROUND {rnd}, PICK {pir}  (overall {rec.current_overall})  "
          f"-- YOU ARE ON THE CLOCK")
    print(f"  Your next pick: overall {rec.my_next_overall} "
          f"({rec.picks_until_next} picks away)")
    print("=" * 70)

    runs = active_runs(state, window=RUN_WINDOW, threshold=RUN_THRESHOLD)
    if runs:
        print("  ACTIVE RUNS: " + ", ".join(
            f"{pos} ({c} of last {RUN_WINDOW})" for pos, c in runs.items()))

    inter = intervening_team_ids(state)
    if inter:
        seen, order = {}, []
        for tid in inter:
            if tid not in seen:
                order.append(tid)
            seen[tid] = seen.get(tid, 0) + 1
        print("  Teams before your next pick (archetype | needs | picks left before you):")
        for tid in order:
            pr = profiles[tid]
            needs = ", ".join(f"{s}x{c}" for s, c in pr.unfilled_starter_slots.items()) or "full"
            tag = f" {tendency_label(pr)}" if tendency_label(pr) != "ADP-aligned" else ""
            print(f"    T{tid:<2} [{pr.archetype}{tag}] x{seen[tid]} | {needs}")

    print(f"\n  {'PLAYER':<22} {'POS':<4} {'TM':<4} {'BYE':>3} {'VORP':>5} "
          f"{'TIER':>4} {'LEFT':>4} {'DROP':>5} {'P(back)':>7} {'RUN':>4}")
    for c in rec.shortlist:
        print(f"  {c.player.name[:22]:<22} {c.player.position:<4} "
              f"{(c.player.team or '-'):<4} {str(c.player.bye_week or '-'):>3} "
              f"{c.vorp:>5.0f} {('T'+str(c.tier)):>4} {c.players_left_in_tier:>4} "
              f"{c.tier_dropoff:>5.0f} {c.p_available_next:>6.0%} "
              f"{('yes' if c.run_active else '-'):>4}")
    print(f"\n  >> RECOMMENDATION: {rec.primary.player.name} "
          f"({rec.primary.player.position})")
    print(f"     {rec.rationale}")


def condensed(state: DraftState, rec):
    rnd = (rec.current_overall - 1) // state.num_teams + 1
    pir = (rec.current_overall - 1) % state.num_teams + 1
    p = rec.primary.player
    print(f"  R{rnd:>2}.{pir:<2} (ovr {rec.current_overall:>3})  -> "
          f"{p.name} ({p.position}, VORP {rec.primary.vorp:.0f}, "
          f"T{rec.primary.tier}, P(back) {rec.primary.p_available_next:.0%})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=5)
    ap.add_argument("--scoring", default="ppr", choices=list(SCORING_REC))
    ap.add_argument("--teams", type=int, default=12)
    ap.add_argument("--superflex", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    conn = setup_league(args.slot, args.scoring, args.teams, args.superflex)

    league, src, extras = board.load_league_settings(conn)
    players = board.load_engine_players(conn)
    players_by_id = {p.player_id: p for p in players}
    draft_order = extras["pick_order"]
    my_team = args.slot
    rounds = league.roster_size

    bot_ids = [t for t in draft_order if t != my_team]
    archetypes = assign_archetypes(bot_ids, rng)

    print(f"PRACTICE DRAFT  |  {league.num_teams}-team {league.scoring_type}"
          f"{' superflex' if league.is_superflex else ' 1-QB'}  |  "
          f"you are team {my_team} (slot {my_team})")
    print("Opponent archetypes:",
          ", ".join(f"T{t}:{archetypes[t]}" for t in bot_ids))

    picks: list[Pick] = []

    def state_now():
        return DraftState(league=league, draft_order=draft_order, picks=list(picks),
                          my_team_id=my_team, players_by_id=players_by_id)

    my_pick_no = 0
    for overall in range(1, rounds * league.num_teams + 1):
        tid = team_id_for_overall(state_now(), overall)
        rnd = (overall - 1) // league.num_teams + 1

        # Keep VORP/tiers fresh for both bots and the recommendation.
        vorp.assign_vorp(players, league)
        tiers.assign_tiers(players)
        drafted = {p.player_id for p in picks}
        available = [p for p in players_by_id.values() if p.player_id not in drafted]

        if tid == my_team:
            my_pick_no += 1
            rec = build_recommendation(state_now())
            if rnd <= DETAIL_ROUNDS:
                overlay(state_now(), rec, profile_all(state_now()), full=True)
                input_note = rec.primary.player.name
            else:
                condensed(state_now(), rec)
                input_note = rec.primary.player.name
            picks.append(Pick(overall=overall, team_id=tid,
                              player_id=rec.primary.player.player_id))
        else:
            pid = bot_pick(state_now(), tid, rnd, archetypes[tid], available, rng)
            picks.append(Pick(overall=overall, team_id=tid, player_id=pid))

    # Final summary.
    final = state_now()
    print("\n" + "#" * 70)
    print("  DRAFT COMPLETE -- YOUR ROSTER")
    print("#" * 70)
    mine = final.roster(my_team)
    byes: dict[int, list[str]] = {}
    for p in sorted(mine, key=lambda x: ("QB RB WR TE K DEF".split().index(x.position), -(x.vorp or 0))):
        print(f"  {p.position:<4} {p.name:<24} {p.team or '-':<4} bye {p.bye_week or '-'}")
        if p.bye_week:
            byes.setdefault(p.bye_week, []).append(f"{p.name}({p.position})")
    conflicts = {w: ns for w, ns in byes.items() if len(ns) >= 3}
    if conflicts:
        print("\n  Bye-week stacking (3+ players off in the same week):")
        for w, ns in sorted(conflicts.items()):
            print(f"    wk{w}: {', '.join(ns)}")

    print("\n  FINAL OPPONENT PROFILES")
    profs = profile_all(final)
    for tid in draft_order:
        if tid == my_team:
            continue
        pr = profs[tid]
        pos = ", ".join(f"{k}{v}" for k, v in sorted(pr.position_counts.items()))
        print(f"    T{tid:<2} [{pr.archetype:<10}] {tendency_label(pr):<12} "
              f"ADPdev {pr.adp_deviation:+.0f}  | {pos}")
    conn.close()


if __name__ == "__main__":
    main()
