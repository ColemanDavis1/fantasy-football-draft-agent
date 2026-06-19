// Runs in the ISOLATED world on the ESPN draft page. Responsibilities:
//   1. Capture picks — preferred: websocket frames mirrored by inject.js;
//      fallback: a DOM MutationObserver on the pick feed.
//   2. POST each new pick to the local server (via the background worker).
//   3. Detect when it's my turn and render the recommendation as an overlay.
//
// The live websocket schema and ESPN's DOM are undocumented and change, so the
// parsers here are best-effort and heavily logged. Turn on calibration mode
// (popup) the night before to inspect real frames/markup, then adjust SELECTORS
// or parseWsPick below. The DOM fallback is the dependable default.

(function () {
  "use strict";

  const state = {
    seenKeys: new Set(),
    lastRecommendation: null,
    calibrate: false,
    useLlm: true,
  };

  // ---- config (selectors overridable from storage) ----------------------
  const SELECTORS = {
    // Container holding the running list of made picks.
    pickFeed: ".draft-columns, .pick-history, [class*='draftHistory']",
    // A single made-pick row within the feed.
    pickRow: "[class*='pick'][class*='made'], .draft-pick, li[class*='pick']",
    // The on-the-clock indicator (text like "On the clock").
    onClock: "[class*='onTheClock'], [class*='on-the-clock'], [class*='clock']",
  };

  chrome.storage.sync.get(["calibrate", "useLlm", "selectors"], (cfg) => {
    state.calibrate = !!cfg.calibrate;
    state.useLlm = cfg.useLlm !== false;
    if (cfg.selectors) Object.assign(SELECTORS, cfg.selectors);
    log("content script ready", SELECTORS);
  });

  function log(...args) {
    if (state.calibrate) console.log("%c[FFDA]", "color:#0a7", ...args);
  }

  // ---- websocket capture (preferred) ------------------------------------
  window.addEventListener("message", (ev) => {
    const m = ev.data;
    if (!m || m.__ffda !== true || m.kind !== "ws") return;
    const p = m.payload;
    // Calibration: confirm the socket was even seen. If you see an "ws OPEN"
    // line, inject.js wrapped the draft-room socket; if you then see "ws frame"
    // lines as picks happen, the websocket path is viable — copy a frame here so
    // the parser can be tuned to ESPN's real schema. No OPEN line at all => the
    // socket isn't on the main thread (worker/other transport) => use the DOM.
    if (state.calibrate && p.event === "open") log("ws OPEN", p.url);
    if (p.event !== "message") return;
    let obj;
    try { obj = JSON.parse(p.data); } catch (e) {
      if (state.calibrate) log("ws non-JSON frame", String(p.data).slice(0, 200));
      return;
    }
    if (state.calibrate) log("ws frame", obj);
    const pick = parseWsPick(obj);
    if (pick) handlePick(pick, "websocket");
    else if (state.calibrate) log("ws frame did NOT parse as a pick (schema?)");
  });

  // Best-effort: recursively find a node that looks like a completed pick.
  // ESPN pick events have varied over time; we look for a player id plus an
  // overall-pick number. Returns {espn_id, team_id, overall} or null.
  function parseWsPick(root) {
    let found = null;
    function walk(node) {
      if (found || !node || typeof node !== "object") return;
      const keys = Object.keys(node);
      const playerKey = keys.find((k) => /^player(id)?$/i.test(k) || /playerid/i.test(k));
      const overallKey = keys.find((k) => /overall.*pick|pick.*overall|overallpicknumber/i.test(k));
      const teamKey = keys.find((k) => /^(team|member)id$/i.test(k) || /teamid|memberid/i.test(k));
      if (playerKey && overallKey) {
        const espnId = String(node[playerKey]);
        const overall = parseInt(node[overallKey], 10);
        const teamId = teamKey ? parseInt(node[teamKey], 10) : null;
        if (espnId && !Number.isNaN(overall)) {
          found = { espn_id: espnId, team_id: teamId, overall: overall };
          return;
        }
      }
      for (const k of keys) walk(node[k]);
    }
    walk(root);
    return found;
  }

  // ---- DOM fallback ------------------------------------------------------
  const observer = new MutationObserver((mutations) => {
    for (const mut of mutations) {
      for (const node of mut.addedNodes) {
        if (node.nodeType !== 1) continue;
        const row = node.matches && node.matches(SELECTORS.pickRow)
          ? node : node.querySelector && node.querySelector(SELECTORS.pickRow);
        if (row) maybePickFromRow(row);
      }
    }
    checkOnClock();
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });

  function maybePickFromRow(row) {
    const text = (row.textContent || "").replace(/\s+/g, " ").trim();
    if (!text) return;
    if (state.calibrate) log("dom pick row", text);
    const pick = parseDomPick(text);
    if (pick) handlePick(pick, "dom");
    else if (state.calibrate) log("  (skipped: not a completed pick row)");
  }

  // ESPN renders a COMPLETED pick row as:
  //   "<Player> / <NFLTeam> <POS>R<round>, P<pickInRound> - <ManagerTeam>"
  //   e.g. "Dallas Goedert / PHI TER7, P8 - 4th and Hurts"
  // Header/upcoming cells ("Round 9", "PICK 114 AUTO …") have no " / " and are
  // skipped. We send name + position + round/pick; the server computes the exact
  // overall from round/pick (so ordering and dedup are reliable).
  function parseDomPick(text) {
    if (/^round\s*\d+/i.test(text)) return null;   // round header
    if (/^pick\s*\d+/i.test(text)) return null;    // upcoming/auto cell
    const i = text.indexOf(" / ");
    if (i === -1) return null;                      // completed picks have " / "
    const name = text.slice(0, i).trim();
    if (!name) return null;
    const rest = text.slice(i + 3);                 // "PHI TER7, P8 - Manager"
    const m = rest.match(/^([A-Za-z]{2,4})\s+([A-Z/]+?)R(\d+),\s*P(\d+)/);
    const out = { name: name };
    if (m) {
      out.position = normPos(m[2]);
      out.round = parseInt(m[3], 10);
      out.pick_in_round = parseInt(m[4], 10);
    }
    return out;
  }

  function normPos(p) {
    p = (p || "").toUpperCase();
    if (p === "D/ST" || p === "DST" || p === "DEF") return "DEF";
    if (p === "PK") return "K";
    return p;
  }

  // ---- turn detection ----------------------------------------------------
  function checkOnClock() {
    const el = document.querySelector(SELECTORS.onClock);
    if (!el) return;
    const txt = (el.textContent || "").toLowerCase();
    const mineNow = /you|your pick|you're on the clock/.test(txt);
    if (mineNow && !state.overlayPinnedForTurn) {
      state.overlayPinnedForTurn = true;
      refreshRecommendation(); // ensure overlay is up the moment we're up
    } else if (!mineNow) {
      state.overlayPinnedForTurn = false;
    }
  }

  // ---- server I/O --------------------------------------------------------
  function send(type, payload) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(Object.assign({ type }, payload), resolve);
    });
  }

  async function handlePick(pick, source) {
    // Dedupe on the most specific id available (round/pick, then overall, then
    // name) — the DOM observer can re-emit the same row on re-render.
    const key = (pick.round != null && pick.pick_in_round != null)
      ? `r${pick.round}p${pick.pick_in_round}`
      : (pick.overall != null ? `o${pick.overall}` : `n:${pick.name}`);
    if (state.seenKeys.has(key)) return;
    state.seenKeys.add(key);
    pick.source = source;
    pick.use_llm = state.useLlm;
    const res = await send("pick", { pick });
    if (!res || !res.ok) { renderError(res && res.error); return; }
    if (res.data.recommendation) render(res.data.recommendation);
    log("pick sent", pick, "->", res.data.pick);
  }

  async function refreshRecommendation() {
    const res = await send("recommendation", { useLlm: state.useLlm });
    if (res && res.ok) render(res.data);
  }

  // ---- overlay -----------------------------------------------------------
  let root = null;
  function ensureOverlay() {
    if (root) return root;
    root = document.createElement("div");
    root.id = "ffda-overlay";
    root.innerHTML = `
      <div id="ffda-header">
        <span id="ffda-title">Draft Agent</span>
        <span id="ffda-sync"></span>
        <button id="ffda-refresh" title="Recompute">↻</button>
        <button id="ffda-min" title="Minimize">—</button>
      </div>
      <div id="ffda-body"></div>`;
    document.body.appendChild(root);
    root.querySelector("#ffda-refresh").onclick = refreshRecommendation;
    root.querySelector("#ffda-min").onclick = () => root.classList.toggle("ffda-collapsed");
    return root;
  }

  function render(rec) {
    state.lastRecommendation = rec;
    const el = ensureOverlay();
    el.classList.toggle("ffda-myturn", !!rec.is_my_turn);
    const sync = el.querySelector("#ffda-sync");
    sync.textContent = rec.sync && rec.sync.in_sync ? "● synced"
      : `● ${(rec.sync && rec.sync.missing_overalls || []).length} missing`;
    sync.className = rec.sync && rec.sync.in_sync ? "ffda-ok" : "ffda-warn";

    const primary = rec.llm || rec.primary;
    const name = rec.llm ? rec.llm.pick_name : (rec.primary && rec.primary.name);
    const rationale = rec.llm ? rec.llm.rationale : rec.engine_rationale;
    const turn = rec.is_my_turn ? "YOU ARE ON THE CLOCK" :
      `Pick ${rec.current_overall} · your next: ${rec.my_next_overall || "?"}`;

    const rows = (rec.shortlist || []).map((c) => `
      <tr>
        <td class="ffda-name">${esc(c.name)}</td>
        <td>${esc(c.position)}</td>
        <td>${Math.round(c.vorp)}</td>
        <td>T${c.tier}</td>
        <td>${Math.round((c.p_available_next || 0) * 100)}%</td>
      </tr>`).join("");

    el.querySelector("#ffda-body").innerHTML = `
      <div id="ffda-turn">${esc(turn)}</div>
      ${name ? `<div id="ffda-primary"><b>Take ${esc(name)}</b>
        ${rec.llm ? '<span class="ffda-badge">AI</span>' : ''}</div>
        <div id="ffda-rationale">${esc(rationale || "")}</div>` : ""}
      <table id="ffda-list">
        <tr><th>Player</th><th>Pos</th><th>VORP</th><th>Tier</th><th>P(back)</th></tr>
        ${rows}
      </table>`;
  }

  function renderError(err) {
    const el = ensureOverlay();
    el.querySelector("#ffda-body").innerHTML =
      `<div class="ffda-error">Server unreachable. Start the backend:<br>
       <code>uvicorn app.server:app --port 8000</code><br>${esc(err || "")}</div>`;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Listen for popup commands (calibrate toggle, manual refresh, correction).
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "popup:refresh") refreshRecommendation();
    if (msg && msg.type === "popup:calibrate") state.calibrate = !!msg.value;
    if (msg && msg.type === "popup:useLlm") state.useLlm = !!msg.value;
  });

  // Initial paint so the user sees status immediately.
  refreshRecommendation();
})();
