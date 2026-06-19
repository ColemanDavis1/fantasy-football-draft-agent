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

        # Team display metadata (name/owner/draft slot) for the dashboard.
        self.team_meta = self._load_team_meta()

        # Auto-size: for a public mock (no ESPN config) the league size isn't
        # known up front, so we infer it from the picks themselves —
        # num_teams = max(pick_in_round). Real ESPN leagues are authoritative
        # (size + draft type come from mSettings), so we never override them.
        self.auto_size = self.league_id in (None, "MOCK")
        self._round1_max = 0          # largest pick_in_round seen in round 1
        self._size_finalized = False  # set once a round-2 pick proves round 1's size
        if self.auto_size:
            self._infer_size_from_persisted()

        # picks: overall -> Pick. Load any already persisted for this league.
        self.picks: dict[int, Pick] = {}
        self._load_persisted_picks()

    def _set_num_teams(self, n: int) -> None:
        """Resize the (mock) league and rebuild the draft order to 1..n."""
        self.league.num_teams = n
        self.draft_order = list(range(1, n + 1))

    def _infer_size_from_persisted(self) -> None:
        """On load, recover the inferred size from stored picks. Round 1's pick
        count is the size, but only trustworthy once round 1 is complete — which
        the presence of any round-2 pick proves."""
        if not self.league_id:
            return
        has_r2 = self.conn.execute(
            "SELECT 1 FROM picks WHERE league_id=? AND season=? AND round>=2 LIMIT 1",
            (self.league_id, self.season),
        ).fetchone()
        row = self.conn.execute(
            "SELECT MAX(pick_in_round) AS m FROM picks "
            "WHERE league_id=? AND season=? AND round=1",
            (self.league_id, self.season),
        ).fetchone()
        m = row["m"] if row else None
        if m:
            self._round1_max = int(m)
        if has_r2 and m:
            self._set_num_teams(int(m))
            self._size_finalized = True

    def _load_team_meta(self) -> dict[int, dict]:
        if not self.league_id:
            return {}
        rows = self.conn.execute(
            "SELECT team_id, name, owner, draft_slot FROM teams "
            "WHERE league_id=? AND season=?", (self.league_id, self.season),
        ).fetchall()
        return {r["team_id"]: {"name": r["name"], "owner": r["owner"],
                               "draft_slot": r["draft_slot"]} for r in rows}

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
                    team_id=None, overall=None, round=None, pick_in_round=None,
                    source="websocket") -> dict:
        """Resolve a captured pick to a player + slot it into the draft.

        Overall precedence: explicit > derived from round/pick (authoritative,
        straight off the ESPN row) > next sequential. Deriving from round/pick
        keeps picks correctly ordered even if the DOM emits them out of order."""
        pid, confidence = self.matcher.match(espn_id, name, position, team)
        # Auto-infer league size from the picks (public mocks). Round 1's pick
        # count is the team count; we finalize it the moment a round-2 pick
        # appears (proof round 1 is complete). Round-1 overalls equal
        # pick_in_round regardless of size, so nothing computed before
        # finalization is wrong.
        if self.auto_size and round and pick_in_round and not self._size_finalized:
            if round == 1:
                self._round1_max = max(self._round1_max, pick_in_round)
            elif round >= 2 and self._round1_max:
                self._set_num_teams(self._round1_max)
                self._size_finalized = True
        if overall is None and round and pick_in_round:
            overall = (round - 1) * self.league.num_teams + pick_in_round
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

    def reconcile(self, parsed: list[dict], overwrite: bool = False,
                  source: str = "bulk") -> dict:
        """Reconcile a parsed pick list against what we have. Fills missing
        picks; with overwrite=True also corrects mismatches. Teams are inferred
        from the overall via snake math (more reliable than a pasted team id).
        Returns a summary incl. names we couldn't resolve."""
        state = self.build_state()
        n = self.league.num_teams
        added = corrected = skipped = 0
        unmatched: list[str] = []
        for i, pk in enumerate(parsed):
            overall = pk.get("overall")
            if overall is None and pk.get("round") and pk.get("pick_in_round"):
                overall = (pk["round"] - 1) * n + pk["pick_in_round"]
            if overall is None:
                overall = i + 1  # bare list -> sequential
            pid, _conf = self.matcher.match(
                None, pk.get("name"), pk.get("position"), pk.get("team"))
            if pid is None or pid not in self.players_by_id:
                unmatched.append(pk.get("name"))
                continue
            try:
                team_id = team_id_for_overall(state, overall)
            except (IndexError, ValueError):
                team_id = pk.get("team_id") or 0
            existing = self.picks.get(overall)
            if existing is None:
                self.add_pick(overall, int(team_id), pid, source)
                added += 1
            elif existing.player_id != pid:
                if overwrite:
                    self.add_pick(overall, int(team_id), pid, source)
                    corrected += 1
                else:
                    skipped += 1
            else:
                skipped += 1
        return {"parsed": len(parsed), "added": added, "corrected": corrected,
                "skipped": skipped, "unmatched": unmatched}

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
        from .engine.profiler import active_runs, profile_all
        profiles = profile_all(state)
        teams = []
        for tid in self.draft_order:
            prof = profiles[tid]
            meta = self.team_meta.get(tid, {})
            roster = [{"name": p.name, "pos": p.position, "team": p.team,
                       "bye": p.bye_week} for p in state.roster(tid)]
            prior = self.priors.get(tid)
            teams.append({
                "team_id": tid,
                "name": meta.get("name") or f"Team {tid}",
                "owner": meta.get("owner"),
                "draft_slot": meta.get("draft_slot"),
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
            "size_inferred": self.auto_size,
            "starter_slots": self.league.starter_slots,
            "scoring_type": self.league.scoring_type,
            "is_superflex": self.league.is_superflex,
            "my_team_id": self.my_team_id,
            "current_overall": state.current_overall,
            "current_round": (state.current_overall - 1) // self.league.num_teams + 1,
            "on_the_clock": self.on_the_clock_team(),
            "is_my_turn": self.is_my_turn(),
            "active_runs": active_runs(state),
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
