"""Tests for the bulk paste parser + reconcile (the recovery backstop).

    python -m pytest backend/tests/test_bulk.py
    python backend/tests/test_bulk.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import bulk, db  # noqa: E402
from app.session import DraftSession  # noqa: E402


def test_parse_pipe_with_and_without_team_id():
    rows = bulk.parse_picks(
        "1 | Ja'Marr Chase | WR | CIN\n3 | 5 | Bijan Robinson | RB | ATL")
    assert rows[0] == {"overall": 1, "round": None, "pick_in_round": None,
                       "name": "Ja'Marr Chase", "position": "WR", "team": "CIN"}, rows[0]
    # The stray team id (5) is ignored; name still resolves.
    assert rows[1]["overall"] == 3 and rows[1]["name"] == "Bijan Robinson"
    assert rows[1]["position"] == "RB" and rows[1]["team"] == "ATL"


def test_parse_numbered_and_parenthetical_and_bare():
    rows = bulk.parse_picks(
        "12. Bijan Robinson, RB ATL\n"
        "CeeDee Lamb (DAL - WR)\n"
        "Patrick Mahomes")
    assert rows[0]["overall"] == 12 and rows[0]["name"] == "Bijan Robinson"
    assert rows[0]["position"] == "RB" and rows[0]["team"] == "ATL"
    assert rows[1]["name"] == "CeeDee Lamb" and rows[1]["team"] == "DAL"
    assert rows[1]["position"] == "WR"
    assert rows[2]["name"] == "Patrick Mahomes" and rows[2]["overall"] is None


def test_parse_round_dot_pick():
    rows = bulk.parse_picks("1.05 Ja'Marr Chase WR CIN")
    assert rows[0]["round"] == 1 and rows[0]["pick_in_round"] == 5
    assert rows[0]["name"] == "Ja'Marr Chase" and rows[0]["overall"] is None


def test_parse_skips_headers_and_blanks():
    rows = bulk.parse_picks("Round 1\n\n  \nDRAFT RESULTS\nJustin Jefferson WR MIN")
    assert len(rows) == 1 and rows[0]["name"] == "Justin Jefferson"


# ---- reconcile against a live session -----------------------------------
def _session() -> DraftSession:
    conn = db.connect(":memory:")
    db.init_db(conn)
    now = "2026-06-18T00:00:00+00:00"
    conn.execute(
        """INSERT INTO league_settings(league_id, season, num_teams, scoring_type,
               starter_slots, pick_order, updated_at)
           VALUES ('L', 2026, 4, 'ppr', ?, ?, ?)""",
        (json.dumps({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1}),
         json.dumps([1, 2, 3, 4]), now))
    conn.executemany(
        "INSERT INTO teams(league_id, season, team_id, name, is_me) VALUES ('L',2026,?,?,?)",
        [(1, "Me", 1), (2, "Bo", 0), (3, "Cy", 0), (4, "Di", 0)])
    conn.executemany(
        """INSERT INTO players(player_id, espn_id, full_name, position, team,
               search_rank, adp, active, updated_at)
           VALUES (?,?,?,?,?,?,?,1,?)""",
        [("chase", "1001", "Ja'Marr Chase", "WR", "CIN", 1, 1.0, now),
         ("bijan", "1002", "Bijan Robinson", "RB", "ATL", 2, 2.0, now),
         ("lamb",  "1003", "CeeDee Lamb",    "WR", "DAL", 3, 3.0, now)])
    conn.commit()
    return DraftSession(conn)


PASTE = ("1 | Ja'Marr Chase | WR | CIN\n"
         "2. Bijan Robinson, RB ATL\n"
         "CeeDee Lamb")


def test_reconcile_adds_picks_and_infers_teams():
    s = _session()
    res = s.reconcile(bulk.parse_picks(PASTE))
    assert res == {"parsed": 3, "added": 3, "corrected": 0, "skipped": 0,
                   "unmatched": []}, res
    # Snake math on a 4-team league: picks 1,2,3 -> teams 1,2,3.
    assert s.picks[1].team_id == 1 and s.picks[1].player_id == "chase"
    assert s.picks[2].team_id == 2 and s.picks[2].player_id == "bijan"
    assert s.picks[3].team_id == 3 and s.picks[3].player_id == "lamb"


def test_reconcile_fills_only_gaps():
    s = _session()
    s.add_pick(2, 2, "bijan", source="websocket")   # already captured pick 2
    res = s.reconcile(bulk.parse_picks(PASTE))
    assert res["added"] == 2 and res["skipped"] == 1 and res["corrected"] == 0, res


def test_reconcile_overwrite_corrects_mismatch():
    s = _session()
    s.add_pick(1, 1, "lamb", source="websocket")     # wrong player at pick 1
    res = s.reconcile(bulk.parse_picks(PASTE), overwrite=True)
    assert res["corrected"] == 1, res
    assert s.picks[1].player_id == "chase"


def test_reconcile_reports_unmatched():
    s = _session()
    res = s.reconcile(bulk.parse_picks("Nonexistent Player\nJa'Marr Chase WR"))
    assert "Nonexistent Player" in res["unmatched"]
    assert res["added"] == 1  # Chase still landed


ALL_TESTS = [
    test_parse_pipe_with_and_without_team_id,
    test_parse_numbered_and_parenthetical_and_bare,
    test_parse_round_dot_pick,
    test_parse_skips_headers_and_blanks,
    test_reconcile_adds_picks_and_infers_teams,
    test_reconcile_fills_only_gaps,
    test_reconcile_overwrite_corrects_mismatch,
    test_reconcile_reports_unmatched,
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
