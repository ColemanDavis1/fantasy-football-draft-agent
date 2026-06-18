"""SQLite layer: connection helper + schema.

Schema covers the whole build, not just Phase 1, so later phases don't require
migrations:
  - players            the draft board (from Sleeper, enriched later)
  - trending           Sleeper add/drop momentum
  - league_settings    auto-detected ESPN config (scoring, slots, draft type)
  - teams              every team in the league + draft slot
  - picks              every pick as the draft unfolds (Phase 3 live capture)
  - rosters            derived per-team roster (slot-aware)
  - opponent_profiles  per-team tendency profile (Phase 2/3)
  - meta               key/value (cache timestamps, schema version, etc.)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS players (
    player_id        TEXT PRIMARY KEY,      -- Sleeper player_id (or team code for DEF)
    espn_id          TEXT,
    full_name        TEXT,
    first_name       TEXT,
    last_name        TEXT,
    position         TEXT,                  -- QB/RB/WR/TE/K/DEF
    fantasy_positions TEXT,                 -- JSON list of eligible positions
    team             TEXT,                  -- NFL team abbrev (Sleeper convention)
    age              INTEGER,
    years_exp        INTEGER,
    injury_status    TEXT,
    bye_week         INTEGER,
    search_rank      INTEGER,               -- Sleeper overall rank (ADP proxy)
    adp              REAL,                   -- best available ADP (defaults to search_rank)
    proj_points      REAL,                   -- season projection (ESPN kona, league-scored)
    proj_source      TEXT,                   -- 'espn' / 'placeholder' / NULL
    proj_updated_at  TEXT,
    enrichment_json  TEXT,                   -- Phase 4 Claude pass: notes + sleeper/bust flags
    active           INTEGER DEFAULT 1,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_players_pos ON players(position);
CREATE INDEX IF NOT EXISTS idx_players_rank ON players(search_rank);

CREATE TABLE IF NOT EXISTS trending (
    player_id   TEXT,
    type        TEXT,                       -- 'add' or 'drop'
    count       INTEGER,
    captured_at TEXT,
    PRIMARY KEY (player_id, type)
);

CREATE TABLE IF NOT EXISTS league_settings (
    league_id     TEXT,
    season        INTEGER,
    name          TEXT,
    num_teams     INTEGER,
    scoring_type  TEXT,                     -- standard / half_ppr / ppr
    ppr_value     REAL,
    is_superflex  INTEGER,
    draft_type    TEXT,                     -- SNAKE / AUCTION / etc.
    roster_slots  TEXT,                     -- JSON: {slot_name: count}
    starter_slots TEXT,                     -- JSON: {slot_name: count} (excludes BE/IR)
    scoring_json  TEXT,                     -- JSON: full parsed scoring map
    pick_order    TEXT,                     -- JSON: list of team_ids in draft-slot order
    raw_json      TEXT,                     -- full ESPN payload for debugging
    updated_at    TEXT,
    PRIMARY KEY (league_id, season)
);

CREATE TABLE IF NOT EXISTS teams (
    league_id    TEXT,
    season       INTEGER,
    team_id      INTEGER,
    name         TEXT,
    abbrev       TEXT,
    owner        TEXT,
    draft_slot   INTEGER,                   -- 1-based position in round 1
    is_me        INTEGER DEFAULT 0,
    PRIMARY KEY (league_id, season, team_id)
);

CREATE TABLE IF NOT EXISTS picks (
    league_id     TEXT,
    season        INTEGER,
    overall_pick  INTEGER,
    round         INTEGER,
    pick_in_round INTEGER,
    team_id       INTEGER,
    player_id     TEXT,
    source        TEXT,                     -- 'websocket' / 'dom' / 'manual'
    captured_at   TEXT,
    PRIMARY KEY (league_id, season, overall_pick)
);

CREATE TABLE IF NOT EXISTS rosters (
    league_id  TEXT,
    season     INTEGER,
    team_id    INTEGER,
    player_id  TEXT,
    slot       TEXT,                        -- assigned lineup slot (or BENCH)
    PRIMARY KEY (league_id, season, team_id, player_id)
);

CREATE TABLE IF NOT EXISTS opponent_profiles (
    league_id    TEXT,
    season       INTEGER,
    team_id      INTEGER,
    archetype    TEXT,
    profile_json TEXT,                      -- JSON: counts, adp_deviation, runs, needs
    updated_at   TEXT,
    PRIMARY KEY (league_id, season, team_id)
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI runs sync endpoints in a threadpool and
    # this is a single-user local server, so cross-thread reuse is safe here.
    conn = sqlite3.connect(db_path or config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# Columns added after v1. ALTER TABLE ADD COLUMN is the only migration SQLite
# needs here (all are nullable adds), so we apply them idempotently rather than
# forcing a DB rebuild.
_MIGRATIONS = {
    "players": {
        "proj_points": "REAL",
        "proj_source": "TEXT",
        "proj_updated_at": "TEXT",
        "enrichment_json": "TEXT",
    },
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _apply_migrations(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None
