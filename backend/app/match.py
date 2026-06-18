"""Match an ESPN draft pick to our player record.

The live draft feed identifies players by ESPN player id (preferred) or by name.
We resolve to our internal player_id (Sleeper id) using, in order:
  1. ESPN id  -> players.espn_id   (most reliable)
  2. normalized name + position
  3. normalized name only
  4. D/ST special case: match team defenses by NFL team abbrev

Returns (player_id, confidence) so the server can flag low-confidence matches
for the one-click correction backstop.
"""

from __future__ import annotations

import re
import sqlite3

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_name(name: str) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    s = re.sub(r"[.\'`]", "", s)          # drop periods/apostrophes
    s = re.sub(r"[^a-z0-9 ]", " ", s)      # other punctuation -> space
    tokens = [t for t in s.split() if t and t not in _SUFFIXES]
    return " ".join(tokens)


class PlayerMatcher:
    def __init__(self, conn: sqlite3.Connection):
        self._by_espn: dict[str, str] = {}
        self._by_name_pos: dict[tuple[str, str], str] = {}
        self._by_name: dict[str, str] = {}
        self._by_team_def: dict[str, str] = {}
        self._build(conn)

    def _build(self, conn: sqlite3.Connection) -> None:
        # Order so the most draft-relevant player wins each name (active first,
        # then best search_rank). setdefault then keeps that one, avoiding
        # collisions with retired namesakes.
        rows = conn.execute(
            """SELECT player_id, espn_id, full_name, position, team
               FROM players
               ORDER BY active DESC, (search_rank IS NULL), search_rank ASC"""
        ).fetchall()
        for r in rows:
            pid = r["player_id"]
            if r["espn_id"]:
                self._by_espn[str(r["espn_id"])] = pid
            nm = normalize_name(r["full_name"] or "")
            if nm:
                self._by_name_pos.setdefault((nm, r["position"] or ""), pid)
                # name-only is ambiguous; keep first seen only.
                self._by_name.setdefault(nm, pid)
            if r["position"] == "DEF" and r["team"]:
                self._by_team_def[r["team"].upper()] = pid

    def match(self, espn_id: str | None = None, name: str | None = None,
              position: str | None = None, team: str | None = None
              ) -> tuple[str | None, str]:
        if espn_id and str(espn_id) in self._by_espn:
            return self._by_espn[str(espn_id)], "espn_id"

        if position == "DEF" and team and team.upper() in self._by_team_def:
            return self._by_team_def[team.upper()], "team_def"

        nm = normalize_name(name or "")
        if nm and position and (nm, position) in self._by_name_pos:
            return self._by_name_pos[(nm, position)], "name+pos"
        if nm and nm in self._by_name:
            return self._by_name[nm], "name"

        return None, "unmatched"
