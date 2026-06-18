"""Live draft session: the stateful glue between captured picks and the engine.

Holds the active league config + full player pool, accepts picks as they arrive
from the extension, persists them, and produces a recommendation for whoever is
on the clock. Engine work (deterministic) runs on every pick; the LLM is invoked
only when it's MY turn (see server.py), per the cost model.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import board, db, priors
from .engine import tiers, vorp
from .engine.draftflow import team_id_for_overall
from .engine.models import DraftState, Pick
from .engine.profiler import tendency_label
from .engine.recommend import Recommendation, build_recommendation
from .match import PlayerMatcher


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DraftSession:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.league, self.source, extras = board.load_league_settings(conn)
        self.league_id = extras.get("league_id")
        self.season = extras.get("season")
        self.my_team_id = extras.get("my_team_id")
        pick_order = extras.get("pick_order") or []
        self.draft_order = pick_order or list(range(1, self.league.num_teams + 1))

        players = board.load_engine_players(conn)
        self.players_by_id = {p.player_id: p for p in players}
        self.matcher = PlayerMatcher(conn)

        # Historical priors (Phase 4): seed opponents so they're not blank on
        # pick 1. Live picks refine the picture as the draft unfolds.
        self.priors = priors.load_priors(conn, self.league_id, self.season)

        # picks: overall -> Pick. Load any already persisted for this league.
        self.picks: dict[int, Pick] = {}
        self._load_persisted_picks()

    # ---- pick ingestion -------------------------------------------------
    def _load_persisted_picks(self) -> None:
        if not self.league_id:
            return
        rows = self.conn.execute(
            "SELECT overall_pick, team_id, player_id FROM picks "
            "WHERE league_id=? AND season=? ORDER BY overall_pick",
            (self.league_id, self.season),
        ).fetchall()
        for r in rows:
            self.picks[r["overall_pick"]] = Pick(
                overall=r["overall_pick"], team_id=r["team_id"],
                player_id=r["player_id"])

    def _persist_pick(self, overall: int, team_id: int, player_id: str,
                      source: str) -> None:
        if not self.league_id:
            return
        rnd = (overall - 1) // self.league.num_teams + 1
        pir = (overall - 1) % self.league.num_teams + 1
        self.conn.execute(
            """INSERT INTO picks (league_id, season, overall_pick, round,
                   pick_in_round, team_id, player_id, source, captured_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(league_id, season, overall_pick) DO UPDATE SET
                   team_id=excluded.team_id, player_id=excluded.player_id,
                   source=excluded.source, captured_at=excluded.captured_at""",
            (self.league_id, self.season, overall, rnd, pir, team_id,
             player_id, source, _now()),
        )
        self.conn.commit()

    def add_pick(self, overall: int, team_id: int, player_id: str,
                 source: str = "websocket") -> None:
        self.picks[overall] = Pick(overall=overall, team_id=team_id,
                                    player_id=player_id)
        self._persist_pick(overall, team_id, player_id, source)

    def ingest_pick(self, espn_id=None, name=None, position=None, team=None,
                    team_id=None, overall=None, source="websocket") -> dict:
        """Resolve a captured pick to a player + slot it into the draft."""
        pid, confidence = self.matcher.match(espn_id, name, position, team)
        if overall is None:
            overall = self.next_overall()
        if team_id is None:
            team_id = team_id_for_overall(self.build_state(), overall)
        if pid is None or pid not in self.players_by_id:
            return {"ok": False, "reason": "unmatched", "overall": overall,
                    "name": name, "confidence": confidence}
        self.add_pick(overall, int(team_id), pid, source)
        return {"ok": True, "overall": overall, "team_id": int(team_id),
                "player_id": pid, "player_name": self.players_by_id[pid].name,
                "confidence": confidence}

    def remove_pick(self, overall: int) -> bool:
        existed = self.picks.pop(overall, None) is not None
        if self.league_id:
            self.conn.execute(
                "DELETE FROM picks WHERE league_id=? AND season=? AND overall_pick=?",
                (self.league_id, self.season, overall))
            self.conn.commit()
        return existed

    # ---- state + recommendation ----------------------------------------
    def next_overall(self) -> int:
        return (max(self.picks) + 1) if self.picks else 1

    def build_state(self) -> DraftState:
        ordered = [self.picks[o] for o in sorted(self.picks)]
        return DraftState(
            league=self.league,
            draft_order=self.draft_order,
            picks=ordered,
            my_team_id=self.my_team_id if self.my_team_id is not None else -1,
            players_by_id=self.players_by_id,
        )

    def on_the_clock_team(self) -> int | None:
        state = self.build_state()
        try:
            return team_id_for_overall(state, state.current_overall)
        except (IndexError, ValueError):
            return None

    def is_my_turn(self) -> bool:
        return (self.my_team_id is not None
                and self.on_the_clock_team() == self.my_team_id)

    def recommend(self) -> Recommendation:
        return build_recommendation(self.build_state())

    def sync_status(self, expected_overall: int | None = None) -> dict:
        """Backstop: does our pick count match what the draft expects?"""
        actual = len(self.picks)
        # Highest overall we have minus count reveals gaps.
        highest = max(self.picks) if self.picks else 0
        missing = [o for o in range(1, highest + 1) if o not in self.picks]
        status = {"picks_recorded": actual, "highest_overall": highest,
                  "missing_overalls": missing, "in_sync": not missing}
        if expected_overall is not None:
            status["expected_overall"] = expected_overall
            status["in_sync"] = (not missing) and (highest == expected_overall - 1)
        return status

    # ---- serialization for the API/overlay -----------------------------
    def state_summary(self) -> dict:
        state = self.build_state()
        vorp.assign_vorp(list(self.players_by_id.values()), self.league)
        from .engine.profiler import profile_all
        profiles = profile_all(state)
        teams = []
        for tid in self.draft_order:
            prof = profiles[tid]
            roster = [{"name": p.name, "pos": p.position, "team": p.team,
                       "bye": p.bye_week} for p in state.roster(tid)]
            prior = self.priors.get(tid)
            teams.append({
                "team_id": tid,
                "is_me": tid == self.my_team_id,
                "archetype": prof.archetype,
                "tendency": tendency_label(prof),
                "adp_deviation": prof.adp_deviation,
                "needs": prof.unfilled_starter_slots,
                "stacks": prof.stacks,
                "bye_conflicts": prof.bye_conflicts,
                # Historical prior (None if no past drafts seeded). Most useful
                # early, before live picks reveal this year's archetype.
                "prior_archetype": prior.get("archetype") if prior else None,
                "prior": prior,
                "roster": roster,
            })
        return {
            "league_source": self.source,
            "num_teams": self.league.num_teams,
            "my_team_id": self.my_team_id,
            "current_overall": state.current_overall,
            "on_the_clock": self.on_the_clock_team(),
            "is_my_turn": self.is_my_turn(),
            "teams": teams,
        }


def recommendation_to_dict(rec: Recommendation, llm: dict | None = None) -> dict:
    return {
        "current_overall": rec.current_overall,
        "my_next_overall": rec.my_next_overall,
        "picks_until_next": rec.picks_until_next,
        "primary": _cand_to_dict(rec.primary) if rec.primary else None,
        "shortlist": [_cand_to_dict(c) for c in rec.shortlist],
        "engine_rationale": rec.rationale,
        "llm": llm,  # {"pick","rationale","model"} or None in no-LLM mode
    }


def _cand_to_dict(c) -> dict:
    return {
        "player_id": c.player.player_id,
        "name": c.player.name,
        "position": c.player.position,
        "team": c.player.team,
        "bye": c.player.bye_week,
        "vorp": c.vorp,
        "tier": c.tier,
        "players_left_in_tier": c.players_left_in_tier,
        "p_available_next": c.p_available_next,
        "pick_score": c.pick_score,
        "needed_by_intervening": c.needed_by_intervening,
        "roster_fit": c.roster_fit,
        "fills_need": c.fills_need,
        "tier_dropoff": c.tier_dropoff,
        "run_active": c.run_active,
        "run_count": c.run_count,
    }


def open_session() -> DraftSession:
    conn = db.connect()
    db.init_db(conn)
    return DraftSession(conn)
