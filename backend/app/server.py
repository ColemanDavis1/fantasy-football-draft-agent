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
from pydantic import BaseModel

from . import config, llm, session as session_mod
from .session import DraftSession, recommendation_to_dict

app = FastAPI(title="Fantasy Draft Agent", version="0.1.0")

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
    overall: int | None = None       # inferred (next pick) if omitted
    source: str = "websocket"
    use_llm: bool = True


class CorrectIn(PickIn):
    overall: int                      # required for a correction


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


def _recommend_payload(s: DraftSession, use_llm: bool) -> dict:
    rec = s.recommend()
    llm_out = None
    if use_llm and s.is_my_turn():
        llm_out = llm.reason_on_the_clock(s, rec)
    payload = recommendation_to_dict(rec, llm_out)
    payload["is_my_turn"] = s.is_my_turn()
    payload["on_the_clock"] = s.on_the_clock_team()
    payload["sync"] = s.sync_status()
    return payload


@app.get("/recommendation")
def recommendation(use_llm: bool = True):
    return _recommend_payload(get_session(), use_llm)


# ---- pick ingestion -----------------------------------------------------
@app.post("/pick")
def add_pick(pick: PickIn):
    s = get_session()
    result = s.ingest_pick(
        espn_id=pick.espn_id, name=pick.name, position=pick.position,
        team=pick.team, team_id=pick.team_id, overall=pick.overall,
        source=pick.source,
    )
    # Recompute for whoever is now on the clock; pre-computes my pick the
    # instant the pick before mine lands.
    rec = _recommend_payload(s, pick.use_llm)
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


@app.delete("/pick/{overall}")
def remove_pick(overall: int):
    s = get_session()
    existed = s.remove_pick(overall)
    return {"removed": existed, "overall": overall, "sync": s.sync_status()}
