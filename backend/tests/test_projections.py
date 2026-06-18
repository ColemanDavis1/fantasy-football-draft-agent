"""Proof of the Phase-4 projection ingest against a synthetic ESPN payload.

We can't hit a live ESPN league here (and 2026 projections may not exist yet),
so we validate the parser + DB join against a kona_player_info-shaped payload,
including the discriminators that matter: statSourceId (0 actual vs 1 projected),
statSplitTypeId (0 = season), and seasonId.

Runnable two ways:
    python -m pytest backend/tests/test_projections.py
    python backend/tests/test_projections.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import board, db, ingest  # noqa: E402
from app.data import espn  # noqa: E402

SEASON = 2026


def _stat(source_id, split_id, season, total):
    return {"statSourceId": source_id, "statSplitTypeId": split_id,
            "seasonId": season, "appliedTotal": total}


def _synthetic_payload() -> dict:
    """Three players. Each has an ACTUAL block (must be ignored) and a season
    PROJECTION block; one also has a weekly projection (must be ignored)."""
    return {"players": [
        {"id": 1001, "player": {
            "id": 1001, "fullName": "Real RB", "defaultPositionId": 2,
            "stats": [
                _stat(espn.STAT_SOURCE_ACTUAL, 0, SEASON - 1, 999.0),     # last yr actual
                _stat(espn.STAT_SOURCE_PROJECTED, 0, SEASON, 305.4),      # <- the one we want
                _stat(espn.STAT_SOURCE_PROJECTED, 1, SEASON, 18.0),       # weekly proj, ignore
            ]}},
        {"id": 1002, "player": {
            "id": 1002, "fullName": "Real WR", "defaultPositionId": 3,
            "stats": [
                _stat(espn.STAT_SOURCE_PROJECTED, 0, SEASON, 270.1),
            ]}},
        {"id": 1003, "player": {
            "id": 1003, "fullName": "No Projection Guy", "defaultPositionId": 4,
            "stats": [
                _stat(espn.STAT_SOURCE_ACTUAL, 0, SEASON - 1, 88.0),      # only actuals
            ]}},
    ]}


def test_parse_picks_projection_not_actual():
    proj = espn.parse_projections(_synthetic_payload(), SEASON)
    # Players with a season projection are kept; the actuals-only one is dropped.
    assert set(proj.keys()) == {"1001", "1002"}, proj
    # The season projection total wins, not the actual (999) or weekly (18).
    assert proj["1001"]["points"] == 305.4, proj["1001"]
    assert proj["1001"]["position"] == "RB"
    assert proj["1002"]["points"] == 270.1


def test_projection_fallback_when_season_mismatch():
    # If ESPN only has a projection keyed to a different season, take it anyway.
    payload = {"players": [{"id": 7, "player": {
        "id": 7, "fullName": "X", "defaultPositionId": 2,
        "stats": [_stat(espn.STAT_SOURCE_PROJECTED, 0, SEASON + 1, 200.0)]}}]}
    proj = espn.parse_projections(payload, SEASON)
    assert proj["7"]["points"] == 200.0


def test_ingest_joins_on_espn_id_and_board_prefers_real():
    conn = db.connect(":memory:")
    db.init_db(conn)
    now = "2026-06-18T00:00:00+00:00"
    # Two players in our board: one matchable by espn_id, one not in ESPN's list.
    conn.executemany(
        """INSERT INTO players(player_id, espn_id, full_name, position, team,
                               search_rank, adp, active, updated_at)
           VALUES (?,?,?,?,?,?,?,1,?)""",
        [("rb_sleeper", "1001", "Real RB", "RB", "DAL", 5, 5.0, now),
         ("wr_only_placeholder", "9999", "Placeholder WR", "WR", "SF", 8, 8.0, now)],
    )
    conn.commit()

    # Monkeypatch the network fetch to return our synthetic payload.
    espn.fetch_player_projections = lambda *a, **k: _synthetic_payload()  # type: ignore
    summary = ingest.load_projections(conn, league_id="X", season=SEASON)
    assert summary["matched"] == 1, summary  # only espn_id 1001 is on our board

    players = {p.player_id: p for p in board.load_engine_players(conn)}
    rb = players["rb_sleeper"]
    wr = players["wr_only_placeholder"]
    assert rb.proj_source == "espn" and rb.proj_points == 305.4, rb
    assert wr.proj_source == "placeholder", wr   # fell back, no ESPN match

    cov = board.projection_coverage(conn)
    assert cov == {"total": 2, "real": 1, "placeholder": 1}, cov
    conn.close()


ALL_TESTS = [
    test_parse_picks_projection_not_actual,
    test_projection_fallback_when_season_mismatch,
    test_ingest_joins_on_espn_id_and_board_prefers_real,
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
