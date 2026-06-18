"""Phase 4 pre-draft enrichment: a night-before Batch API pass over the pool.

Adds qualitative scouting context the deterministic engine can't compute — a
one-line note plus a sleeper/bust/value flag — grounded in fresh web research
(camp battles, depth-chart moves, rookie roles). Design per the spec:

  - Batch API (50% off, async, not time-sensitive) — run it the night before.
  - Opus 4.8 with adaptive thinking; the shared task + scoring context sit in a
    cached system block so every request only pays full price for the player.
  - web_search server tool for current news; structured output for clean parse.
  - Stored on players.enrichment_json. Fully optional: no key -> no-op, and the
    engine/LLM both run without it.

Flow is split into submit -> collect so a long-running batch can be picked up
later (batch id is stashed in meta). `run(..., wait=True)` does both.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

from . import config, db

MODEL = "claude-opus-4-8"
BATCH_ID_META = "enrich_batch_id"

# Structured-output schema for each player's enrichment (no prefill on 4.8).
_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {"type": "string"},  # one skimmable sentence
        "flag": {"type": "string",
                 "enum": ["sleeper", "value", "solid", "risk", "bust"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["note", "flag", "confidence"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are an elite fantasy football analyst preparing draft-board notes the "
    "night before a draft. For the single player in the user message, use web "
    "search to check the latest news (training-camp battles, depth-chart and "
    "role changes, injuries, rookie usage, beat-writer reporting) and return a "
    "concise draft-day read. 'note' is ONE skimmable sentence a drafter can act "
    "on. 'flag' is your single best label: sleeper (likely outperforms ADP), "
    "value (solid pick at cost), solid (steady, fairly priced), risk (volatile "
    "or injury/role concern), bust (likely underperforms ADP). 'confidence' "
    "reflects how strong the current signal is. Be specific and current; do not "
    "invent news."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _player_rows(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Top `limit` active players by ADP/search_rank — the ones worth a note."""
    return conn.execute(
        """SELECT player_id, full_name, position, team, bye_week, adp,
                  search_rank, age, years_exp, injury_status, proj_points
           FROM players
           WHERE active=1 AND position IN ('QB','RB','WR','TE','K','DEF')
           ORDER BY (adp IS NULL), adp ASC, (search_rank IS NULL), search_rank ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def _user_text(r: sqlite3.Row, season: int) -> str:
    bits = [
        f"Season: {season}.",
        f"Player: {r['full_name']} | {r['position']} | "
        f"{r['team'] or 'FA'} | bye {r['bye_week'] or '?'}.",
        f"ADP ~{r['adp']:.0f}." if r["adp"] is not None else "ADP unknown.",
    ]
    if r["age"]:
        bits.append(f"Age {r['age']}, {r['years_exp'] or 0} yrs exp.")
    if r["injury_status"]:
        bits.append(f"Injury status: {r['injury_status']}.")
    if r["proj_points"]:
        bits.append(f"Projected season points: {r['proj_points']:.0f}.")
    return " ".join(bits)


def _build_requests(conn, rows, season, use_web_search: bool):
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    system = [{"type": "text", "text": _SYSTEM,
               "cache_control": {"type": "ephemeral"}}]
    tools = ([{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
             if use_web_search else [])
    requests = []
    for r in rows:
        params = MessageCreateParamsNonStreaming(
            model=MODEL,
            max_tokens=2000,
            thinking={"type": "adaptive"},
            system=system,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": _user_text(r, season)}],
            **({"tools": tools} if tools else {}),
        )
        requests.append(Request(custom_id=r["player_id"], params=params))
    return requests


def submit(conn: sqlite3.Connection, limit: int = 150,
           use_web_search: bool = True, season: int | None = None) -> str:
    """Create the batch and stash its id in meta. Returns the batch id."""
    if not config.anthropic_api_key():
        raise RuntimeError("ANTHROPIC_API_KEY not set — enrichment needs a key. "
                           "(The draft tool itself runs fine without it.)")
    import anthropic
    season = season or config.current_nfl_season()
    rows = _player_rows(conn, limit)
    if not rows:
        raise RuntimeError("No players loaded. Run `refresh` first.")
    requests = _build_requests(conn, rows, season, use_web_search)
    client = anthropic.Anthropic()
    batch = client.messages.batches.create(requests=requests)
    db.set_meta(conn, BATCH_ID_META, batch.id)
    db.set_meta(conn, "enrich_batch_submitted_at", _now())
    db.set_meta(conn, "enrich_batch_count", str(len(requests)))
    return batch.id


def _extract(message) -> dict | None:
    """Pull the JSON object out of a (structured-output) batch message."""
    text = next((b.text for b in message.content if b.type == "text"), "")
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None


def collect(conn: sqlite3.Connection, batch_id: str | None = None,
            wait: bool = False, poll_secs: int = 30,
            max_wait_secs: int = 3600) -> dict:
    """Fetch results for a (completed) batch and write enrichment onto players.
    If wait=True, polls until the batch ends or max_wait_secs elapses."""
    import anthropic
    batch_id = batch_id or db.get_meta(conn, BATCH_ID_META)
    if not batch_id:
        raise RuntimeError("No batch id known. Run `enrich --submit` first.")
    client = anthropic.Anthropic()

    waited = 0
    status = client.messages.batches.retrieve(batch_id).processing_status
    while status != "ended" and wait and waited < max_wait_secs:
        time.sleep(poll_secs)
        waited += poll_secs
        status = client.messages.batches.retrieve(batch_id).processing_status
    if status != "ended":
        return {"status": status, "written": 0, "batch_id": batch_id}

    now = _now()
    written = errored = 0
    updates = []
    for result in client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            errored += 1
            continue
        parsed = _extract(result.result.message)
        if not parsed:
            errored += 1
            continue
        parsed["model"] = MODEL
        parsed["updated_at"] = now
        updates.append((json.dumps(parsed), result.custom_id))
        written += 1
    conn.executemany(
        "UPDATE players SET enrichment_json=? WHERE player_id=?", updates)
    db.set_meta(conn, "enrich_last_collected_at", now)
    conn.commit()
    return {"status": "ended", "written": written, "errored": errored,
            "batch_id": batch_id}


def run(conn: sqlite3.Connection, limit: int = 150, use_web_search: bool = True,
        wait: bool = True) -> dict:
    """Submit a fresh batch, then (optionally) wait and collect."""
    batch_id = submit(conn, limit=limit, use_web_search=use_web_search)
    out = {"batch_id": batch_id, "submitted": True}
    if wait:
        out.update(collect(conn, batch_id=batch_id, wait=True))
    return out


def coverage(conn: sqlite3.Connection) -> dict:
    """How many active players carry enrichment notes."""
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN enrichment_json IS NOT NULL THEN 1 ELSE 0 END) AS enriched
           FROM players WHERE active=1
             AND position IN ('QB','RB','WR','TE','K','DEF')"""
    ).fetchone()
    return {"total": row["total"] or 0, "enriched": row["enriched"] or 0}
