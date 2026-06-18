"""Sleeper API client (no auth, read-only).

The full player DB is large (~5MB / ~11k players), so it is cached to disk and
only re-fetched when stale (>24h) or when force-refreshed. Sleeper does not
expose a public global-ADP endpoint, so we use each player's `search_rank`
(Sleeper's overall ranking) as a clean, real ADP proxy. A richer ADP source can
be plugged in later without schema changes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from .. import config

PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
TRENDING_URL = "https://api.sleeper.app/v1/players/nfl/trending/{kind}"
STATE_URL = "https://api.sleeper.app/v1/state/nfl"

PLAYERS_CACHE: Path = config.DATA_CACHE_DIR / "sleeper_players.json"
DEFAULT_REFRESH_SECONDS = 24 * 3600

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}


def _cache_age_seconds(path: Path) -> float | None:
    if not path.exists():
        return None
    return time.time() - path.stat().st_mtime


def fetch_players(force: bool = False, max_age_seconds: int = DEFAULT_REFRESH_SECONDS):
    """Return (players_dict, from_network). Cached to disk; refreshed when stale."""
    age = _cache_age_seconds(PLAYERS_CACHE)
    if age is not None and not force and age < max_age_seconds:
        data = json.loads(PLAYERS_CACHE.read_text(encoding="utf-8"))
        return data, False

    with httpx.Client(timeout=120.0) as client:
        resp = client.get(PLAYERS_URL)
        resp.raise_for_status()
        data = resp.json()

    PLAYERS_CACHE.write_text(json.dumps(data), encoding="utf-8")
    return data, True


def fetch_trending(kind: str = "add", lookback_hours: int = 24, limit: int = 50):
    """Trending adds/drops. kind = 'add' or 'drop'. Returns list of
    {player_id, count}."""
    url = TRENDING_URL.format(kind=kind)
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(
            url, params={"lookback_hours": lookback_hours, "limit": limit}
        )
        resp.raise_for_status()
        return resp.json()


def fetch_state():
    """NFL state (current week/season). Useful for staleness + scheduling."""
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(STATE_URL)
        resp.raise_for_status()
        return resp.json()


def is_fantasy_relevant(player: dict) -> bool:
    """Keep only draftable fantasy positions with a team or a search rank."""
    pos = player.get("position")
    fpos = set(player.get("fantasy_positions") or [])
    if pos not in FANTASY_POSITIONS and not (fpos & FANTASY_POSITIONS):
        return False
    # Drop clearly inactive players with no ranking signal at all.
    if player.get("search_rank") is None and not player.get("team"):
        return False
    return True
