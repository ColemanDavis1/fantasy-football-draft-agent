"""Phase 4 historical opponent priors (seeded from past drafts).

For a returning league, past drafts ARE available from ESPN's mDraftDetail view
(it only fails for the live draft). We pull prior seasons, follow each manager by
their stable owner id, and derive a tendency prior — draft archetype, positional
priority, position mix — then attach it to that manager's CURRENT team. Live
picks refine these as the draft unfolds (the profiler runs every pick); the prior
is just the pre-draft seed so opponents aren't blank slates on pick 1.

Positions for historical picks come from that season's kona_player_info map, with
the current Sleeper-sourced board as a fallback.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import config, db
from .data import espn

RB = {"RB"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _archetype_from_positions(order: list[str]) -> str:
    """Same logic as the live profiler's archetype, on a list of position
    codes in draft order (so priors and live profiles speak one language)."""
    if len(order) < 2:
        return "Undeclared"
    first3, first4 = order[:3], order[:4]
    rb3 = sum(1 for p in first3 if p in RB)
    rb4 = sum(1 for p in first4 if p in RB)
    if rb4 == 0:
        return "Zero-RB"
    if rb3 >= 2:
        return "Robust-RB"
    if rb3 == 1 and order[0] in RB:
        return "Hero-RB"
    return "Balanced/BPA"


def _current_owner_to_team(conn: sqlite3.Connection, league_id: str,
                           season: int) -> dict[str, int]:
    """owner id -> CURRENT team_id, from the saved league payload."""
    row = conn.execute(
        "SELECT raw_json FROM league_settings WHERE league_id=? AND season=?",
        (league_id, season)).fetchone()
    if not row or not row["raw_json"]:
        return {}
    payload = json.loads(row["raw_json"])
    return {owner: tid for tid, owner in espn.team_owner_map(payload).items()}


def _current_positions(conn: sqlite3.Connection) -> dict[str, str]:
    """espn_id -> position from the current board (fallback position lookup)."""
    rows = conn.execute(
        "SELECT espn_id, position FROM players WHERE espn_id IS NOT NULL").fetchall()
    return {r["espn_id"]: r["position"] for r in rows if r["position"]}


def build_priors(conn: sqlite3.Connection, league_id: str, season: int,
                 prior_seasons: list[int], prior_league_id: str | None = None,
                 swid: str | None = None, espn_s2: str | None = None) -> dict:
    """Pull prior-season drafts, derive per-owner tendency priors, and write
    them onto the matching current teams in opponent_profiles."""
    prior_league_id = prior_league_id or league_id
    fallback_pos = _current_positions(conn)

    # owner_id -> aggregate accumulators across all prior seasons
    archetypes: dict[str, list[str]] = defaultdict(list)
    pos_counts: dict[str, Counter] = defaultdict(Counter)
    first_round: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    owner_names: dict[str, str] = {}
    seasons_used: list[int] = []

    for yr in prior_seasons:
        try:
            payload = espn.fetch_draft_detail(prior_league_id, yr,
                                              swid=swid, espn_s2=espn_s2)
        except Exception:
            continue
        picks = espn.parse_draft_picks(payload)
        if not picks:
            continue
        seasons_used.append(yr)
        owners = espn.team_owner_map(payload)
        owner_names.update(espn.member_name_map(payload))
        try:
            pos_map = espn.fetch_player_positions(prior_league_id, yr,
                                                  swid=swid, espn_s2=espn_s2)
        except Exception:
            pos_map = {}

        # Group this season's non-keeper picks by owner, in draft order.
        by_owner: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for p in picks:
            if p["keeper"] or p["espn_player_id"] is None:
                continue
            owner = owners.get(p["team_id"])
            pos = pos_map.get(p["espn_player_id"]) or fallback_pos.get(p["espn_player_id"])
            if owner and pos:
                by_owner[owner].append((p["round"] or 99, pos))

        for owner, plist in by_owner.items():
            plist.sort(key=lambda x: x[0])
            order = [pos for _, pos in plist]
            archetypes[owner].append(_archetype_from_positions(order))
            for rnd, pos in plist:
                pos_counts[owner][pos] += 1
                first_round[owner][pos].append(rnd)

    if not seasons_used:
        return {"seasons_used": [], "owners": 0, "written": 0}

    owner_to_team = _current_owner_to_team(conn, league_id, season)
    now = _now()
    written = 0
    for owner, arch_list in archetypes.items():
        total = sum(pos_counts[owner].values()) or 1
        share = {pos: round(c / total, 2) for pos, c in pos_counts[owner].items()}
        avg_first = {pos: round(sum(rs) / len(rs), 1)
                     for pos, rs in first_round[owner].items()}
        modal = Counter(arch_list).most_common(1)[0][0]
        prior = {
            "source": "historical",
            "name": owner_names.get(owner),
            "seasons_used": seasons_used,
            "drafts": len(arch_list),
            "archetype_distribution": dict(Counter(arch_list)),
            "position_share": share,
            "avg_first_round_by_pos": avg_first,
        }
        team_id = owner_to_team.get(owner)
        if team_id is None:
            continue  # manager not in the current league
        conn.execute(
            """INSERT INTO opponent_profiles
                   (league_id, season, team_id, archetype, profile_json, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(league_id, season, team_id) DO UPDATE SET
                   archetype=excluded.archetype,
                   profile_json=excluded.profile_json,
                   updated_at=excluded.updated_at""",
            (league_id, season, team_id, modal, json.dumps(prior), now),
        )
        written += 1
    db.set_meta(conn, "priors_last_built_at", now)
    conn.commit()
    return {"seasons_used": seasons_used, "owners": len(archetypes),
            "written": written}


def load_priors(conn: sqlite3.Connection, league_id: str | None,
                season: int | None) -> dict[int, dict]:
    """team_id -> prior dict for the current league (empty if none built)."""
    if not league_id:
        return {}
    rows = conn.execute(
        "SELECT team_id, archetype, profile_json FROM opponent_profiles "
        "WHERE league_id=? AND season=?", (league_id, season)).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        try:
            prof = json.loads(r["profile_json"]) if r["profile_json"] else {}
        except (json.JSONDecodeError, TypeError):
            prof = {}
        if prof.get("source") == "historical":
            prof["archetype"] = r["archetype"]
            out[r["team_id"]] = prof
    return out
