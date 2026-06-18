"""Phase-4 historical-priors tests against synthetic ESPN payloads (no network).

Validates the draft-recap parser, the archetype classifier, and the end-to-end
build that follows a manager across two prior seasons by owner id and attaches
the prior to their current team.

    python -m pytest backend/tests/test_priors.py
    python backend/tests/test_priors.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db, priors  # noqa: E402
from app.data import espn  # noqa: E402

# Owner GUIDs (stable across seasons); team_ids deliberately differ per season.
OWNER_A = "{AAAA-1111}"   # drafts RB-RB early -> Robust-RB
OWNER_B = "{BBBB-2222}"   # avoids RB early -> Zero-RB


def _draft_payload(season, team_a, team_b, picks):
    """picks: list of (overall, round, team_id, espn_player_id)."""
    return {
        "draftDetail": {"drafted": True, "picks": [
            {"overallPickNumber": o, "roundId": r, "roundPickNumber": o,
             "teamId": tid, "playerId": pid, "keeper": False}
            for (o, r, tid, pid) in picks
        ]},
        "teams": [{"id": team_a, "primaryOwner": OWNER_A, "owners": [OWNER_A]},
                  {"id": team_b, "primaryOwner": OWNER_B, "owners": [OWNER_B]}],
        "members": [{"id": OWNER_A, "displayName": "Alice"},
                    {"id": OWNER_B, "displayName": "Bob"}],
    }


# espn player id -> position (synthetic universe)
POS = {"100": "RB", "101": "RB", "102": "WR", "103": "WR",
       "200": "WR", "201": "WR", "202": "RB", "203": "TE"}


def test_parse_draft_picks_orders_and_shapes():
    payload = _draft_payload(2024, 1, 2,
                             [(2, 1, 2, "200"), (1, 1, 1, "100")])
    picks = espn.parse_draft_picks(payload)
    assert [p["overall"] for p in picks] == [1, 2]  # sorted by overall
    assert picks[0]["team_id"] == 1 and picks[0]["espn_player_id"] == "100"


def test_archetype_classifier():
    assert priors._archetype_from_positions(["RB", "RB", "WR"]) == "Robust-RB"
    assert priors._archetype_from_positions(["WR", "WR", "TE", "WR"]) == "Zero-RB"
    assert priors._archetype_from_positions(["RB", "WR", "WR"]) == "Hero-RB"


def test_build_priors_follows_owner_across_seasons():
    conn = db.connect(":memory:")
    db.init_db(conn)
    now = "2026-06-18T00:00:00+00:00"

    # Current league: Alice is team 7, Bob is team 9 (different from prior years).
    current_payload = {
        "teams": [{"id": 7, "primaryOwner": OWNER_A, "owners": [OWNER_A]},
                  {"id": 9, "primaryOwner": OWNER_B, "owners": [OWNER_B]}],
        "members": [{"id": OWNER_A, "displayName": "Alice"},
                    {"id": OWNER_B, "displayName": "Bob"}],
    }
    conn.execute(
        """INSERT INTO league_settings(league_id, season, num_teams, scoring_type,
                                       raw_json, updated_at)
           VALUES ('L1', 2026, 12, 'ppr', ?, ?)""",
        (json.dumps(current_payload), now))
    conn.commit()

    # Two prior seasons. Alice (team ids 1 then 3) takes RB-RB first; Bob
    # (team ids 2 then 4) avoids RB early.
    seasons = {
        2024: _draft_payload(2024, 1, 2, [
            (1, 1, 1, "100"), (2, 1, 2, "200"),   # A: RB, B: WR
            (3, 2, 1, "101"), (4, 2, 2, "201"),   # A: RB, B: WR
            (5, 3, 1, "102"), (6, 3, 2, "203"),   # A: WR, B: TE
        ]),
        2025: _draft_payload(2025, 3, 4, [
            (1, 1, 3, "100"), (2, 1, 4, "200"),
            (3, 2, 3, "101"), (4, 2, 4, "201"),
            (5, 3, 3, "102"), (6, 3, 4, "202"),   # B eventually takes an RB
        ]),
    }
    espn.fetch_draft_detail = lambda lid, yr, **k: seasons[yr]      # type: ignore
    espn.fetch_player_positions = lambda lid, yr, **k: POS          # type: ignore

    res = priors.build_priors(conn, "L1", 2026, [2024, 2025], prior_league_id="L1")
    assert res["seasons_used"] == [2024, 2025], res
    assert res["written"] == 2, res

    loaded = priors.load_priors(conn, "L1", 2026)
    assert loaded[7]["archetype"] == "Robust-RB", loaded[7]   # Alice -> current team 7
    assert loaded[9]["archetype"] == "Zero-RB", loaded[9]     # Bob -> current team 9
    assert loaded[7]["drafts"] == 2
    assert loaded[7]["name"] == "Alice"
    # RB should dominate Alice's position share.
    assert loaded[7]["position_share"]["RB"] >= 0.5, loaded[7]["position_share"]
    conn.close()


ALL_TESTS = [
    test_parse_draft_picks_orders_and_shapes,
    test_archetype_classifier,
    test_build_priors_follows_owner_across_seasons,
]


def _run_standalone() -> int:
    failures = 0
    for t in ALL_TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # pragma: no cover
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(ALL_TESTS) - failures}/{len(ALL_TESTS)} passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
