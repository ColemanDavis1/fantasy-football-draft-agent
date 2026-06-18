# Claude Code Prompt — Live Fantasy Football Draft Agent

> Paste everything below the line into Claude Code. Fill in the bracketed `[…]` items
> (or let Claude Code ask you). Build it phase by phase — don't let it try to do
> everything at once.

---

## Role & Goal

You are helping me build a **live fantasy football draft assistant**: a local app that runs
alongside the ESPN fantasy draft room in another browser window and tells me the optimal pick
every time I'm on the clock. It has two phases of operation:

1. **Pre-draft research** — builds a ranked, tiered "draft board" enriched with current trends,
   and seeds an opponent-tendency profile for each leaguemate from past drafts.
2. **Live draft** — tracks every pick by every team, maintains a complete picture of every
   opponent's roster, records each opponent's tendencies as the draft unfolds, models what they
   will take next, and recommends my pick with the best reasoning available.

Two priorities drive every design decision:
- **Hands-off operation is the goal. The only thing I should do is click my pick.** The agent reads
  the league configuration AND every live pick on its own — I never type anything in during the
  draft. It stays attached to my open ESPN draft and surfaces a recommendation the moment I'm on
  the clock.
- **Analysis quality is paramount on my turn,** with a full picture of every opponent's roster and
  how they've been drafting. Heavy analysis fires only when I'm on the clock (~16 times a draft);
  everything else (data, profiling, math) is deterministic and free.

The agent must read my league's scoring, roster slots, number of teams, draft order, and draft
rules on its own from ESPN (see below) — do NOT ask me to type these in.

This is an **advisory tool only**. It never makes picks for me — it recommends, I click. Do not
build anything that automates submitting picks to ESPN.

## CRITICAL technical constraints (read before designing anything)

- **League configuration auto-reads from ESPN's API — no manual entry.** The `mSettings` and
  `mTeam` views on `lm-api-reads.fantasy.espn.com` return scoring rules, roster slot config, number
  of teams, draft order, and draft type. Read these directly from my league ID and confirm what was
  detected; never make me type them in.
- **Live picks CANNOT come from the read API.** ESPN's read API (`mDraftDetail`) only populates
  *after* the draft ends; the live draft room runs on a separate websocket system. So live picks
  must be captured from my actual open draft room. **This is the primary ingestion path — build it,
  not manual entry.**
- **Automatic live capture (primary):** a browser extension / content script that runs on my ESPN
  draft page and ingests picks on its own, then POSTs each to the local server.
  - **Prefer reading the websocket / network pick events** the draft room receives — structured and
    far more robust than scraping HTML, which breaks when ESPN changes markup. Fall back to a DOM
    MutationObserver only if the websocket path isn't workable.
  - **Detect whose turn it is** from the page (the on-the-clock indicator + countdown), and
    **pre-compute my recommendation during the pick before mine** so it's ready instantly when I'm up.
  - **Render the recommendation as an overlay on the ESPN page** (or a side panel) so I never leave
    the draft room.
- **Safety net (NOT the primary flow):** because the live feed is undocumented and can break, show a
  **sync indicator** (does the running pick count match expected?) and a **one-click correction** to
  add/fix a single missed pick. This is a backstop, not something I use in a normal draft. Do not
  make manual entry the main workflow.
- **Scoring-format aware:** PPR / half-PPR / standard and superflex change positional values
  completely. Apply the auto-detected scoring before producing any rankings.

## Tech stack

- **Backend:** Python + FastAPI. Recommendation engine, data ingestion, and Claude API calls live
  here. Use a local SQLite DB for the player board and draft state.
- **Frontend / capture layer:** A **browser extension (content script)** on the ESPN draft page is
  the core of the hands-off experience: it auto-captures picks (websocket events preferred, DOM as
  fallback), detects my turn, and renders the recommendation as an **on-page overlay** so I never
  leave the draft room. It talks to the local server over `localhost`. Drive it in the Chrome where
  I'm already logged into ESPN — the tool reads my own authenticated session; it never logs in for
  me. A separate local dashboard (React via Vite or plain HTML+JS) can show the full board, my
  roster, every opponent's roster, and tendencies for when I want the big picture.
  - **Alternative low-build path:** I have the **Claude in Chrome** integration. Support it as an
    option that attaches to my logged-in draft tab and reads the board live without a custom
    extension — more token-heavy, less polished, but a fast fallback.
- **AI (two-stage, tiered for cost + quality):** The deterministic engine does all the data
  crunching and produces a shortlist plus every computed signal. The LLM only reasons over that
  shortlist — judgment, not arithmetic. Model tiering:
  - **On the clock (my pick): use the strongest model — Claude Opus 4.8** (or Sonnet 4.6 if I want
    faster responses). This is the ~16-calls-per-draft hot path where quality decides my draft, and
    I have 30–90s on the clock, so let it reason thoroughly (enable extended thinking).
  - **Pre-draft research enrichment:** Opus or Sonnet via the **Batch API (50% off)** the night
    before — not time-sensitive.
  - **Everything else (profiling, runs, survival math):** pure Python, no LLM.
  - **Prompt caching:** cache the static board + opponent profiles so on-the-clock calls only pay
    full price for the changing draft state (~90% off the cached portion).
  - Provide a **"no-LLM mode"** toggle that falls back to a templated rationale from the engine
    outputs, so the tool is fully usable at $0 if I want.
  Read the API key from an environment variable. NEVER hardcode keys; add `.env` to `.gitignore`
  from the first commit.
- Keep everything in one repo with a clear `backend/` and `frontend/` split and a top-level README.

## Data sources (all free)

- **Sleeper API** (backbone, no auth, read-only):
  - Full player DB: `GET https://api.sleeper.app/v1/players/nfl` (large ~5MB — cache to disk, refresh ≤1×/day)
  - Trending adds/drops: `GET https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=24&limit=50`
  - Draft/ADP data via Sleeper draft endpoints.
- **ESPN undocumented endpoints** (to match what my leaguemates see, and to auto-configure):
  - **League config via the `mSettings` + `mTeam` views** — scoring rules, roster slots, team count,
    draft order, draft type. Use this to configure the tool automatically; do not ask me to type it.
  - Player info/projections via the `kona_player_info` view on the v3 fantasy API.
  - Injury status.
  - **Past drafts via `mDraftDetail`** — this view DOES work for *completed* drafts (it only fails
    for live ones). If my league is returning, pull prior seasons' draft recaps to seed each
    manager's tendency profile before the draft starts (see Opponent Modeling below).
- **Live web research:** use the Claude API with web search enabled for qualitative trends
  (camp/beat-writer news, depth-chart changes, rookie roles) during the research phase.

## The recommendation engine (this is the core — make it good, not a toy)

Implement all three of these, not just raw rankings:

1. **Value Based Drafting (VBD / VORP).** Compute each player's projected points *over replacement*
   at their position, where the replacement baseline = the Nth-best player at that position and
   N = (number of starting slots for that position across the whole league). Rank by VORP, not raw
   points. Recompute baselines from MY league's roster settings.
2. **Tier-based logic.** Group players into value tiers (cluster by VORP gaps). Surface "tier cliff"
   warnings: if the last player in a tier is likely to be gone before my next pick, that's urgency
   to draft now.
3. **Survival probability** ("will he get back to me?"). For each candidate, estimate P(player
   still available at my NEXT pick). Do NOT use a naive ADP-only model — drive it off the
   per-opponent profiles below: weight each intervening opponent's roster needs against their
   observed tendency against ADP. Combine the result with VORP to decide draft-now vs. wait.

## Opponent modeling (required — this is the edge)

Maintain a live profile for every other team and update it on every pick. Two layers:

- **Deterministic profiler (runs on each pick, no LLM):** per opponent, track position counts,
  ADP deviation (do they reach ahead of value or wait for it?), run participation (do they start
  or follow positional runs?), team-stacking (QB + same-team WR/TE), and an inferred archetype
  (Zero-RB / Robust-RB / Hero-RB / best-available). Also track each team's unfilled starting slots
  (their needs) and bye-week conflicts.
- **Historical priors:** if the league is returning, seed each manager's profile from past drafts
  pulled via ESPN's `mDraftDetail` (works for completed drafts). Start from the prior, then update
  it as live picks confirm or contradict it.

These profiles are the input to the survival-probability model and must be shown to the on-the-clock
LLM call so its reasoning is opponent-aware ("the 4 teams before your next pick all have TE filled,
so this TE is safe to wait on").

Also account for: roster construction (starting requirements, flex, bench), bye-week conflicts,
handcuffs, and positional runs (detect when 3+ of one position go in a short window and flag it).

## Recommendation output format (every time I'm on the clock)

Show me, in one screen:
- **Every team's roster** — mine plus all opponents, by slot, with filled/empty slots, computed
  needs, and bye-week conflicts flagged. I need the full board state visible when deciding.
- **Live tendencies** — a compact read on how each opponent has been drafting (archetype + any
  active positional run), so I can see the trends as they're forming.
- **Ranked top-5** candidates with, for each: player, position, team, VORP, current tier, and
  P(available at my next pick).
- **One primary recommendation** with a 2–3 sentence rationale that explicitly references opponent
  state ("RB tier 3 has 2 left and the 4 teams before your next pick all still need RB, so it
  won't return — take Player X now"). Keep it skimmable; I'm on the clock.

## Build it in phases (ship something runnable at each step)

- **Phase 1 — Data + auto-config layer.** Ingest + cache the Sleeper player DB and ADP. Auto-read my
  league config from ESPN (`mSettings`/`mTeam`) given my league ID. SQLite schema for players, teams,
  picks, rosters, league_settings, and opponent_profiles. CLI to verify data loads and prints the
  detected league config for me to confirm. No UI yet.
- **Phase 2 — Recommendation engine (offline).** Implement VORP, tiers, the opponent profiler, and
  opponent-aware survival probability as pure functions, tested against a hardcoded sample draft
  state. Prove the math before any UI.
- **Phase 3 — Automatic live capture + recommendations.** The browser extension that auto-captures
  picks from my open draft room (websocket events preferred, DOM fallback), detects my turn,
  pre-computes during the prior pick, and renders the recommendation overlay on the ESPN page.
  FastAPI endpoints receive picks, update all rosters/profiles, and return a recommendation. Include
  the sync indicator + one-click correction backstop. This is the fully hands-off tool.
- **Phase 4 — Pre-draft enrichment + historical priors.** Claude API pass (batched, night-before)
  over the player pool + fresh web research for tier notes and sleeper/bust flags; and, for a
  returning league, pull past drafts via `mDraftDetail` to seed each opponent's profile.
- **Phase 5 (optional) — Claude in Chrome path + full dashboard.** A no-custom-extension capture
  option using Claude in Chrome, and the separate local dashboard for the full-board view.

## Before you start coding

The tool auto-detects league config, so I should only have to give you:
- **My ESPN league ID** (and prior season IDs if it's a returning league, for opponent priors). If
  the league is private, tell me you'll need me to be logged into ESPN in the browser you attach to
  (I'll handle the login — you never enter my credentials).

Read the league config from ESPN, show me what you detected (scoring, roster slots, teams, my draft
slot, draft type) for a quick confirm, then build Phase 1 before moving on. After each phase, show
me how to run it and pause for feedback.
