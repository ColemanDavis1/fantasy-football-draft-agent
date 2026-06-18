"""Pure-function tests for the Phase-4 enrichment pass (no network/LLM).

Covers the two risky non-network paths: extracting the JSON object from a batch
message (structured output, with a prose-wrapped fallback) and the DB join that
lands enrichment on the board.

    python -m pytest backend/tests/test_enrich.py
    python backend/tests/test_enrich.py
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import board, db, enrich  # noqa: E402


def _msg(text: str):
    """Mimic an anthropic message: .content list of blocks with .type/.text."""
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def test_extract_clean_json():
    payload = {"note": "Camp standout, locked into WR1 reps.",
               "flag": "sleeper", "confidence": "medium"}
    out = enrich._extract(_msg(json.dumps(payload)))
    assert out == payload, out


def test_extract_handles_prose_wrapped_json():
    text = 'Here is the read:\n{"note": "Aging, role shrinking.", ' \
           '"flag": "bust", "confidence": "high"}\nThanks!'
    out = enrich._extract(_msg(text))
    assert out["flag"] == "bust" and out["confidence"] == "high", out


def test_extract_returns_none_on_garbage():
    assert enrich._extract(_msg("no json here at all")) is None


def test_coverage_and_board_carry_enrichment():
    conn = db.connect(":memory:")
    db.init_db(conn)
    now = "2026-06-18T00:00:00+00:00"
    enr = json.dumps({"note": "Sleeper RB2 with standalone value.",
                      "flag": "sleeper", "confidence": "medium"})
    conn.executemany(
        """INSERT INTO players(player_id, full_name, position, team, search_rank,
                               adp, enrichment_json, active, updated_at)
           VALUES (?,?,?,?,?,?,?,1,?)""",
        [("rb_enriched", "Enriched RB", "RB", "DAL", 5, 5.0, enr, now),
         ("rb_plain", "Plain RB", "RB", "SF", 8, 8.0, None, now)],
    )
    conn.commit()

    cov = enrich.coverage(conn)
    assert cov == {"total": 2, "enriched": 1}, cov

    players = {p.player_id: p for p in board.load_engine_players(conn)}
    assert players["rb_enriched"].enrichment["flag"] == "sleeper"
    assert players["rb_plain"].enrichment is None
    conn.close()


ALL_TESTS = [
    test_extract_clean_json,
    test_extract_handles_prose_wrapped_json,
    test_extract_returns_none_on_garbage,
    test_coverage_and_board_carry_enrichment,
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
