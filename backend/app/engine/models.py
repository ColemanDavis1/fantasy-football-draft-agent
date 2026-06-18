"""In-memory data model for the engine. Plain dataclasses, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Player:
    player_id: str
    name: str
    position: str                 # QB/RB/WR/TE/K/DEF
    team: str | None = None       # NFL team abbrev
    bye_week: int | None = None
    adp: float = 999.0            # lower = drafted earlier
    proj_points: float = 0.0      # season projection (real or fallback)
    # computed by the engine:
    vorp: float | None = None
    tier: int | None = None       # per-position tier (1 = best)


@dataclass
class LeagueSettings:
    num_teams: int
    starter_slots: dict[str, int]  # slot name -> count per team (excludes BE/IR)
    roster_size: int = 16
    scoring_type: str = "ppr"
    is_superflex: bool = False


@dataclass
class Pick:
    overall: int
    team_id: int
    player_id: str


@dataclass
class DraftState:
    league: LeagueSettings
    draft_order: list[int]         # team_ids in draft-slot order (round 1)
    picks: list[Pick]              # picks made so far, in order
    my_team_id: int
    players_by_id: dict[str, Player] = field(default_factory=dict)

    @property
    def num_teams(self) -> int:
        return self.league.num_teams

    @property
    def current_overall(self) -> int:
        """The pick about to be made (1-based)."""
        return len(self.picks) + 1

    @property
    def drafted_ids(self) -> set[str]:
        return {p.player_id for p in self.picks}

    def available(self) -> list[Player]:
        drafted = self.drafted_ids
        return [p for p in self.players_by_id.values() if p.player_id not in drafted]

    def roster_player_ids(self, team_id: int) -> list[str]:
        return [p.player_id for p in self.picks if p.team_id == team_id]

    def roster(self, team_id: int) -> list[Player]:
        return [self.players_by_id[p.player_id]
                for p in self.picks if p.team_id == team_id
                and p.player_id in self.players_by_id]
