# Live board dashboard

A single-file, full-board view that runs beside your draft and updates every
pick. It's the big-picture companion to the on-page extension overlay: the
overlay shows *your* pick in the ESPN room; the dashboard shows *everything*.

## Open it

The local server serves it directly — no build step, no separate process:

```
uvicorn app.server:app --port 8000      # from backend/
# then open http://localhost:8000/  (or /dashboard)
```

It polls the same server the extension feeds, so it reflects live picks the
moment they're captured. Run `POST /session/reset` after configuring your league
so the board loads the right teams.

## What it shows

- **Header** — scoring/format, current pick + round, who's on the clock (YOU
  highlighted), active positional runs, and the live **sync** badge (does the
  captured pick count match the draft?).
- **Your pick** (left) — the engine's primary recommendation with its signals
  (VORP, tier + how many left, P(available at your next pick), pick score, run
  and roster-need flags) and the top-5 shortlist. A button fetches **Opus 4.8**
  reasoning on demand — it is *not* polled, to respect the ~16-calls-per-draft
  cost model (set `ANTHROPIC_API_KEY` to enable it; the overlay is the primary
  on-the-clock trigger).
- **Every team** (grid, in draft order) — roster by player, computed **needs**
  (unfilled starting slots), inferred **archetype** (with the historical prior
  shown until they reveal one live), reach/value tendency, team stacks, and
  bye-week conflicts. This is the "what does each team still need, and how does
  that shape who they take" view.

Data comes from `GET /state` and `GET /recommendation`; everything renders
client-side. If the server isn't running, a banner says so.
