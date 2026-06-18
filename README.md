# Fantasy Football Draft Agent

A local, hands-off **live draft assistant** for ESPN fantasy football. It runs
alongside your open ESPN draft room and tells you the optimal pick every time
you're on the clock. You only ever click your pick.

The tool is **league-agnostic and open-sourceable**: no league ID is baked in.
You provide a league ID at run time (e.g. on draft day) and the tool auto-reads
that league's scoring, roster slots, team count, draft order, and draft type
from ESPN. Point it at any league.

> **Advisory only.** It recommends; you click. It never submits picks to ESPN.

## How it works

1. **Pre-draft research** builds a ranked, tiered draft board and seeds an
   opponent-tendency profile for each leaguemate from past drafts.
2. **Live draft** tracks every pick, maintains every opponent's roster, models
   what they'll take next, and recommends your pick with full reasoning.

Heavy AI analysis fires only when **you're on the clock** (~16 calls per draft).
Everything else (data, profiling, survival math) is deterministic and free.

## Build phases

| Phase | What | Status |
|-------|------|--------|
| **1** | Data + auto-config: Sleeper player DB/ADP cache, ESPN league auto-read, SQLite schema, verification CLI | ✅ done |
| **2** | Recommendation engine (offline): VORP, tiers + cliff detection, opponent profiler, opponent-aware survival probability | ✅ done |
| **3** | Live capture: browser extension (websocket-preferred, DOM fallback), turn detection, on-page recommendation overlay, FastAPI server, on-the-clock LLM (Opus 4.8) with no-LLM fallback, sync indicator + one-click correction | ✅ done |
| 4 | Pre-draft enrichment (batched Claude pass) + historical opponent priors via ESPN `mDraftDetail` | planned |
| 5 | (optional) Claude-in-Chrome capture path + full local dashboard | planned |

## Quickstart (Phase 1)

Requires Python 3.11+ (developed on 3.14).

```bash
cd backend
python -m pip install -r requirements.txt

# 1. Pull + cache the player board (Sleeper) and trending adds/drops.
#    Re-run any time to refresh; player DB caches to disk (refresh <=1x/day).
python -m app.cli refresh            # add --force to bypass the 24h cache

# 2. Peek at the loaded board.
python -m app.cli players --pos RB -n 15
python -m app.cli status

# 3. On draft day: auto-read your league config from ESPN and confirm it.
python -m app.cli config --league-id 123456 --my-team-id 7
```

`config` prints the detected scoring, superflex flag, roster slots, draft type,
and full draft order (with **YOU** marked) for a quick confirm, then saves it.

### Phase 2 — recommendation engine (offline)

```bash
# Prove the math against a hardcoded sample draft (no deps needed):
python tests/test_engine.py

# See a full on-the-clock recommendation for the sample draft state
# (rosters + needs + tendencies, ranked top-5 with signals, primary pick):
python -m app.engine.demo

# Run VORP + tiers on the LIVE board you loaded (placeholder projections):
python -m app.cli board -n 30
python -m app.cli board --pos RB -n 15
```

The engine (`app/engine/`) is pure, deterministic functions: VORP with
league-aware replacement baselines, per-position tiers + cliff detection, the
opponent profiler (archetype, needs, ADP deviation, runs, stacks, byes), and
opponent-aware survival probability. The LLM (Phase 3) only reasons over the
shortlist these produce. Projections are a rank-based placeholder until Phase 4
swaps in real ones.

### Phase 3 — live capture + recommendations (hands-off)

```bash
# 1. Start the local server (from backend/):
uvicorn app.server:app --port 8000

# 2. On draft day, point it at your league, then reload the session:
python -m app.cli config --league-id 123456 --my-team-id 7
curl -X POST http://localhost:8000/session/reset

# 3. Load the extension and open your draft (see frontend/extension/README.md):
#    chrome://extensions -> Developer mode -> Load unpacked -> frontend/extension/
```

The extension auto-captures every pick (websocket preferred, DOM fallback),
detects your turn, and renders the recommendation as an overlay on the ESPN
page. The server matches each pick, updates all rosters + opponent profiles, and
returns a recommendation; when it's **your** turn it adds Opus 4.8's reasoning
over the engine shortlist (set `ANTHROPIC_API_KEY` in `.env`, or leave it unset
for free no-LLM mode). A sync indicator + one-click correction back up the feed.

Server endpoints: `/health`, `/state`, `/pick`, `/pick/correct`,
`/recommendation`, `/sync`, `/session/reset` (interactive docs at `/docs`).

Verify the whole live loop without a browser or API key:

```bash
python tests/test_server_sim.py   # saves a synthetic league, drives 28 picks
```

### Refreshing later

You won't draft for a while; data refreshes on demand. Just re-run
`python -m app.cli refresh` (optionally `--force`) before your draft to pull the
latest players, injuries, byes, and trends.

## Configuration

Copy `.env.example` to `.env` and fill in only what you need. Nothing is
required to refresh data. For reading a specific league:

- `ESPN_LEAGUE_ID` / `--league-id` — your league (from the league URL).
- `ESPN_SEASON` / `--season` — defaults to the current year.
- `MY_TEAM_ID` / `--my-team-id` — flags your team and computes your draft slot.
- `ESPN_SWID` + `ESPN_S2` — **private leagues only.** Copy these two cookies
  from your logged-in ESPN browser session. You handle the login; this tool
  **never** sees your password and never logs in for you.
- `ANTHROPIC_API_KEY` — used in Phase 3+ for on-the-clock reasoning. Read from
  env only, never hardcoded. Leave blank to run in no-LLM mode.

## Data sources (all free)

- **Sleeper** (no auth): full player DB, trending adds/drops. ADP is proxied by
  Sleeper's `search_rank` (a richer ADP source can plug in later).
- **ESPN read API** (`lm-api-reads.fantasy.espn.com`): league config via
  `mSettings`+`mTeam`, pro-team bye weeks, and past drafts via `mDraftDetail`
  (completed drafts only — live picks come from the draft room in Phase 3).

## Project layout

```
backend/
  app/
    config.py        runtime config (env/.env/CLI merge; nothing league-specific baked in)
    db.py            SQLite connection + full schema (covers all phases)
    ingest.py        load Sleeper + ESPN data into SQLite (idempotent upserts)
    cli.py           Phase 1 verification CLI
    data/
      sleeper.py     Sleeper client + disk cache
      espn.py        ESPN read-API client + config parser
    server.py        Phase 3 FastAPI server (pick ingestion + recommendation)
    session.py       live draft session: picks -> engine -> recommendation
    match.py         resolve ESPN picks to our players (espn_id / name / D-ST)
    llm.py           on-the-clock Opus 4.8 reasoning (prompt-cached, no-LLM fallback)
    board.py         DB rows -> engine models (shared by CLI + server)
    engine/          Phase 2 recommendation engine (pure functions)
      vorp.py        value over replacement, league-aware baselines
      tiers.py       per-position tiers + cliff detection
      profiler.py    opponent tendency profiles + roster needs
      survival.py    opponent-aware P(available at next pick)
      draftflow.py   snake-draft order math
      recommend.py   shortlist + signals + templated rationale
      sample.py      hardcoded sample draft state (for tests/demo)
      demo.py        prints a full recommendation for the sample
  tests/
    test_engine.py     offline proof of the engine math
    test_server_sim.py end-to-end live-pipeline simulation (no browser/LLM)
  data_cache/        gitignored disk cache (regenerated on demand)
  draft.db           gitignored SQLite DB
frontend/
  extension/         Chrome MV3 extension: capture + overlay (Phase 3)
    manifest.json    MV3 config
    inject.js        page-context WebSocket interceptor (MAIN world)
    content.js       pick parsing, DOM fallback, turn detection, overlay
    background.js    talks to the local server (avoids CORS)
    popup.html/js    settings, connection check, manual correction
    overlay.css      on-page overlay styling
```

## Privacy & safety

- Secrets live in `.env`, which is gitignored from the first commit. API keys
  are read from the environment only.
- The tool reads your own authenticated ESPN session for private leagues; it
  never stores or transmits your credentials.
- It is strictly advisory and never automates picks.
