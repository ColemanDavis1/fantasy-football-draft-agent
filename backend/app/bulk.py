"""Parse a pasted pick list into structured picks for bulk reconcile.

This is the recovery backstop: if the live capture breaks (or you're in a mock
room the tool isn't wired into), paste the board — from Claude in Chrome or
copied straight off ESPN — and the server fills any gaps. The format is fuzzy on
purpose; people paste all kinds of shapes. We support, one pick per line:

  - `overall | player | pos | team`     (recommended; team id, if present, is ignored)
  - `12. Bijan Robinson, RB ATL`        (numbered)
  - `1.05 Ja'Marr Chase WR CIN`         (round.pick)
  - `Bijan Robinson (ATL - RB)`         (parenthetical meta)
  - `Ja'Marr Chase`                     (bare names, taken in listed order)

Each line yields {overall, round, pick_in_round, name, position, team}; missing
overalls are filled sequentially by the reconciler. Player resolution + team
inference (snake math) happen later in session.reconcile — this module only
turns text into rows.
"""

from __future__ import annotations

import re

from .data.espn import PRO_TEAM_ABBREV

# NFL abbrevs we accept in pasted text (Sleeper/ESPN conventions + aliases).
_NFL = set(PRO_TEAM_ABBREV.values()) | {
    "WAS", "JAC", "LAR", "LAC", "LV", "OAK", "SD", "STL", "ARI", "BAL", "HOU"}
_POS = {"QB", "RB", "WR", "TE", "K", "DEF"}

# Lines that are clearly headers/labels, not picks.
_SKIP = re.compile(r"^(round\s*\d+|rd\s*\d+|draft\s+results?|pick\s+history|"
                   r"on the clock|results?)\b", re.I)


def _match_pos(tok: str) -> str | None:
    t = re.sub(r"[^A-Za-z/]", "", tok).upper()
    if t in ("DST", "DEF", "D/ST"):
        return "DEF"
    if t in ("K", "PK"):
        return "K"
    if t in ("QB", "RB", "WR", "TE"):
        return t
    return None


def _match_team(tok: str) -> str | None:
    t = re.sub(r"[^A-Za-z]", "", tok).upper()
    return t if t in _NFL else None


def _extract_name_meta(text: str) -> tuple[str, str | None, str | None]:
    """Pull (name, position, team) out of the non-numeric remainder of a line."""
    position: str | None = None
    team: str | None = None

    # Parenthetical/bracket groups carry meta like "(ATL - RB)".
    metas = re.findall(r"[\(\[]([^)\]]*)[\)\]]", text)
    core = re.sub(r"[\(\[][^)\]]*[\)\]]", "", text)

    # A comma or dash often separates the name from trailing meta.
    segments = re.split(r"\s*[,–—]\s*|\s+-\s+", core)
    name_part = segments[0]
    metas.extend(segments[1:])

    # Peel position/team words off the END of the name ("Bijan Robinson RB ATL").
    words = name_part.split()
    while words:
        w = words[-1]
        p, t = _match_pos(w), _match_team(w)
        if p and not position:
            position, _ = p, words.pop()
        elif t and not team:
            team, _ = t, words.pop()
        else:
            break
    name = " ".join(words).strip(" .,-")

    # Scan the trailing meta for anything we still need.
    for tok in re.split(r"[\s/]+", " ".join(metas)):
        if not tok:
            continue
        p, t = _match_pos(tok), _match_team(tok)
        if p and not position:
            position = p
        if t and not team:
            team = t
    return name, position, team


def _parse_pipe(line: str) -> dict | None:
    parts = [p.strip() for p in line.split("|") if p.strip()]
    if not parts:
        return None
    overall = int(parts[0].lstrip("#")) if parts[0].lstrip("#").isdigit() else None
    rest = parts[1:] if overall is not None else parts
    # First part with letters is the name; pure-int parts (e.g. team id) ignored.
    name_raw = next((p for p in rest if any(c.isalpha() for c in p)), None)
    if not name_raw:
        return None
    name, position, team = _extract_name_meta(name_raw)
    # Any remaining parts may hold pos/team too.
    for p in rest:
        if p == name_raw:
            continue
        pos, tm = _match_pos(p), _match_team(p)
        position = position or pos
        team = team or tm
    if not name:
        return None
    return {"overall": overall, "round": None, "pick_in_round": None,
            "name": name, "position": position, "team": team}


def _parse_free(line: str) -> dict | None:
    s = re.sub(r"^(pick|sel|selection)\s+", "", line, flags=re.I).strip()
    overall = rnd = pir = None

    m = re.match(r"^R?(\d+)\s*[.\-_ ]\s*P?(\d+)\b[.):]?", s, re.I)  # round.pick
    if m and "." in s[:m.end()]:
        rnd, pir = int(m.group(1)), int(m.group(2))
        s = s[m.end():].strip()
    else:
        m = re.match(r"^#?(\d+)\s*[.):\-]\s*", s)                   # "12." / "12)"
        if not m:
            m = re.match(r"^#?(\d+)\s+(?=\D)", s)                   # "12 Name"
        if m:
            overall = int(m.group(1))
            s = s[m.end():].strip()

    if not s or not any(c.isalpha() for c in s):
        return None
    name, position, team = _extract_name_meta(s)
    if not name:
        return None
    return {"overall": overall, "round": rnd, "pick_in_round": pir,
            "name": name, "position": position, "team": team}


def parse_picks(text: str) -> list[dict]:
    """Parse pasted text into a list of pick dicts, in listed order."""
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or not any(c.isalpha() for c in line) or _SKIP.match(line):
            continue
        rec = _parse_pipe(line) if "|" in line else _parse_free(line)
        if rec and rec["name"]:
            out.append(rec)
    return out
