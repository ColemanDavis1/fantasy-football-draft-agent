"""Lineup-slot eligibility.

Maps ESPN lineup-slot names to the set of player positions that can fill them.
This is what lets the engine handle FLEX, superflex (OP), and combo slots
correctly when computing replacement baselines and roster needs.
"""

from __future__ import annotations

POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]

# slot name -> eligible positions
SLOT_ELIGIBILITY: dict[str, set[str]] = {
    "QB": {"QB"},
    "TQB": {"QB"},
    "RB": {"RB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "K": {"K"},
    "D/ST": {"DEF"},
    "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"},
    "WR/TE": {"WR", "TE"},
    "RB/WR": {"RB", "WR"},
    "OP": {"QB", "RB", "WR", "TE"},        # superflex
    "SUPERFLEX": {"QB", "RB", "WR", "TE"},
}


def eligible_positions(slot: str) -> set[str]:
    return SLOT_ELIGIBILITY.get(slot, set())


def is_flex_slot(slot: str) -> bool:
    """A slot that more than one position can fill (FLEX, OP, WR/TE, ...)."""
    return len(eligible_positions(slot)) > 1


def is_startable_slot(slot: str) -> bool:
    return bool(eligible_positions(slot))
