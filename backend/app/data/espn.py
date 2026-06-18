"""ESPN fantasy read-API client + config parser.

Reads league configuration directly from ESPN's undocumented read API so you
never type scoring/roster/team settings by hand. Works for public leagues with
no auth; private leagues need the SWID + espn_s2 cookies from your logged-in
browser session (you supply them, we never log in for you).

IMPORTANT: this read API is for CONFIG and COMPLETED drafts only. Live picks do
NOT appear here until the draft ends — that is Phase 3's websocket/DOM job.
"""

from __future__ import annotations

import json

import httpx

READ_HOST = "https://lm-api-reads.fantasy.espn.com"
LEAGUE_URL = READ_HOST + "/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}"
SEASON_URL = READ_HOST + "/apis/v3/games/ffl/seasons/{season}"

# ESPN lineup slot id -> human name. Starter slots are everything except BE/IR.
LINEUP_SLOT_NAMES = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE",
    7: "OP", 8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S",
    14: "DB", 15: "DP", 16: "D/ST", 17: "K", 18: "P", 19: "HC",
    20: "BE", 21: "IR", 23: "FLEX", 24: "ER", 25: "Rookie",
}
NON_STARTER_SLOTS = {"BE", "IR"}
# Slots that can hold a QB → used to detect superflex/2QB formats.
QB_CAPABLE_SLOTS = {"QB", "OP", "TQB"}

# Reception stat id in ESPN scoring → distinguishes PPR / half / standard.
RECEPTION_STAT_ID = 53

# ESPN stat-block discriminators (used to pick the right projection).
STAT_SOURCE_ACTUAL = 0      # statSourceId: real, in-season results
STAT_SOURCE_PROJECTED = 1   # statSourceId: preseason/in-season projection
STAT_SPLIT_SEASON = 0       # statSplitTypeId: full-season total (not weekly)

# ESPN defaultPositionId → our position code (offense + D/ST only).
ESPN_POSITION_BY_ID = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}

# ESPN pro-team id -> abbrev (Sleeper convention) for bye-week mapping.
# Filled from the season endpoint at runtime; this static map is a fallback.
PRO_TEAM_ABBREV = {
    1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR",
    15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ",
    21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA",
    27: "TB", 28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}


def _client(swid: str | None, espn_s2: str | None) -> httpx.Client:
    cookies = {}
    if swid and espn_s2:
        # SWID must be brace-wrapped; tolerate either input form.
        cookies["SWID"] = swid if swid.startswith("{") else "{" + swid + "}"
        cookies["espn_s2"] = espn_s2
    headers = {"User-Agent": "Mozilla/5.0 (fantasy-draft-agent)"}
    return httpx.Client(timeout=30.0, cookies=cookies, headers=headers)


def fetch_league(league_id: str, season: int, views: list[str],
                 swid: str | None = None, espn_s2: str | None = None) -> dict:
    """Fetch one or more ESPN views (e.g. ['mSettings', 'mTeam'])."""
    url = LEAGUE_URL.format(season=season, league_id=league_id)
    params = [("view", v) for v in views]
    with _client(swid, espn_s2) as client:
        resp = client.get(url, params=params)
        if resp.status_code == 401:
            raise PermissionError(
                "ESPN returned 401 — this league is private. Supply ESPN_SWID "
                "and ESPN_S2 cookies from your logged-in browser session."
            )
        resp.raise_for_status()
        return resp.json()


def fetch_bye_weeks(season: int) -> dict[str, int]:
    """Return {team_abbrev: bye_week} from ESPN's pro-team schedule view."""
    url = SEASON_URL.format(season=season)
    with _client(None, None) as client:
        resp = client.get(url, params={"view": "proTeamSchedules_wl"})
        resp.raise_for_status()
        data = resp.json()
    byes: dict[str, int] = {}
    pro_teams = (data.get("settings") or {}).get("proTeams") or []
    for t in pro_teams:
        abbrev = (t.get("abbrev") or PRO_TEAM_ABBREV.get(t.get("id"), "")).upper()
        bye = t.get("byeWeek")
        if abbrev and bye:
            byes[abbrev] = bye
    return byes


def fetch_player_projections(
    league_id: str, season: int, swid: str | None = None,
    espn_s2: str | None = None, limit: int = 1500,
) -> dict:
    """Fetch the `kona_player_info` view for a league.

    Queried at the LEAGUE endpoint (not the generic players endpoint) on purpose:
    ESPN computes each projection's `appliedTotal` using THIS league's scoring
    rules, so PPR / half / standard / superflex are baked in for free — exactly
    the scoring-format awareness the engine needs. Selection + paging is driven
    by the `x-fantasy-filter` header, the way ESPN's own client does it.
    """
    url = LEAGUE_URL.format(season=season, league_id=league_id)
    # Sort by projected season points (descending) and take the top `limit`.
    flt = {
        "players": {
            "limit": limit,
            "offset": 0,
            "sortAppliedStatTotal": {
                "sortAsc": False, "sortPriority": 1, "value": None,
            },
            "filterStatsForExternalIds": {"value": [season]},
        }
    }
    with _client(swid, espn_s2) as client:
        resp = client.get(
            url,
            params=[("view", "kona_player_info")],
            headers={"x-fantasy-filter": json.dumps(flt)},
        )
        if resp.status_code == 401:
            raise PermissionError(
                "ESPN returned 401 — this league is private. Supply ESPN_SWID "
                "and ESPN_S2 cookies from your logged-in browser session."
            )
        resp.raise_for_status()
        return resp.json()


def _projection_total(stats: list[dict], season: int) -> float | None:
    """Pick the season-total PROJECTION from a player's stat blocks.

    Prefers an exact season match; otherwise accepts any projected season total
    (handles the preseason window where ESPN may key the projection slightly
    differently). Returns None if the player has no projection at all.
    """
    fallback: float | None = None
    for s in stats or []:
        if s.get("statSourceId") != STAT_SOURCE_PROJECTED:
            continue
        if s.get("statSplitTypeId") != STAT_SPLIT_SEASON:
            continue
        total = s.get("appliedTotal")
        if total is None:
            continue
        if s.get("seasonId") == season:
            return float(total)
        fallback = float(total)
    return fallback


def parse_projections(payload: dict, season: int) -> dict[str, dict]:
    """Map ESPN player id (str) -> {points, position, name} from a
    kona_player_info payload. Keyed by ESPN id because that is the join key to
    our Sleeper-sourced players (we store each player's espn_id).
    """
    out: dict[str, dict] = {}
    for item in payload.get("players") or []:
        # Items wrap the player under "player"; tolerate a flat shape too.
        player = item.get("player") if isinstance(item.get("player"), dict) else item
        espn_id = player.get("id") or item.get("id")
        if espn_id is None:
            continue
        points = _projection_total(player.get("stats") or [], season)
        if points is None:
            continue
        out[str(espn_id)] = {
            "points": round(points, 1),
            "position": ESPN_POSITION_BY_ID.get(player.get("defaultPositionId")),
            "name": player.get("fullName"),
        }
    return out


def parse_player_positions(payload: dict) -> dict[str, str]:
    """Map ESPN player id (str) -> position from a kona_player_info payload.
    Unlike parse_projections this keeps every player (no stats requirement),
    so it works on a completed past season for seeding opponent priors."""
    out: dict[str, str] = {}
    for item in payload.get("players") or []:
        player = item.get("player") if isinstance(item.get("player"), dict) else item
        espn_id = player.get("id") or item.get("id")
        pos = ESPN_POSITION_BY_ID.get(player.get("defaultPositionId"))
        if espn_id is not None and pos:
            out[str(espn_id)] = pos
    return out


def fetch_player_positions(league_id: str, season: int, swid: str | None = None,
                           espn_s2: str | None = None) -> dict[str, str]:
    """espn_id -> position for a season, via the kona_player_info view."""
    payload = fetch_player_projections(league_id, season, swid=swid,
                                       espn_s2=espn_s2, limit=2000)
    return parse_player_positions(payload)


def fetch_draft_detail(league_id: str, season: int, swid: str | None = None,
                       espn_s2: str | None = None) -> dict:
    """Fetch a COMPLETED draft's recap (mDraftDetail) + teams (mTeam).

    mDraftDetail only populates for finished drafts — exactly the past-season
    case we want for seeding opponent priors. mTeam comes along so we can map
    each pick's teamId to the manager (owner) who made it."""
    return fetch_league(league_id, season, ["mDraftDetail", "mTeam"],
                        swid=swid, espn_s2=espn_s2)


def parse_draft_picks(payload: dict) -> list[dict]:
    """Return draft picks as
    [{overall, round, pick_in_round, team_id, espn_player_id, keeper}]."""
    detail = payload.get("draftDetail") or {}
    picks = []
    for p in detail.get("picks") or []:
        picks.append({
            "overall": p.get("overallPickNumber"),
            "round": p.get("roundId"),
            "pick_in_round": p.get("roundPickNumber"),
            "team_id": p.get("teamId"),
            "espn_player_id": str(p.get("playerId")) if p.get("playerId") is not None else None,
            "keeper": bool(p.get("keeper")),
        })
    picks.sort(key=lambda x: (x["overall"] is None, x["overall"] or 0))
    return picks


def team_owner_map(payload: dict) -> dict[int, str]:
    """team_id -> stable owner id (ESPN member GUID). Lets us follow a manager
    across seasons even when their team_id changes."""
    out: dict[int, str] = {}
    for t in payload.get("teams") or []:
        owners = t.get("owners") or ([t.get("primaryOwner")] if t.get("primaryOwner") else [])
        owner = next((o for o in owners if o), None)
        if t.get("id") is not None and owner:
            out[t["id"]] = owner
    return out


def member_name_map(payload: dict) -> dict[str, str]:
    """owner id -> display name."""
    return {m.get("id"): m.get("displayName")
            for m in (payload.get("members") or []) if m.get("id")}


def _detect_scoring(settings: dict) -> tuple[str, float]:
    """Return (scoring_type, ppr_value) from the reception scoring item."""
    scoring_items = (settings.get("scoringSettings") or {}).get("scoringItems") or []
    ppr = 0.0
    for item in scoring_items:
        if item.get("statId") == RECEPTION_STAT_ID:
            if item.get("points") is not None:
                ppr = float(item["points"])
            else:
                overrides = item.get("pointsOverrides") or {}
                if overrides:
                    ppr = float(next(iter(overrides.values())))
            break
    if ppr >= 1.0:
        return "ppr", ppr
    if ppr > 0:
        return "half_ppr", ppr
    return "standard", 0.0


def _detect_roster_slots(settings: dict) -> tuple[dict[str, int], dict[str, int]]:
    """Return (all_slots, starter_slots) as {slot_name: count}."""
    counts = (settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    all_slots: dict[str, int] = {}
    starters: dict[str, int] = {}
    for slot_id, count in counts.items():
        if not count:
            continue
        name = LINEUP_SLOT_NAMES.get(int(slot_id), f"SLOT_{slot_id}")
        all_slots[name] = all_slots.get(name, 0) + int(count)
        if name not in NON_STARTER_SLOTS:
            starters[name] = starters.get(name, 0) + int(count)
    return all_slots, starters


def _detect_superflex(starter_slots: dict[str, int]) -> bool:
    qb_capable = sum(c for s, c in starter_slots.items() if s in QB_CAPABLE_SLOTS)
    return qb_capable >= 2


def _team_name(team: dict) -> str:
    if team.get("name"):
        return team["name"]
    loc = team.get("location", "") or ""
    nick = team.get("nickname", "") or ""
    full = f"{loc} {nick}".strip()
    return full or team.get("abbrev") or f"Team {team.get('id')}"


def parse_league_config(payload: dict, my_team_id: int | None = None) -> dict:
    """Turn a merged mSettings+mTeam payload into a clean config dict."""
    settings = payload.get("settings") or {}
    draft_settings = settings.get("draftSettings") or {}

    scoring_type, ppr_value = _detect_scoring(settings)
    all_slots, starter_slots = _detect_roster_slots(settings)

    teams = []
    member_names = {
        m.get("id"): m.get("displayName")
        for m in (payload.get("members") or [])
    }
    pick_order = draft_settings.get("pickOrder") or []
    for t in payload.get("teams") or []:
        tid = t.get("id")
        owner_ids = t.get("owners") or ([t.get("primaryOwner")] if t.get("primaryOwner") else [])
        owner = next((member_names.get(o) for o in owner_ids if member_names.get(o)), None)
        draft_slot = (pick_order.index(tid) + 1) if tid in pick_order else None
        teams.append({
            "team_id": tid,
            "name": _team_name(t),
            "abbrev": t.get("abbrev"),
            "owner": owner,
            "draft_slot": draft_slot,
            "is_me": (tid == my_team_id),
        })
    teams.sort(key=lambda x: (x["draft_slot"] is None, x["draft_slot"] or 0))

    return {
        "league_id": str(payload.get("id")),
        "season": payload.get("seasonId"),
        "name": settings.get("name"),
        "num_teams": settings.get("size") or len(teams),
        "scoring_type": scoring_type,
        "ppr_value": ppr_value,
        "is_superflex": _detect_superflex(starter_slots),
        "draft_type": draft_settings.get("type"),
        "roster_slots": all_slots,
        "starter_slots": starter_slots,
        "pick_order": pick_order,
        "teams": teams,
    }


def get_league_config(league_id: str, season: int, swid: str | None = None,
                      espn_s2: str | None = None, my_team_id: int | None = None) -> dict:
    """Fetch mSettings + mTeam and return a parsed config dict."""
    payload = fetch_league(
        league_id, season, ["mSettings", "mTeam"], swid=swid, espn_s2=espn_s2
    )
    config = parse_league_config(payload, my_team_id=my_team_id)
    config["raw"] = payload
    return config
