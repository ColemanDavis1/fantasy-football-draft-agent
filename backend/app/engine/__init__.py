"""Recommendation engine (Phase 2).

Pure, deterministic functions — no DB, no network, no LLM. Everything here is
exact arithmetic over an in-memory draft state, so it can be unit-tested and
runs for free on every pick. The LLM (Phase 3) only reasons over the shortlist
these functions produce.

Modules:
  models      dataclasses for Player / LeagueSettings / DraftState
  slots       lineup-slot eligibility (handles FLEX, superflex, etc.)
  vorp        value over replacement, with league-aware baselines
  tiers       per-position value tiers + cliff detection
  draftflow   snake-draft order math (who picks before my next turn)
  profiler    per-opponent tendency profile + roster needs
  survival    opponent-aware P(player available at my next pick)
  recommend   ties it together into the on-the-clock recommendation
"""
