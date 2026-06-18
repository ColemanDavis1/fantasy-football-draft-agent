"""Runtime configuration.

Nothing about a specific league is baked in. The league ID, season, optional
private-league cookies, and "which team is me" all come from the environment
(.env) or CLI flags at run time. This keeps the tool league-agnostic and
open-sourceable: clone it, point it at any league on draft day.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# Resolve key paths relative to this file so it works from any CWD.
BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent

# Load .env from the repo root if present. Real env vars win over .env.
load_dotenv(REPO_ROOT / ".env", override=False)

DATA_CACHE_DIR = BACKEND_DIR / "data_cache"
DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("DRAFT_DB_PATH", str(BACKEND_DIR / "draft.db")))


def current_nfl_season() -> int:
    """The NFL season year. Drafts happen late summer; before September the
    'current' season is still this calendar year."""
    return date.today().year


@dataclass
class LeagueConfig:
    """Everything needed to talk to one ESPN league. All optional until you
    actually want to read a league (i.e. on draft day)."""

    league_id: str | None
    season: int
    swid: str | None
    espn_s2: str | None
    my_team_id: int | None

    @property
    def is_private(self) -> bool:
        return bool(self.swid and self.espn_s2)

    @property
    def has_league(self) -> bool:
        return bool(self.league_id)


def load_league_config(
    league_id: str | None = None,
    season: int | None = None,
    swid: str | None = None,
    espn_s2: str | None = None,
    my_team_id: int | None = None,
) -> LeagueConfig:
    """Merge CLI args (highest priority) over environment/.env values."""
    league_id = league_id or os.getenv("ESPN_LEAGUE_ID") or None

    if season is None:
        env_season = os.getenv("ESPN_SEASON")
        season = int(env_season) if env_season else current_nfl_season()

    swid = swid or os.getenv("ESPN_SWID") or None
    espn_s2 = espn_s2 or os.getenv("ESPN_S2") or None

    if my_team_id is None:
        env_team = os.getenv("MY_TEAM_ID")
        my_team_id = int(env_team) if env_team else None

    return LeagueConfig(
        league_id=league_id,
        season=season,
        swid=swid,
        espn_s2=espn_s2,
        my_team_id=my_team_id,
    )


def anthropic_api_key() -> str | None:
    """Read the Anthropic key from env only. Never hardcoded."""
    return os.getenv("ANTHROPIC_API_KEY") or None
