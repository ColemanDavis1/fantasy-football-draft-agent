"""On-the-clock LLM reasoning (Opus 4.8) over the engine's shortlist.

Two-stage by design: the deterministic engine does all the math and produces a
shortlist + signals; the LLM only applies judgment over that shortlist. It fires
ONLY when it's my turn (~16 calls/draft), per the cost model.

Cost controls:
  - Opus 4.8 with adaptive thinking + high effort (thorough reasoning; I have
    30-90s on the clock). budget_tokens is removed on 4.8 — adaptive only.
  - Prompt caching: the stable board + league config sit in a cached system
    block; the volatile draft state (rosters, profiles, shortlist) goes in the
    user message AFTER the cache breakpoint, so on-the-clock calls only pay full
    price for what changed.
  - No-LLM mode: if ANTHROPIC_API_KEY is unset (or use_llm=False), return None
    and the caller falls back to the engine's templated rationale ($0).
"""

from __future__ import annotations

import json

from . import config
from .engine.profiler import tendency_label
from .engine.recommend import Recommendation
from .session import DraftSession

MODEL = "claude-opus-4-8"

_INSTRUCTIONS = (
    "You are an elite fantasy football draft advisor sitting next to the user "
    "during a live snake/auction draft. A deterministic engine has already "
    "computed value (VORP), tiers, opponent tendencies, and the probability "
    "each candidate survives to the user's next pick. Your job is JUDGMENT over "
    "that shortlist, not arithmetic: pick the single best player to draft right "
    "now and justify it in 2-3 skimmable sentences that reference opponent "
    "state (needs, tendencies, tier cliffs, who picks before the user's next "
    "turn). Trust the engine's numbers. Only recommend a player from the "
    "provided shortlist. Respond with ONLY a JSON object: "
    '{"pick_player_id": "...", "pick_name": "...", "rationale": "...", '
    '"alternatives": ["name", "name"]}.'
)


def _static_board_text(session: DraftSession) -> str:
    """Stable reference board (cached): top players by VORP + league config.
    Does not change as picks come in, so it caches cleanly across the draft."""
    players = sorted(
        (p for p in session.players_by_id.values() if p.vorp is not None),
        key=lambda p: p.vorp, reverse=True,
    )[:200]
    lines = [
        f"LEAGUE: {session.league.num_teams} teams, {session.league.scoring_type}"
        f"{', superflex' if session.league.is_superflex else ''}.",
        f"Starters: {json.dumps(session.league.starter_slots)}.",
        "",
        "REFERENCE BOARD (player_id | name | pos | team | bye | VORP | tier):",
    ]
    for p in players:
        lines.append(
            f"{p.player_id} | {p.name} | {p.position} | {p.team or '-'} | "
            f"{p.bye_week or '-'} | {p.vorp:.0f} | T{p.tier}"
        )
    return "\n".join(lines)


def _situation_text(session: DraftSession, rec: Recommendation) -> str:
    summary = session.state_summary()
    lines = [
        f"ON THE CLOCK: overall pick {rec.current_overall}. "
        f"Your next pick: {rec.my_next_overall} ({rec.picks_until_next} away).",
        "",
        "TEAMS (needs = unfilled starting slots):",
    ]
    for t in summary["teams"]:
        me = " [YOU]" if t["is_me"] else ""
        roster = ", ".join(f"{r['name']}({r['pos']})" for r in t["roster"]) or "empty"
        needs = ", ".join(f"{s}x{c}" for s, c in t["needs"].items()) or "full"
        run = f", {t['tendency']}" if t["tendency"] != "ADP-aligned" else ""
        # Historical prior, when seeded — most informative before this team has
        # revealed an archetype with live picks.
        prior = ""
        if t.get("prior_archetype") and t["archetype"] in ("Undeclared", "Balanced/BPA"):
            prior = f", prior:{t['prior_archetype']}"
        lines.append(
            f"  T{t['team_id']}{me} [{t['archetype']}{run}{prior}] needs: {needs} | "
            f"roster: {roster}"
        )
    lines.append("")
    lines.append("SHORTLIST (engine-ranked; pick from these):")
    lines.append("name | pos | team | VORP | tier | players_left_in_tier | "
                 "P(avail at your next pick) | #intervening_teams_needing_pos | "
                 "scouting note")
    for c in rec.shortlist:
        enr = getattr(c.player, "enrichment", None) or {}
        note = ""
        if enr.get("note"):
            note = f"[{enr.get('flag', '?')}] {enr['note']}"
        lines.append(
            f"  {c.player.name} | {c.player.position} | {c.player.team or '-'} | "
            f"{c.vorp:.0f} | T{c.tier} | {c.players_left_in_tier} left | "
            f"{c.p_available_next:.0%} | {c.needed_by_intervening} | {note}"
        )
    lines.append("")
    lines.append("Engine's provisional pick + reasoning (improve on it if "
                 f"warranted): {rec.rationale}")
    return "\n".join(lines)


def _parse(text: str) -> dict | None:
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def reason_on_the_clock(session: DraftSession, rec: Recommendation) -> dict | None:
    """Return {pick_player_id, pick_name, rationale, alternatives, model} or
    None (caller falls back to the engine's templated rationale)."""
    if not config.anthropic_api_key() or rec.primary is None:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    system = [
        {"type": "text", "text": _INSTRUCTIONS},
        {"type": "text", "text": _static_board_text(session),
         "cache_control": {"type": "ephemeral"}},  # stable prefix → cached
    ]
    user = _situation_text(session, rec)

    try:
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        with client.messages.stream(
            model=MODEL,
            max_tokens=6000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            msg = stream.get_final_message()
    except Exception:  # network/auth/etc — degrade to engine rationale
        return None

    text = "".join(b.text for b in msg.content if b.type == "text")
    parsed = _parse(text)
    if not parsed:
        # Model returned prose, not JSON — still usable as a rationale.
        return {"pick_player_id": rec.primary.player.player_id,
                "pick_name": rec.primary.player.name,
                "rationale": text.strip() or rec.rationale,
                "alternatives": [], "model": MODEL}
    parsed["model"] = MODEL
    return parsed
