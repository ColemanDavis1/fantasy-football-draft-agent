"""Bridge DB rows -> engine models. Shared by the CLI and the live server."""

from __future__ import annotations

import json
import sqlite3

from .engine import projections
from .engine.models import LeagueSettings, Player

DEFAULT_SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1}


def load_league_settings(conn: sqlite3.Connection) -> tuple[LeagueSettings, str, dict]:
    """Return (LeagueSettings, human_source, extras) where extras carries
    league_id/season/pick_order/my_team_id when a league has been saved."""
    row = conn.execute(
        "SELECT * FROM league_settings ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return (LeagueSettings(num_teams=12, starter_slots=DEFAULT_SLOTS,
                               scoring_type="ppr"),
                "default 12-team PPR (no league saved)", {})

    slots = json.loads(row["starter_slots"] or "{}") or DEFAULT_SLOTS
    pick_order = json.loads(row["pick_order"] or "[]")
    settings = LeagueSettings(
        num_teams=row["num_teams"] or 12,
        starter_slots=slots,
        scoring_type=row["scoring_type"] or "ppr",
        is_superflex=bool(row["is_superflex"]),
    )
    # Find my_team_id from the teams table (is_me flag), if set.
    me = conn.execute(
        "SELECT team_id FROM teams WHERE league_id=? AND season=? AND is_me=1",
        (row["league_id"], row["season"]),
    ).fetchone()
    extras = {
        "league_id": row["league_id"],
        "season": row["season"],
        "pick_order": pick_order,
        "my_team_id": me["team_id"] if me else None,
    }
    return settings, f"league {row['league_id']} ({row['scoring_type']})", extras


def load_engine_players(conn: sqlite3.Connection) -> list[Player]:
    """Build engine Player objects for the full pool, with placeholder
    rank-based projections (real projections arrive in Phase 4)."""
    rows = conn.execute(
        """SELECT player_id, full_name, position, team, bye_week, adp, search_rank
           FROM players WHERE active=1
             AND position IN ('QB','RB','WR','TE','K','DEF')"""
    ).fetchall()
    by_pos: dict[str, list] = {}
    for r in rows:
        by_pos.setdefault(r["position"], []).append(r)
    players: list[Player] = []
    for pos, plist in by_pos.items():
        plist.sort(key=lambda r: (r["search_rank"] is None,
                                  r["search_rank"] if r["search_rank"] is not None else 1e9))
        for rank0, r in enumerate(plist):
            players.append(Player(
                player_id=r["player_id"],
                name=r["full_name"] or r["player_id"],
                position=pos,
                team=r["team"],
                bye_week=r["bye_week"],
                adp=float(r["adp"]) if r["adp"] is not None else 999.0,
                proj_points=projections.project(pos, rank0),
            ))
    return players
