"""End-to-end simulation of the live pipeline WITHOUT a browser or the LLM.

Saves a synthetic 12-team PPR league to the DB, then drives picks through the
FastAPI endpoints exactly as the extension would, and checks that rosters,
turn detection, sync, and the recommendation all come out right.

Requires the player board to be loaded first:  python -m app.cli refresh
Run:  python backend/tests/test_server_sim.py   (or via pytest)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, ingest, server  # noqa: E402
from app.data import espn  # noqa: E402

NUM_TEAMS = 12
MY_TEAM_ID = 5


def _save_synthetic_league():
    conn = db.connect()
    db.init_db(conn)
    payload = {
        "id": 555000, "seasonId": 2026,
        "settings": {
            "name": "Sim League", "size": NUM_TEAMS,
            "scoringSettings": {"scoringItems": [{"statId": 53, "points": 1.0}]},
            "rosterSettings": {"lineupSlotCounts": {
                "0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "16": 1, "17": 1, "20": 6}},
            "draftSettings": {"type": "SNAKE",
                              "pickOrder": list(range(1, NUM_TEAMS + 1))},
        },
        "members": [],
        "teams": [{"id": i, "name": f"Team {i}", "abbrev": f"T{i}"}
                  for i in range(1, NUM_TEAMS + 1)],
    }
    cfg = espn.parse_league_config(payload, my_team_id=MY_TEAM_ID)
    cfg["raw"] = {}
    ingest.save_league_config(conn, cfg)
    # Clear any prior sim picks so the run is deterministic.
    conn.execute("DELETE FROM picks WHERE league_id=? AND season=?", ("555000", 2026))
    conn.commit()
    conn.close()


def _top_player_names(client, n):
    """Pull the top-n names off the live board (by VORP) to draft with."""
    state = client.get("/state").json()  # forces session load
    # Use the session's own players for names by VORP via the recommendation.
    sess = server.get_session()
    from app.engine import vorp
    players = list(sess.players_by_id.values())
    vorp.assign_vorp(players, sess.league)
    players.sort(key=lambda p: (p.vorp or -1e9), reverse=True)
    return [p.name for p in players[:n]]


def run():
    _save_synthetic_league()
    client = TestClient(server.app)
    client.post("/session/reset")

    h = client.get("/health").json()
    assert h["league_loaded"], "league config not loaded"
    assert h["my_team_id"] == MY_TEAM_ID
    assert h["players_loaded"] > 1000, h

    names = _top_player_names(client, 40)
    # Draft the top 28 players in board order (overall inferred server-side).
    last = None
    for i, name in enumerate(names[:28]):
        r = client.post("/pick", json={"name": name, "use_llm": False})
        assert r.status_code == 200, r.text
        last = r.json()
        assert last["pick"]["ok"], f"unmatched pick: {name} -> {last['pick']}"

    # After 28 picks, overall 29 is on the clock. Snake: team 5.
    rec = last["recommendation"]
    assert rec["current_overall"] == 29, rec["current_overall"]
    assert rec["on_the_clock"] == MY_TEAM_ID, rec["on_the_clock"]
    assert rec["is_my_turn"] is True
    assert rec["primary"] is not None
    assert rec["my_next_overall"] == 44, rec["my_next_overall"]
    assert rec["picks_until_next"] == 14
    assert rec["sync"]["in_sync"] is True
    assert len(rec["shortlist"]) >= 1

    # Sync backstop: drop a pick, confirm the gap is detected, then restore it.
    client.delete("/pick/10")
    assert client.get("/sync").json()["in_sync"] is False
    client.post("/pick/correct", json={"overall": 10, "name": names[9],
                                        "use_llm": False})
    assert client.get("/sync").json()["in_sync"] is True

    print("PASS  server simulation: 28 picks ingested, turn/next/sync correct")
    print(f"      on the clock at overall {rec['current_overall']} (team "
          f"{rec['on_the_clock']}), next pick {rec['my_next_overall']}")
    print(f"      engine pick: {rec['primary']['name']} "
          f"({rec['primary']['position']}, VORP {rec['primary']['vorp']:.0f}, "
          f"P(avail next)={rec['primary']['p_available_next']:.0%})")
    print(f"      rationale: {rec['engine_rationale']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
