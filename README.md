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
| **4** | Pre-draft enrichment: real ESPN projections (`kona_player_info`, league-scored), batched Claude scouting pass (sleeper/bust flags), historical opponent priors via ESPN `mDraftDetail` | ✅ done |
| **5** | Full local dashboard (live full-board view, served by the server) ✅ done · Claude-in-Chrome capture path — planned | partial |

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
shortlist these produce. Projections start as a rank-based placeholder and are
replaced by real ESPN projections in Phase 4 (the `board` output labels which).

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

Server endpoints: `/health`, `/state`, `/pick`, `/pick/correct`, `/picks/bulk`,
`/recommendation`, `/sync`, `/session/reset` (interactive docs at `/docs`).

**Recovery backstop (paste-to-resync).** If the live capture breaks (ESPN markup
change), a tab restarts, or you're in a mock room the tool isn't wired into,
paste the board into the dashboard's "Paste picks to resync" box (or POST text to
`/picks/bulk`) and the server fills any gaps. It accepts loose formats —
`overall | player | pos | team`, `12. Bijan Robinson, RB ATL`, `1.05 Chase WR CIN`,
or bare names in order — resolves each to a player, infers the drafting team from
snake-draft math, and only adds what's missing (`overwrite` also corrects
mismatches). Because every pick lives in SQLite and the server rebuilds from it,
**a tab/extension restart loses nothing** — resync at any point. This is also the
landing spot for the optional Claude-in-Chrome path: have it read the board and
emit a pick list, then paste it here.

Verify the whole live loop without a browser or API key:

```bash
python tests/test_server_sim.py   # saves a synthetic league, drives 28 picks
```

### Phase 4 — pre-draft enrichment + historical priors

Phase 4 replaces the placeholder projections with real ones and adds two
optional intelligence layers. None of it changes the live flow — it just makes
the board and the opponent model sharper.

```bash
# 1. Real projections (league-scored). Folded into `refresh` once a league is
#    configured, or run standalone. ESPN applies YOUR scoring (PPR/half/superflex)
#    to each projection, so cross-position VORP is finally trustworthy.
python -m app.cli projections --league-id 123456
python -m app.cli board -n 30        # now labelled "real ESPN projections"

# 2. Night-before Claude scouting pass (Batch API, 50% off). One scouting note +
#    a sleeper/value/solid/risk/bust flag per player, grounded in fresh web
#    research. Needs ANTHROPIC_API_KEY; skipped cleanly without one.
python -m app.cli enrich --limit 150          # submit + wait + write
python -m app.cli enrich --limit 150 --no-wait  # submit only...
python -m app.cli enrich --collect              # ...collect later

# 3. Historical opponent priors (returning leagues). Learns each manager's
#    tendencies from past drafts via ESPN mDraftDetail and attaches a prior to
#    their CURRENT team. Live picks refine it as the draft unfolds.
python -m app.cli priors --league-id 123456 --prior-seasons 2023 2024 2025
```

Projection provenance and enrichment/priors freshness all show up in
`python -m app.cli status`. The on-the-clock LLM sees each shortlist player's
scouting note and each opponent's historical archetype, so its reasoning is
projection-, scouting-, and history-aware.

### Draft-day runbook

The full hands-off sequence, start to finish:

```bash
cd backend
# --- The night before ---
python -m app.cli refresh --force                       # latest players/byes/trends
python -m app.cli config --league-id 123456 --my-team-id 7   # auto-read + confirm league
python -m app.cli projections                           # real, league-scored projections
python -m app.cli priors --prior-seasons 2023 2024 2025 # returning leagues only
python -m app.cli enrich                                # optional Claude scouting pass

# --- Draft time ---
uvicorn app.server:app --port 8000                      # start the local server
curl -X POST http://localhost:8000/session/reset        # load the configured league
# Load frontend/extension/ in Chrome (Developer mode -> Load unpacked), open your
# ESPN draft, and watch the overlay. Click your pick when prompted — that's it.
# Optional full-board view: open http://localhost:8000/ in another tab — every
# team's roster, needs, tendencies, and your recommendation, live each pick.
```

Set `ANTHROPIC_API_KEY` in `.env` for on-the-clock reasoning + enrichment; leave
it unset to run everything else at $0.

### Refreshing later

You won't draft for a while; data refreshes on demand. Just re-run
`python -m app.cli refresh` (optionally `--force`) before your draft to pull the
latest players, injuries, byes, and trends (it pulls projections too once a
league is configured).

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
  `mSettings`+`mTeam`, pro-team bye weeks, real season projections via
  `kona_player_info` (scored under your league's rules), and past drafts via
  `mDraftDetail` (completed drafts only — live picks come from the draft room in
  Phase 3).
- **Claude API** (optional, env key only): a night-before Batch API scouting
  pass with web search for sleeper/bust flags, and on-the-clock Opus 4.8
  reasoning over the engine shortlist.

## Project layout

```
backend/
  app/
    config.py        runtime config (env/.env/CLI merge; nothing league-specific baked in)
    db.py            SQLite connection + full schema (covers all phases)
    ingest.py        load Sleeper + ESPN data (players, projections) into SQLite
    enrich.py        Phase 4 night-before Claude scouting pass (Batch API)
    priors.py        Phase 4 historical opponent priors from past drafts
    cli.py           CLI: refresh / config / projections / enrich / priors / board / status
    data/
      sleeper.py     Sleeper client + disk cache
      espn.py        ESPN read-API client + config parser
    server.py        Phase 3 FastAPI server (pick ingestion + recommendation + dashboard)
    session.py       live draft session: picks -> engine -> recommendation; bulk reconcile
    match.py         resolve ESPN picks to our players (espn_id / name / D-ST)
    bulk.py          parse a pasted board into picks (recovery/mock-draft backstop)
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
    test_engine.py      offline proof of the engine math
    test_projections.py ESPN projection parse + board join (synthetic payload)
    test_enrich.py      enrichment JSON extraction + board join
    test_priors.py      draft-recap parse + cross-season owner priors
    test_bulk.py        paste parser + reconcile (gaps/overwrite/unmatched)
    test_server_sim.py  end-to-end live-pipeline simulation (no browser/LLM)
  data_cache/        gitignored disk cache (regenerated on demand)
  draft.db           gitignored SQLite DB
frontend/
  dashboard/         full-board live view, served by the server at / (Phase 5)
    index.html       self-contained dashboard (polls /state + /recommendation)
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
