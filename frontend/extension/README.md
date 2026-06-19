# Fantasy Draft Agent — browser extension

Auto-captures your ESPN draft picks and shows the optimal pick as an on-page
overlay. It reads your own logged-in draft session (it never logs in for you)
and talks only to your local server over `localhost`.

## Install (Chrome, unpacked)

1. Start the backend first (from `backend/`):
   ```bash
   uvicorn app.server:app --port 8000
   ```
2. Open `chrome://extensions`, enable **Developer mode**, click **Load unpacked**,
   and select this `frontend/extension/` folder.
3. Open your ESPN draft room in the same Chrome profile. The overlay appears
   top-right. Click the toolbar icon to set the server URL, toggle AI reasoning,
   or enter a manual correction.

## How capture works

- **Preferred — websocket.** `inject.js` runs in the page at `document_start`
  and wraps `window.WebSocket` to mirror each draft frame (read-only) to
  `content.js`, which parses picks and POSTs them to the server. Structured and
  robust.
- **Fallback — DOM.** A `MutationObserver` watches the pick feed and sends the
  player name for the server to match. Used if the websocket parse misses.
- **React state.** `inject.js` polls ESPN's React fiber and unions picks from
  every pick-bearing structure (the full history list *and* the recent-picks
  widgets), so the server never lags behind a partial view. It also scrapes the
  live pick order, team list, and on-the-clock team name.
- **Turn detection** reads the on-the-clock indicator. ESPN's current pick is
  the source of truth: the overlay shows "Syncing — catching up (N/M)…" until
  our recorded picks match ESPN's pick number, and only then asserts "YOU ARE
  ON THE CLOCK" (and only then spends an LLM call).
- **Identity.** Set your **ESPN team/owner name** in the popup. When you're on
  the clock and the room shows that name, the server locks in your team id and
  adopts the room's live pick order, so snake math (your next pick) is correct
  even in a practice draft with ephemeral team ids.
- **Drafted safety net.** The extension mirrors any player ESPN marks DRAFTED to
  the server, which keeps them out of the shortlist even before their pick
  syncs — so an already-taken player can never be recommended.
- **Backstop.** The overlay shows a sync indicator (recorded vs expected picks);
  the popup has a one-click correction to add/fix a single pick.

## Draft-day setup (do this the night before)

ESPN's live websocket schema and DOM markup are undocumented and change, so
calibrate once before it counts:

1. In the popup, set your **ESPN team/owner name**, enable **Calibration
   logging**, and **Check connection** (it shows your detected team + draft
   slot). Confirm the league is loaded; if your team id is unset and the name
   match doesn't catch it, run
   `python -m app.cli config --league-id <id> --my-team-id <id>` then
   **Reload league**.
2. Open a mock draft. Watch the page console (`F12`) for `[FFDA]` logs:
   - `ws frame` lines show the live frames — confirm `parseWsPick` in
     `content.js` is extracting `espn_id` + `overall`. Adjust the key regexes
     if ESPN's field names differ.
   - `dom pick row` lines show what the fallback sees — adjust `SELECTORS` in
     `content.js` (also storable via `chrome.storage`) if rows aren't detected.
3. Turn calibration off for the real draft.

If capture ever drifts mid-draft, the sync indicator turns amber; use the
popup's manual correction to patch the missed pick and keep going.

## No-LLM mode

Uncheck **AI reasoning on my turn** (or leave `ANTHROPIC_API_KEY` unset) to run
fully on the deterministic engine at $0 — the overlay shows the engine's
templated rationale instead of the LLM's.
