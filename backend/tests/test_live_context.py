"""Live draft-room reconciliation + drafted safety net (Phase 6).

Drives the same FastAPI endpoints the extension uses to prove:
  - POST /session/live-context applies the room's live pick order + locks in my
    team (explicitly or by matching my name to the on-the-clock team), so snake
    math (my_next_overall) tracks the real room, not stale config.
  - A player ESPN marks DRAFTED is kept out of the shortlist even before his
    pick syncs, and the server refuses to call it MY turn while catching up.

Requires the player board:  python -m app.cli refresh
Run:  python backend/tests/test_live_context.py   (or via pytest)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, server  # noqa: E402
from test_server_sim import (MY_TEAM_ID, NUM_TEAMS,  # noqa: E402
                             _save_synthetic_league)


def _fresh_client() -> TestClient:
    _save_synthetic_league()
    client = TestClient(server.app)
    client.post("/session/reset")
    return client


def _espn_id_for(name: str) -> str | None:
    conn = db.connect()
    row = conn.execute(
        "SELECT espn_id FROM players WHERE full_name=? AND espn_id IS NOT NULL "
        "LIMIT 1", (name,)).fetchone()
    return row["espn_id"] if row else None


def test_live_pick_order_drives_snake():
    """A reversed live pick order must change which overall is my next pick."""
    client = _fresh_client()

    # Default order is 1..12 → my team (5) sits at slot 5, first pick overall 5.
    base = client.get("/recommendation").json()
    assert base["my_next_overall"] == 5, base["my_next_overall"]

    # Live room reports a REVERSED order. Team 5 now sits at slot index 7, so
    # its first pick is overall 8 (slot 8 in round 1).
    reversed_order = list(range(NUM_TEAMS, 0, -1))
    res = client.post("/session/live-context", json={
        "pick_order": reversed_order,
        "my_team_id": MY_TEAM_ID,
        "use_llm": False,
    }).json()
    assert res.get("draft_order_updated") is True, res
    assert res["draft_order"] == reversed_order, res["draft_order"]
    assert res["my_team_id"] == MY_TEAM_ID

    rec = res["recommendation"]
    assert rec["my_next_overall"] == 8, rec["my_next_overall"]
    print("PASS  live pick order drives snake: my_next 5 -> 8 after reversal")


def test_name_match_locks_in_my_team():
    """When I'm on the clock and the room shows my name, lock in that team."""
    client = _fresh_client()
    # Synthetic team 7 is named "Team 7"; default order means overall 7 is theirs.
    res = client.post("/session/live-context", json={
        "on_clock_name": "Team 7",
        "on_clock_overall": 7,
        "my_name": "Team 7",
        "use_llm": False,
    }).json()
    assert res.get("my_team_id_updated") is True, res
    assert res["my_team_id"] == 7, res
    print("PASS  name match locks in my_team_id = 7")


def test_drafted_safety_net_excludes_player():
    """A DRAFTED espn_id is excluded from the shortlist, and we stay 'syncing'
    (not my turn) while ESPN's pick is ahead of our recorded picks."""
    client = _fresh_client()
    rec = client.get("/recommendation").json()
    # Use the first shortlisted player that has an ESPN id (the DRAFTED board is
    # keyed by ESPN id). Not every Sleeper player carries one.
    top, espn_id = None, None
    for c in rec["shortlist"]:
        eid = _espn_id_for(c["name"])
        if eid:
            top, espn_id = c["name"], eid
            break
    assert espn_id, f"no shortlist candidate had an espn_id: {rec['shortlist']}"

    # No picks recorded, but ESPN says we're at pick 10 and `top` is DRAFTED.
    out = client.post("/picks/sync", json={
        "picks": [],
        "drafted_espn_ids": [espn_id],
        "expected_overall": 10,
        "use_llm": True,
    }).json()
    rec2 = out["recommendation"]
    names = [c["name"] for c in rec2["shortlist"]]
    assert top not in names, f"{top} still in shortlist {names}"
    assert rec2["synced"] is False, rec2
    assert rec2["behind"] == 9, rec2["behind"]      # expected 10 - current 1
    assert rec2["is_my_turn"] is False, rec2         # never my turn while behind
    assert rec2["llm"] is None, "LLM must not fire while catching up"
    print(f"PASS  drafted safety net excludes {top}; stays syncing (behind 9)")


def test_my_next_overall_locks_draft_slot():
    """ESPN's highlighted next pick (overall 29) must lock my team slot."""
    client = _fresh_client()
    # 12-team default order: team 5's third pick is overall 29 (R1:5, R2:20, R3:29).
    res = client.post("/session/live-context", json={
        "on_clock_overall": 28,
        "my_next_overall": 29,
        "use_llm": False,
    }).json()
    assert res["my_team_id"] == MY_TEAM_ID, res
    assert res["my_next_overall"] == 29, res
    print("PASS  my_next_overall=29 identifies team 5")


def test_my_pick_overall_locks_slot():
    """Observing the overall I'm picking RIGHT NOW must pin my draft slot."""
    client = _fresh_client()
    # Default 12-team order: overall 29 belongs to slot 5 (team 5).
    res = client.post("/session/live-context", json={
        "my_pick_overall": 29, "use_llm": False,
    }).json()
    assert res["my_team_id"] == 5, res
    assert res["my_slot"] == 5, res
    print("PASS  my_pick_overall=29 locks slot 5 (team 5)")


def test_live_size_corrects_snake():
    """A live room smaller than saved config (practice draft) must drive snake
    math off the REAL size, not the stale 12-team config."""
    client = _fresh_client()
    # Saved config is 12 teams; the live room is 10. I'm picking overall 1 (slot
    # 1), so my next pick is overall 20 in a 10-team snake (24 if it stayed 12).
    res = client.post("/session/live-context", json={
        "num_teams": 10, "my_pick_overall": 1, "use_llm": False,
    }).json()
    assert res["num_teams"] == 10, res
    assert res["my_team_id"] == 1 and res["my_slot"] == 1, res
    assert res["my_next_overall"] == 20, res["my_next_overall"]
    print("PASS  live size 10 corrects snake: my_next_overall=20 (not 24)")


def test_unmatched_pick_fills_slot_no_hole():
    """An unmatched pick must occupy its overall (as a placeholder) so the
    count stays aligned with ESPN instead of lagging by the number of holes."""
    client = _fresh_client()
    rec = client.get("/recommendation").json()
    real = [c["name"] for c in rec["shortlist"]][:4]
    assert len(real) == 4, rec["shortlist"]

    # Overalls 1,2,4,5 are real players; overall 3 is a player not on our board.
    picks = [
        {"overall": 1, "name": real[0]},
        {"overall": 2, "name": real[1]},
        {"overall": 3, "name": "Zzz Notaplayer", "espn_id": "999999999"},
        {"overall": 4, "name": real[2]},
        {"overall": 5, "name": real[3]},
    ]
    out = client.post("/picks/sync", json={
        "picks": picks, "expected_overall": 6, "use_llm": False,
    }).json()
    assert out["unmatched"] == 1, out
    # Slot 3 is filled by a placeholder → 5 slots occupied, next pick is 6.
    assert out["picks_recorded"] == 5, out
    rec2 = out["recommendation"]
    assert rec2["current_overall"] == 6, rec2["current_overall"]
    assert rec2["synced"] is True, rec2          # no hole → caught up to ESPN
    assert rec2["sync"]["missing_overalls"] == [], rec2["sync"]
    print("PASS  unmatched pick fills slot; no sync hole (current_overall=6)")


def run():
    test_live_pick_order_drives_snake()
    test_name_match_locks_in_my_team()
    test_my_next_overall_locks_draft_slot()
    test_my_pick_overall_locks_slot()
    test_live_size_corrects_snake()
    test_drafted_safety_net_excludes_player()
    test_unmatched_pick_fills_slot_no_hole()
    print("\nAll live-context tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
