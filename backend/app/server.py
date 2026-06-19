"""FastAPI server — the local hub the browser extension talks to over localhost.

Flow: the extension captures each pick from the live draft room and POSTs it
here. The server matches the player, updates rosters + opponent profiles, and
returns a recommendation for whoever is on the clock. When it's MY turn, the
recommendation includes the LLM's reasoning (unless no-LLM mode); otherwise it's
the deterministic engine output only. A sync indicator + one-click correction
endpoint back up the live feed.

Run:  uvicorn app.server:app --reload --port 8000   (from backend/)
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import bulk as bulk_mod, config, llm, session as session_mod
from .session import DraftSession, recommendation_to_dict

app = FastAPI(title="Fantasy Draft Agent", version="0.1.0")

_DASHBOARD = config.REPO_ROOT / "frontend" / "dashboard" / "index.html"

# Local advisory tool: the extension runs on the ESPN page and calls localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_session: DraftSession | None = None


def get_session() -> DraftSession:
    global _session
    if _session is None:
        _session = session_mod.open_session()
    return _session


# ---- request models -----------------------------------------------------
class PickIn(BaseModel):
    espn_id: str | None = None
    name: str | None = None
    position: str | None = None
    team: str | None = None          # NFL team abbrev (for D/ST matching)
    team_id: int | None = None       # ESPN fantasy team id; inferred if omitted
    overall: int | None = None       # inferred if omitted (from round/pick, else next)
    round: int | None = None         # draft round (DOM capture provides this)
    pick_in_round: int | None = None # pick within the round
    source: str = "websocket"
    use_llm: bool = True
    expected_overall: int | None = None  # ESPN's on-the-clock pick (sync truth)


class CorrectIn(PickIn):
    overall: int                      # required for a correction


class BulkIn(BaseModel):
    text: str                         # pasted pick list (any supported shape)
    overwrite: bool = False           # also correct existing mismatches
    use_llm: bool = False             # recompute with LLM after reconcile


class PickSyncIn(BaseModel):
    picks: list[dict]                 # structured picks from react/ws capture
    overwrite: bool = False
    use_llm: bool = True
    expected_overall: int | None = None   # ESPN's on-the-clock pick (sync truth)
    drafted_espn_ids: list[str] | None = None  # ESPN board "DRAFTED" safety net
    drafted_names: list[str] | None = None


class LiveContextIn(BaseModel):
    """Live draft-room identity scraped from the page (inject.js)."""
    on_clock_name: str | None = None       # team currently on the clock
    on_clock_overall: int | None = None    # the overall they're picking
    my_pick_overall: int | None = None     # overall I'm picking RIGHT NOW (my turn)
    my_next_overall: int | None = None     # my upcoming pick (from ESPN UI)
    num_teams: int | None = None           # live league size, if observed
    teams: list[dict] | None = None        # [{team_id, name, draft_slot}]
    pick_order: list[int] | None = None    # live draft-slot order of team_ids
    my_team_id: int | None = None          # explicit, if the room exposes it
    my_name: str | None = None             # my ESPN display name (popup config)
    drafted_espn_ids: list[str] | None = None
    drafted_names: list[str] | None = None
    use_llm: bool = True


# ---- dashboard (full-board live view) -----------------------------------
@app.get("/")
@app.get("/dashboard")
def dashboard():
    """Serve the local full-board dashboard (Phase 5). Open
    http://localhost:8000/ — it polls /state and /recommendation live."""
    return FileResponse(_DASHBOARD)


# ---- lifecycle ----------------------------------------------------------
@app.get("/health")
def health():
    s = get_session()
    return {
        "ok": True,
        "league_source": s.source,
        "league_loaded": s.league_id is not None,
        "my_team_id": s.my_team_id,
        "players_loaded": len(s.players_by_id),
        "llm_available": bool(config.anthropic_api_key()),
        "picks_recorded": len(s.picks),
    }


@app.post("/session/reset")
def reset_session():
    """Reload league config, players, and persisted picks from the DB.
    Call after auto-reading league config or refreshing the board."""
    global _session
    _session = session_mod.open_session()
    return health()


# ---- state + recommendation --------------------------------------------
@app.get("/state")
def state():
    return get_session().state_summary()


@app.get("/sync")
def sync(expected_overall: int | None = None):
    return get_session().sync_status(expected_overall)


def _recommend_payload(s: DraftSession, use_llm: bool,
                       expected_overall: int | None = None) -> dict:
    sync = s.sync_status(expected_overall)
    synced = sync["in_sync"]
    server_turn = s.is_my_turn()
    # ESPN's on-the-clock pick is the source of truth: only treat it as MY turn
    # (and only spend an LLM call) once our recorded picks have caught up.
    my_turn = server_turn and synced
    rec = s.recommend()
    llm_out = None
    if use_llm and my_turn:
        llm_out = llm.reason_on_the_clock(s, rec)
    payload = recommendation_to_dict(rec, llm_out)
    payload["is_my_turn"] = my_turn
    payload["on_the_clock"] = s.on_the_clock_team()
    payload["sync"] = sync
    payload["synced"] = synced
    if expected_overall is not None:
        payload["expected_overall"] = expected_overall
        payload["behind"] = max(0, expected_overall - rec.current_overall)
    return payload


@app.get("/recommendation")
def recommendation(use_llm: bool = True, expected_overall: int | None = None):
    return _recommend_payload(get_session(), use_llm, expected_overall)


# ---- pick ingestion -----------------------------------------------------
@app.post("/pick")
def add_pick(pick: PickIn):
    s = get_session()
    result = s.ingest_pick(
        espn_id=pick.espn_id, name=pick.name, position=pick.position,
        team=pick.team, team_id=pick.team_id, overall=pick.overall,
        round=pick.round, pick_in_round=pick.pick_in_round, source=pick.source,
    )
    # Recompute for whoever is now on the clock; pre-computes my pick the
    # instant the pick before mine lands.
    rec = _recommend_payload(s, pick.use_llm, pick.expected_overall)
    return {"pick": result, "recommendation": rec}


@app.post("/pick/correct")
def correct_pick(pick: CorrectIn):
    """One-click backstop: add or overwrite a single pick at a known overall."""
    s = get_session()
    result = s.ingest_pick(
        espn_id=pick.espn_id, name=pick.name, position=pick.position,
        team=pick.team, team_id=pick.team_id, overall=pick.overall,
        source="manual",
    )
    return {"pick": result, "recommendation": _recommend_payload(s, pick.use_llm)}


@app.post("/picks/sync")
def sync_picks(body: PickSyncIn):
    """Batch ingest structured picks (react state / fetch mirror). Faster than
    posting picks one-by-one when joining a draft mid-stream."""
    s = get_session()
    # Mirror ESPN's DRAFTED board so the safety net is current before we score.
    s.set_live_drafted(body.drafted_espn_ids, body.drafted_names)
    n = s.league.num_teams
    added = corrected = skipped = unmatched = 0
    unmatched_names: list[str] = []

    def _overall(pk: dict) -> int | None:
        if pk.get("overall"):
            return int(pk["overall"])
        # Derive from round/pick when the row carries them but no overall.
        if pk.get("round") and pk.get("pick_in_round"):
            return (int(pk["round"]) - 1) * n + int(pk["pick_in_round"])
        return None

    for pk in sorted(body.picks, key=lambda p: _overall(p) or 0):
        overall = _overall(pk)
        if not overall:
            continue
        existing = s.picks.get(overall)
        pid_probe, _ = s.matcher.match(
            pk.get("espn_id"), pk.get("name"), pk.get("position"), pk.get("team"))
        if pid_probe is None:
            unmatched += 1
            if pk.get("name"):
                unmatched_names.append(pk["name"])
            # Still ingest so the slot is occupied by a placeholder — an
            # unmatched pick must not leave a hole that makes us lag ESPN.
            s.ingest_pick(
                espn_id=pk.get("espn_id"), name=pk.get("name"),
                position=pk.get("position"), team=pk.get("team"),
                team_id=pk.get("team_id"), overall=overall,
                round=pk.get("round"), pick_in_round=pk.get("pick_in_round"),
                source=pk.get("source") or "sync",
            )
            continue
        if existing and existing.player_id == pid_probe and not body.overwrite:
            skipped += 1
            continue
        s.ingest_pick(
            espn_id=pk.get("espn_id"), name=pk.get("name"),
            position=pk.get("position"), team=pk.get("team"),
            team_id=pk.get("team_id"), overall=overall,
            round=pk.get("round"), pick_in_round=pk.get("pick_in_round"),
            source=pk.get("source") or "sync",
        )
        if existing and existing.player_id != pid_probe:
            corrected += 1
        elif not existing:
            added += 1
    return {
        "added": added, "corrected": corrected, "skipped": skipped,
        "unmatched": unmatched, "unmatched_names": unmatched_names,
        "picks_recorded": len(s.picks),
        "recommendation": _recommend_payload(s, body.use_llm, body.expected_overall),
    }


@app.post("/session/live-context")
def live_context(body: LiveContextIn):
    """Reconcile the live draft room's identity (on-the-clock team, team list,
    pick order) against saved config, and refresh the DRAFTED safety net. Lets
    snake math + my-turn detection track the actual room instead of stale config."""
    s = get_session()
    info = s.apply_live_context(
        on_clock_name=body.on_clock_name, on_clock_overall=body.on_clock_overall,
        teams=body.teams, pick_order=body.pick_order,
        my_team_id=body.my_team_id, my_name=body.my_name,
        my_next_overall=body.my_next_overall,
        my_pick_overall=body.my_pick_overall, num_teams=body.num_teams,
    )
    if body.drafted_espn_ids is not None or body.drafted_names is not None:
        info["live_drafted"] = s.set_live_drafted(
            body.drafted_espn_ids, body.drafted_names)
    info["recommendation"] = _recommend_payload(s, body.use_llm,
                                                body.on_clock_overall)
    return info


@app.post("/picks/bulk")
def bulk_reconcile(body: BulkIn):
    """Recovery backstop: paste a full/partial board (from Claude in Chrome or
    copied off ESPN) and fill any gaps. Resilient to tab/extension restarts
    since the server holds state — resync at any point."""
    s = get_session()
    parsed = bulk_mod.parse_picks(body.text)
    summary = s.reconcile(parsed, overwrite=body.overwrite)
    summary["recommendation"] = _recommend_payload(s, body.use_llm)
    return summary


@app.delete("/pick/{overall}")
def remove_pick(overall: int):
    s = get_session()
    existed = s.remove_pick(overall)
    return {"removed": existed, "overall": overall, "sync": s.sync_status()}
