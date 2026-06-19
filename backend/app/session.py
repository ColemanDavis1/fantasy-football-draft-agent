"""Live draft session: the stateful glue between captured picks and the engine.

Holds the active league config + full player pool, accepts picks as they arrive
from the extension, persists them, and produces a recommendation for whoever is
on the clock. Engine work (deterministic) runs on every pick; the LLM is invoked
only when it's MY turn (see server.py), per the cost model.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from . import board, db, priors
from .match import normalize_name
from .engine import tiers, vorp
from .engine.draftflow import slot_index_for_overall, team_id_for_overall
from .engine.models import DraftState, Pick
from .engine.profiler import tendency_label
from .engine.recommend import Recommendation, build_recommendation
from .match import PlayerMatcher


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Player_id stored for a pick we can't resolve to our board (usually a player
# not in our fantasy-relevant pool). It occupies the overall slot so the pick
# count + snake math stay aligned with ESPN, but it isn't in players_by_id, so
# it never shows up in a roster or the available pool.
_UNMATCHED_PREFIX = "__unmatched__"


def _is_unmatched(player_id: str) -> bool:
    return player_id.startswith(_UNMATCHED_PREFIX)


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

        # My draft SLOT (0-based position in the order). This — not a team id —
        # is what snake math needs, and it's what we learn live from the room
        # (each time I'm on the clock). Seed it from any saved team id.
        self.my_slot_index: int | None = None
        if self.my_team_id is not None and self.my_team_id in self.draft_order:
            self.my_slot_index = self.draft_order.index(self.my_team_id)

        # Live size: the draft actually happening is the truth. We infer the
        # team count from the picks (num_teams = picks-per-round) and adopt it,
        # so a practice/mock draft — or any room whose size differs from saved
        # config — gets the right snake math instead of a stale guess. Only
        # trustworthy once a round-2 pick proves round 1 is complete.
        self._round1_max = 0          # largest pick_in_round seen in round 1
        self._size_finalized = False  # set once a round-2 pick proves the size
        # Display hint: True once the size came from the live room rather than
        # authoritative ESPN config (mock/practice drafts, or a size mismatch).
        self.auto_size = self.league_id in (None, "MOCK")

        # Live safety net (Phase 6): player_ids ESPN's board marks DRAFTED that
        # we haven't ingested a pick for yet. Kept out of the shortlist so sync
        # lag can't make us recommend an already-taken player. Not persisted —
        # it's a transient mirror of the live UI. (Set before any build_state.)
        self.live_drafted_ids: set[str] = set()

        # picks: overall -> Pick. Load any already persisted for this league.
        self.picks: dict[int, Pick] = {}
        self._load_persisted_picks()
        self._infer_size_from_persisted()

    def _adopt_size(self, n: int) -> None:
        """Set the live team count. If it differs from the current size, rebuild
        the order to 1..n and re-attribute every recorded pick to the team that
        owns its slot under the new size, so rosters stay coherent. My slot is
        preserved and my team id re-derived from it."""
        if n < 2 or n == self.league.num_teams:
            return
        self.auto_size = True  # size now reflects the live room, not config
        self.league.num_teams = n
        self.draft_order = list(range(1, n + 1))
        # Re-attribute picks: a slot's owner changes when the size changes.
        for o in sorted(self.picks):
            pk = self.picks[o]
            new_tid = team_id_for_overall(self.build_state(), o)
            if new_tid != pk.team_id:
                self.add_pick(o, int(new_tid), pk.player_id, "resize")
        self._apply_my_slot()

    def _apply_my_slot(self) -> None:
        """Re-derive my team id from my draft slot under the current order."""
        if self.my_slot_index is None:
            return
        if 0 <= self.my_slot_index < len(self.draft_order):
            tid = self.draft_order[self.my_slot_index]
            if tid != self.my_team_id:
                self._set_my_team(tid)

    def note_my_pick(self, overall: int) -> dict:
        """Record that it's MY turn at this overall (observed live from the
        room). Derives my draft slot — the reliable, self-correcting source of
        identity — and re-derives my team id from it."""
        slot = slot_index_for_overall(overall, self.league.num_teams)
        changed = slot != self.my_slot_index
        self.my_slot_index = slot
        self._apply_my_slot()
        return {"my_slot": slot + 1, "my_team_id": self.my_team_id,
                "changed": changed}

    def _infer_size_from_persisted(self) -> None:
        """Recover the inferred size from stored picks. Round 1's pick count is
        the team count, trustworthy once round 1 is complete — which the
        presence of any round-2 pick proves."""
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
            self._adopt_size(int(m))
            self._size_finalized = True

    def _load_team_meta(self) -> dict[int, dict]:
        if not self.league_id:
            return {}
        rows = self.conn.execute(
            "SELECT team_id, name, abbrev, owner, draft_slot FROM teams "
            "WHERE league_id=? AND season=?", (self.league_id, self.season),
        ).fetchall()
        return {r["team_id"]: {"name": r["name"], "abbrev": r["abbrev"],
                               "owner": r["owner"], "draft_slot": r["draft_slot"]}
                for r in rows}

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
        # Infer the live league size from the picks themselves so a practice/mock
        # draft — or any room whose size differs from saved config — gets the
        # right snake math. Two ways, most reliable first:
        #   exact: a pick that carries overall + round + pick_in_round pins the
        #          size at N = (overall - pick_in_round)/(round-1). Robust to
        #          out-of-order/missing round-1 picks (ESPN's react feed).
        #   count: a public mock with no overall — round 1's pick count is the
        #          size, trusted once a round-2 pick proves round 1 is complete.
        if round and pick_in_round and not self._size_finalized:
            if round == 1:
                self._round1_max = max(self._round1_max, pick_in_round)
            if overall and round >= 2:
                span = overall - pick_in_round
                if span % (round - 1) == 0 and span // (round - 1) >= 2:
                    self._adopt_size(span // (round - 1))
                    self._size_finalized = True
            elif round >= 2 and self._round1_max:
                self._adopt_size(self._round1_max)
                self._size_finalized = True
        if overall is None and round and pick_in_round:
            overall = (round - 1) * self.league.num_teams + pick_in_round
        if overall is None:
            overall = self.next_overall()
        if team_id is None:
            team_id = team_id_for_overall(self.build_state(), overall)
        if pid is None or pid not in self.players_by_id:
            # Occupy the slot with a placeholder so an unmatched pick (typically
            # a player not on our board) can't leave a hole that makes us look
            # behind ESPN. Don't overwrite a slot we've already resolved.
            recorded = False
            if overall is not None:
                existing = self.picks.get(overall)
                if existing is None or _is_unmatched(existing.player_id):
                    ph = f"{_UNMATCHED_PREFIX}{espn_id or name or overall}"
                    self.add_pick(overall, int(team_id), ph, source)
                    recorded = True
            return {"ok": False, "reason": "unmatched", "overall": overall,
                    "recorded": recorded, "name": name, "confidence": confidence}
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
        # Don't let the live-drafted safety net hide a player we've recorded a
        # pick for (the recorded pick is authoritative for rosters).
        recorded = {p.player_id for p in ordered}
        return DraftState(
            league=self.league,
            draft_order=self.draft_order,
            picks=ordered,
            my_team_id=self.my_team_id if self.my_team_id is not None else -1,
            players_by_id=self.players_by_id,
            extra_drafted_ids=self.live_drafted_ids - recorded,
        )

    # ---- live draft-room context ---------------------------------------
    def set_live_drafted(self, espn_ids: list[str] | None = None,
                         names: list[str] | None = None) -> int:
        """Mirror ESPN's DRAFTED board into a safety-net set. Maps each ESPN id
        or player name to our player id; unmapped entries are ignored."""
        ids: set[str] = set()
        for eid in (espn_ids or []):
            pid, _ = self.matcher.match(espn_id=str(eid))
            if pid in self.players_by_id:
                ids.add(pid)
        for name in (names or []):
            pid, _ = self.matcher.match(name=name)
            if pid in self.players_by_id:
                ids.add(pid)
        self.live_drafted_ids = ids
        return len(ids)

    def _match_team_by_name(self, name: str) -> int | None:
        """Fuzzy-match a draft-room display string to a known team_id by
        name/owner/abbrev. Exact matches win first so a substring (e.g. 'Team 1'
        vs 'Team 12') can't shadow them. Returns None if nothing matches."""
        target = normalize_name(name)
        if not target:
            return None

        def cands(meta: dict) -> list[str]:
            return [normalize_name(v or "") for v in
                    (meta.get("name"), meta.get("owner"), meta.get("abbrev"))]

        for tid, meta in self.team_meta.items():
            if target in cands(meta):
                return tid
        # Fall back to a substring containment (handles "Coleman" vs
        # "Coleman's Squad"), requiring a non-trivial overlap.
        for tid, meta in self.team_meta.items():
            for cand in cands(meta):
                if len(cand) >= 3 and (cand in target or target in cand):
                    return tid
        return None

    def _set_draft_order(self, order: list[int]) -> None:
        self.draft_order = list(order)
        self._size_finalized = True  # live order is authoritative; stop inferring
        if self.league.num_teams != len(order):
            self.league.num_teams = len(order)
        if self.league_id:
            self.conn.execute(
                "UPDATE league_settings SET pick_order=? WHERE league_id=? AND season=?",
                (json.dumps(list(order)), self.league_id, self.season))
            self.conn.commit()

    def _set_my_team(self, team_id: int) -> None:
        self.my_team_id = int(team_id)
        # Keep my slot in lockstep so identity survives a later size/order change.
        if team_id in self.draft_order:
            self.my_slot_index = self.draft_order.index(team_id)
        if self.league_id:
            self.conn.execute(
                "UPDATE teams SET is_me=0 WHERE league_id=? AND season=?",
                (self.league_id, self.season))
            self.conn.execute(
                "UPDATE teams SET is_me=1 WHERE league_id=? AND season=? AND team_id=?",
                (self.league_id, self.season, int(team_id)))
            self.conn.commit()

    def _upsert_team_meta(self, teams: list[dict]) -> None:
        for t in teams:
            tid = t.get("team_id")
            if tid is None:
                continue
            tid = int(tid)
            meta = self.team_meta.setdefault(tid, {})
            if t.get("name"):
                meta["name"] = t["name"]
            if t.get("draft_slot") is not None:
                meta["draft_slot"] = t["draft_slot"]
            if self.league_id:
                self.conn.execute(
                    """INSERT INTO teams (league_id, season, team_id, name, draft_slot)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(league_id, season, team_id) DO UPDATE SET
                           name=COALESCE(excluded.name, teams.name),
                           draft_slot=COALESCE(excluded.draft_slot, teams.draft_slot)""",
                    (self.league_id, self.season, tid, t.get("name"),
                     t.get("draft_slot")))
        if self.league_id:
            self.conn.commit()

    def apply_live_context(self, on_clock_name: str | None = None,
                           on_clock_overall: int | None = None,
                           teams: list[dict] | None = None,
                           pick_order: list[int] | None = None,
                           my_team_id: int | None = None,
                           my_name: str | None = None,
                           my_next_overall: int | None = None,
                           my_pick_overall: int | None = None,
                           num_teams: int | None = None) -> dict:
        """Reconcile live draft-room identity against saved config.

        Identity is anchored to my DRAFT SLOT, learned from the room itself, so
        snake math (who picks before me, what they have) is always right:
        - my_pick_overall: the overall I'm picking RIGHT NOW (observed each time
          I'm on the clock) — the strongest, self-correcting signal.
        - my_next_overall: my upcoming pick highlighted on ESPN's draft strip.
        - explicit my_team_id, or my name matched to the on-the-clock team.
        Also adopts the room's live team count + pick order so a practice/mock
        draft uses the real size, not stale config."""
        result: dict = {"my_team_id_before": self.my_team_id}
        if num_teams and num_teams >= 2:
            self._adopt_size(int(num_teams))   # live room size wins
        if teams:
            self._upsert_team_meta(teams)
        # Live pick order updates the ORDER (a same-length permutation). Size is
        # learned from the picks themselves (more reliable), so we don't resize
        # off a possibly-partial order scrape.
        if pick_order:
            order = [int(t) for t in pick_order if t is not None]
            if (len(order) == self.league.num_teams and len(set(order)) == len(order)
                    and list(order) != list(self.draft_order)):
                self._set_draft_order(order)
                self._apply_my_slot()
                result["draft_order_updated"] = True

        # Resolve my draft SLOT (0-based) from the strongest available signal.
        n = self.league.num_teams
        new_slot = None
        if my_pick_overall is not None:
            new_slot = slot_index_for_overall(int(my_pick_overall), n)
        elif my_next_overall is not None:
            new_slot = slot_index_for_overall(int(my_next_overall), n)
            result["my_next_overall"] = int(my_next_overall)
        elif my_team_id is not None and int(my_team_id) in self.draft_order:
            new_slot = self.draft_order.index(int(my_team_id))
        elif my_name and on_clock_name and normalize_name(my_name) \
                and normalize_name(my_name) == normalize_name(on_clock_name) \
                and on_clock_overall:
            # I'm on the clock and the room shows my name → that slot is me.
            new_slot = slot_index_for_overall(int(on_clock_overall), n)
        else:
            # Fuzzy name fallback → a team id, converted to a slot if we can.
            fuzzy = None
            if my_name and on_clock_name and normalize_name(my_name) \
                    == normalize_name(on_clock_name):
                fuzzy = self._match_team_by_name(on_clock_name)
            elif my_name:
                fuzzy = self._match_team_by_name(my_name)
            if fuzzy is not None and fuzzy in self.draft_order:
                new_slot = self.draft_order.index(fuzzy)

        if new_slot is not None and 0 <= new_slot < n:
            before = self.my_team_id
            self.my_slot_index = new_slot
            self._apply_my_slot()
            if self.my_team_id != before:
                result["my_team_id_updated"] = True

        result["my_team_id"] = self.my_team_id
        result["my_slot"] = (self.my_slot_index + 1
                             if self.my_slot_index is not None else None)
        result["num_teams"] = self.league.num_teams
        result["on_clock_team"] = self._match_team_by_name(on_clock_name) \
            if on_clock_name else None
        result["draft_order"] = list(self.draft_order)
        if "my_next_overall" not in result:
            rec = self.recommend()
            result["my_next_overall"] = rec.my_next_overall
        return result

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
