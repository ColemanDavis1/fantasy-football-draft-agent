"""Load external data into SQLite.

- Sleeper players + trending  -> players / trending tables
- ESPN league config          -> league_settings / teams tables

All idempotent: safe to re-run any time you want fresh data (it upserts).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from . import db
from .data import espn, sleeper


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_players(conn: sqlite3.Connection, force: bool = False) -> dict:
    """Refresh the Sleeper player DB into SQLite. Returns a small summary."""
    players, from_network = sleeper.fetch_players(force=force)
    byes = {}
    try:
        byes = espn.fetch_bye_weeks(sleeper_season_guess())
    except Exception:
        # Bye weeks are a nice-to-have; never block player load on them.
        byes = {}

    now = _now()
    rows = []
    kept = 0
    for pid, p in players.items():
        if not isinstance(p, dict) or not sleeper.is_fantasy_relevant(p):
            continue
        kept += 1
        team = (p.get("team") or "").upper() or None
        search_rank = p.get("search_rank")
        rows.append((
            pid,
            str(p.get("espn_id")) if p.get("espn_id") else None,
            p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip(),
            p.get("first_name"),
            p.get("last_name"),
            p.get("position"),
            json.dumps(p.get("fantasy_positions") or []),
            team,
            p.get("age"),
            p.get("years_exp"),
            p.get("injury_status"),
            byes.get(team),
            search_rank,
            float(search_rank) if search_rank is not None else None,  # adp proxy
            1 if p.get("active", True) else 0,
            now,
        ))

    conn.executemany(
        """
        INSERT INTO players (
            player_id, espn_id, full_name, first_name, last_name, position,
            fantasy_positions, team, age, years_exp, injury_status, bye_week,
            search_rank, adp, active, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(player_id) DO UPDATE SET
            espn_id=excluded.espn_id, full_name=excluded.full_name,
            first_name=excluded.first_name, last_name=excluded.last_name,
            position=excluded.position, fantasy_positions=excluded.fantasy_positions,
            team=excluded.team, age=excluded.age, years_exp=excluded.years_exp,
            injury_status=excluded.injury_status, bye_week=excluded.bye_week,
            search_rank=excluded.search_rank, adp=excluded.adp,
            active=excluded.active, updated_at=excluded.updated_at
        """,
        rows,
    )
    db.set_meta(conn, "sleeper_players_last_refresh", now)
    conn.commit()
    return {"fetched_from_network": from_network, "total": len(players),
            "kept_fantasy": kept, "bye_weeks_loaded": len(byes)}


def load_projections(
    conn: sqlite3.Connection, league_id: str, season: int,
    swid: str | None = None, espn_s2: str | None = None,
) -> dict:
    """Pull real season projections from ESPN (kona_player_info, league-scored)
    and write them onto the players we already have, joined by espn_id.

    Players we can't match (no espn_id, or absent from ESPN's list) keep
    proj_source NULL and fall back to the rank-based placeholder in the engine.
    """
    payload = espn.fetch_player_projections(league_id, season, swid=swid, espn_s2=espn_s2)
    projections = espn.parse_projections(payload, season)
    if not projections:
        return {"espn_players": 0, "matched": 0, "unmatched": 0}

    now = _now()
    # Build espn_id -> player_id from our table (one ESPN id maps to one player).
    id_rows = conn.execute(
        "SELECT player_id, espn_id FROM players WHERE espn_id IS NOT NULL"
    ).fetchall()
    pid_by_espn = {r["espn_id"]: r["player_id"] for r in id_rows}

    matched = 0
    updates = []
    for espn_id, proj in projections.items():
        pid = pid_by_espn.get(espn_id)
        if pid is None:
            continue
        matched += 1
        updates.append((proj["points"], "espn", now, pid))
    conn.executemany(
        "UPDATE players SET proj_points=?, proj_source=?, proj_updated_at=? "
        "WHERE player_id=?",
        updates,
    )
    db.set_meta(conn, "espn_projections_last_refresh", now)
    db.set_meta(conn, "espn_projections_season", str(season))
    conn.commit()
    return {"espn_players": len(projections), "matched": matched,
            "unmatched": len(projections) - matched}


def load_trending(conn: sqlite3.Connection, limit: int = 50) -> dict:
    now = _now()
    summary = {}
    for kind in ("add", "drop"):
        items = sleeper.fetch_trending(kind=kind, lookback_hours=24, limit=limit)
        rows = [(it.get("player_id"), kind, it.get("count"), now) for it in items]
        conn.executemany(
            """INSERT INTO trending (player_id, type, count, captured_at)
               VALUES (?,?,?,?)
               ON CONFLICT(player_id, type) DO UPDATE SET
                   count=excluded.count, captured_at=excluded.captured_at""",
            rows,
        )
        summary[kind] = len(rows)
    db.set_meta(conn, "sleeper_trending_last_refresh", now)
    conn.commit()
    return summary


def save_league_config(conn: sqlite3.Connection, cfg: dict) -> None:
    """Persist a parsed ESPN config dict into league_settings + teams."""
    now = _now()
    conn.execute(
        """
        INSERT INTO league_settings (
            league_id, season, name, num_teams, scoring_type, ppr_value,
            is_superflex, draft_type, roster_slots, starter_slots, scoring_json,
            pick_order, raw_json, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(league_id, season) DO UPDATE SET
            name=excluded.name, num_teams=excluded.num_teams,
            scoring_type=excluded.scoring_type, ppr_value=excluded.ppr_value,
            is_superflex=excluded.is_superflex, draft_type=excluded.draft_type,
            roster_slots=excluded.roster_slots, starter_slots=excluded.starter_slots,
            scoring_json=excluded.scoring_json, pick_order=excluded.pick_order,
            raw_json=excluded.raw_json, updated_at=excluded.updated_at
        """,
        (
            cfg["league_id"], cfg["season"], cfg.get("name"), cfg["num_teams"],
            cfg["scoring_type"], cfg["ppr_value"], 1 if cfg["is_superflex"] else 0,
            cfg.get("draft_type"), json.dumps(cfg["roster_slots"]),
            json.dumps(cfg["starter_slots"]), json.dumps(cfg.get("scoring", {})),
            json.dumps(cfg.get("pick_order", [])),
            json.dumps(cfg.get("raw", {})), now,
        ),
    )
    for t in cfg["teams"]:
        conn.execute(
            """
            INSERT INTO teams (league_id, season, team_id, name, abbrev, owner,
                               draft_slot, is_me)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(league_id, season, team_id) DO UPDATE SET
                name=excluded.name, abbrev=excluded.abbrev, owner=excluded.owner,
                draft_slot=excluded.draft_slot, is_me=excluded.is_me
            """,
            (cfg["league_id"], cfg["season"], t["team_id"], t["name"],
             t.get("abbrev"), t.get("owner"), t.get("draft_slot"),
             1 if t.get("is_me") else 0),
        )
    conn.commit()


def sleeper_season_guess() -> int:
    from . import config as _config
    return _config.current_nfl_season()
