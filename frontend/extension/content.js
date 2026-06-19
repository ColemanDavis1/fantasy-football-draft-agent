// Runs in the ISOLATED world on the ESPN draft page. Responsibilities:
//   1. Capture picks — react state (inject.js poll), websocket, fetch/XHR, DOM.
//   2. POST each new pick to the local server (via the background worker).
//   3. Detect when it's my turn and render the recommendation as an overlay.

(function () {
  "use strict";

  const state = {
    seenKeys: new Set(),
    lastRecommendation: null,
    calibrate: false,
    useLlm: true,
    expectedOverall: null,
    currentRound: null,
    myNextOverall: null,     // parsed from ESPN ("on the clock in: 1 pick")
    myPickOverall: null,     // overall I'm picking RIGHT NOW (set while my turn)
    onClockName: null,
    myName: null,            // popup config or auto-detected roster name
    liveContext: null,       // {pick_order, teams, my_team_id} from react
    draftedEspnIds: [],
    rememberedNames: new Set(),
    rememberedEspnIds: new Set(),
    numTeams: null,          // live league size observed from the picks
    r1max: 0,                // max pick_in_round seen in round 1
    sawR2: false,            // a round-2 pick has appeared (round 1 is complete)
    lastPickCount: 0,
    lastMaxOverall: 0,
    lastContextKey: null,    // dedup live-context POSTs
    preDraft: false,
    newDraftStarted: false,
  };

  const SELECTORS = {
    pickFeed: ".draft-columns, .pick-history, [class*='draftHistory'], [class*='PickHistory'], [class*='draft-board']",
    pickRow: "[class*='pick'][class*='made'], .draft-pick, li[class*='pick'], [class*='completedPick'], [class*='CompletedPick']",
    onClock: "[class*='onTheClock'], [class*='on-the-clock'], [class*='clock'], [class*='Clock']",
  };

  chrome.storage.sync.get(["calibrate", "useLlm", "selectors", "myName"], (cfg) => {
    state.calibrate = !!cfg.calibrate;
    state.useLlm = cfg.useLlm !== false;
    state.myName = (cfg.myName || "").trim() || null;
    if (cfg.selectors) Object.assign(SELECTORS, cfg.selectors);
    log("content script ready", SELECTORS);
    chrome.storage.session.get(["draftedNames", "draftedEspnIds"], (mem) => {
      (mem.draftedNames || []).forEach((n) => state.rememberedNames.add(n));
      (mem.draftedEspnIds || []).forEach((id) => state.rememberedEspnIds.add(id));
      bootstrapScan();
    });
  });

  function log(...args) {
    if (state.calibrate) console.log("%c[FFDA]", "color:#0a7", ...args);
  }

  // ---- messages from inject.js (MAIN world) -----------------------------
  window.addEventListener("message", (ev) => {
    const m = ev.data;
    if (!m || m.__ffda !== true) return;

    if (m.kind === "capture") {
      handleCapture(m.payload);
      return;
    }

    // Legacy ws-only messages (older inject builds)
    if (m.kind === "ws") handleCapture(Object.assign({ transport: "ws" }, m.payload));
  });

  function clearPreDraftIfActive() {
    if (!state.preDraft) return false;
    const hay = (document.body.innerText || "").replace(/\s+/g, " ");
    if (/on\s+the\s+clock:?\s*pick\s*\d+/i.test(hay)) { state.preDraft = false; return true; }
    if (/you are on the clock/i.test(hay)) { state.preDraft = false; return true; }
    const rm = hay.match(/round\s+(\d+)\s+of\s+\d+/i);
    if (rm && parseInt(rm[1], 10) > 1) { state.preDraft = false; return true; }
    if (state.expectedOverall && state.expectedOverall > 1) { state.preDraft = false; return true; }
    return false;
  }

  function handleCapture(p) {
    if (!p) return;

    if (p.event === "open" && state.calibrate) {
      log("transport OPEN", p.transport || "ws", p.url);
    }

    if (p.event === "clock") {
      if (p.pre_draft && !clearPreDraftIfActive()) {
        state.preDraft = true;
        state.expectedOverall = p.current_overall || 1;
        if (p.my_next_overall) state.myNextOverall = p.my_next_overall;
        if (p.my_first_pick_in_round && !state.myNextOverall) {
          state.myNextOverall = p.my_first_pick_in_round;
        }
        ensureNewDraft();
      } else {
        state.preDraft = false;
        if (p.current_overall) state.expectedOverall = p.current_overall;
        if (p.current_round) state.currentRound = p.current_round;
      }
      if (p.on_clock_name) state.onClockName = p.on_clock_name;
      if (p.my_roster_name) {
        if (!state.myName) state.myName = p.my_roster_name;
      }
      if (p.my_pick_overall) {
        state.myPickOverall = p.my_pick_overall;
        state.myNextOverall = null;
      } else if (p.my_next_overall && !p.pre_draft) {
        state.myNextOverall = p.my_next_overall;
        state.myPickOverall = null;
      }
      postLiveContext();
      checkOnClock();
      return;
    }

    if (p.event === "picks" && state.preDraft) {
      clearPreDraftIfActive();
      if (state.preDraft) return;  // ignore stale react picks before draft starts
    }

    if (p.event === "context" && p.context) {
      state.liveContext = p.context;
      postLiveContext();
      return;
    }

    if (p.event === "message" && p.data) {
      let obj;
      try { obj = JSON.parse(p.data); } catch (e) {
        if (state.calibrate) log("ws non-JSON frame", String(p.data).slice(0, 200));
        return;
      }
      if (state.calibrate) log("ws frame", obj);
      const pick = parseWsPick(obj);
      if (pick) handlePick(pick, "websocket");
      else if (state.calibrate) log("ws frame did NOT parse as a pick");
      return;
    }

    if (p.event === "picks" && Array.isArray(p.picks) && p.picks.length) {
      if (state.calibrate) log("batch picks", p.source, p.picks.length, p.picks.slice(-3));
      observeLeagueSize(p.picks);
      const maxOverall = Math.max(...p.picks.map((x) => x.overall || 0));
      const behind = state.expectedOverall && maxOverall < state.expectedOverall - 1;
      if (p.picks.length !== state.lastPickCount || maxOverall > state.lastMaxOverall
          || behind) {
        state.lastPickCount = p.picks.length;
        state.lastMaxOverall = Math.max(state.lastMaxOverall, maxOverall);
        ingestPickBatch(p.picks, p.source || "react");
      }
    }
  }

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

  async function ingestPickBatch(picks, source) {
    const sorted = picks.slice().sort((a, b) => (a.overall || 0) - (b.overall || 0));
    for (const pick of sorted) {
      const key = pick.overall != null ? `o${pick.overall}` : null;
      if (key) state.seenKeys.add(key);
    }
    rememberDrafted(null, null, sorted);
    const res = await send("syncPicks", {
      picks: sorted, useLlm: state.useLlm,
      expectedOverall: state.expectedOverall,
      draftedEspnIds: allDraftedEspnIds(),
      draftedNames: allDraftedNames(),
    });
    if (!res || !res.ok) {
      for (const pick of sorted) {
        if (pick.overall != null) state.seenKeys.delete(`o${pick.overall}`);
      }
      renderError(res && res.error);
      return;
    }
    if (res.data.recommendation) render(res.data.recommendation);
    if (state.calibrate && res.data.unmatched) {
      log("sync UNMATCHED", res.data.unmatched, res.data.unmatched_names || []);
    }
    log("batch synced", source, res.data);
  }

  // Send live draft-room identity so snake math + my-turn track the real room.
  async function postLiveContext() {
    const ctx = state.liveContext || {};
    const payload = {
      on_clock_name: state.onClockName,
      on_clock_overall: state.expectedOverall,
      my_pick_overall: state.myPickOverall,   // set only while it's my turn
      my_next_overall: state.myNextOverall,
      pre_draft: state.preDraft,
      my_first_pick_in_round: state.preDraft ? state.myNextOverall : null,
      num_teams: state.numTeams || (ctx.pick_order && ctx.pick_order.length)
        || (ctx.teams && ctx.teams.length) || null,
      teams: ctx.teams || null,
      pick_order: ctx.pick_order || null,
      my_team_id: ctx.my_team_id != null ? ctx.my_team_id : null,
      my_name: state.myName,
      drafted_espn_ids: allDraftedEspnIds(),
      drafted_names: allDraftedNames(),
      use_llm: state.useLlm,
    };
    const key = JSON.stringify([payload.on_clock_name, payload.on_clock_overall,
      payload.my_pick_overall, payload.my_next_overall, payload.num_teams,
      payload.pick_order, payload.my_team_id, payload.my_name, payload.pre_draft]);
    if (key === state.lastContextKey) return;  // nothing identity-relevant changed
    state.lastContextKey = key;
    const res = await send("liveContext", { context: payload });
    if (res && res.ok) {
      if (res.data.recommendation) render(res.data.recommendation);
      log("live-context", res.data);
    }
  }

  async function ensureNewDraft() {
    if (state.newDraftStarted) return;
    state.newDraftStarted = true;
    state.seenKeys.clear();
    state.lastPickCount = 0;
    state.lastMaxOverall = 0;
    state.rememberedNames.clear();
    state.rememberedEspnIds.clear();
    chrome.storage.session.remove(["draftedNames", "draftedEspnIds"]);
    const res = await send("newDraft");
    if (res && res.ok) {
      log("new draft — cleared stale picks", res.data);
      if (res.data.recommendation) render(res.data.recommendation);
    }
  }

  function persistDraftedMemory() {
    chrome.storage.session.set({
      draftedNames: [...state.rememberedNames],
      draftedEspnIds: [...state.rememberedEspnIds],
    });
  }

  function rememberDrafted(names, espnIds, picks) {
    (names || []).forEach((n) => state.rememberedNames.add(n));
    (espnIds || []).forEach((id) => state.rememberedEspnIds.add(String(id)));
    (picks || []).forEach((p) => { if (p.name) state.rememberedNames.add(p.name); });
    persistDraftedMemory();
  }

  function allDraftedNames() {
    const names = new Set(state.rememberedNames);
    scrapeDraftedNames().forEach((n) => names.add(n));
    return [...names];
  }

  function allDraftedEspnIds() {
    const ids = new Set(state.rememberedEspnIds);
    scrapeDraftedEspnIds().forEach((id) => ids.add(id));
    return [...ids];
  }

  function draftedQueryParams() {
    const names = allDraftedNames();
    const ids = allDraftedEspnIds();
    const parts = [];
    if (names.length) parts.push("drafted_names=" + encodeURIComponent(names.join("|")));
    if (ids.length) parts.push("drafted_espn_ids=" + encodeURIComponent(ids.join("|")));
    return parts.length ? "&" + parts.join("&") : "";
  }
  // Pull their ESPN ids so the server keeps them out of the shortlist even if
  // their pick hasn't synced yet. Safe to return [] when nothing matches.
  // Names of players ESPN marks DRAFTED (fallback when espn id isn't in the DOM).
  function scrapeDraftedNames() {
    const names = new Set(state.rememberedNames);
    const rows = document.querySelectorAll(
      "[class*='player'], [class*='Player'], tr, li, button");
    rows.forEach((row) => {
      const t = (row.textContent || "").replace(/\s+/g, " ");
      const isDrafted = /\bdrafted\b/i.test(t)
        || (row.matches && row.matches("button") && /drafted/i.test(t)
            && (row.disabled || row.getAttribute("aria-disabled") === "true"));
      if (!isDrafted) return;
      const link = row.querySelector("a") || row.closest("[class*='player']")?.querySelector("a");
      const text = ((link && link.textContent) || t)
        .replace(/\bdrafted\b/gi, "").replace(/\s+/g, " ").trim();
      const m = text.match(/([A-Za-z][A-Za-z .'\-]{2,})/);
      if (m) names.add(m[1].trim());
    });
    return [...names];
  }

  function scrapeDraftedEspnIds() {
    const ids = new Set();
    const rows = document.querySelectorAll(
      "[class*='player'], [class*='Player'], tr, li");
    rows.forEach((row) => {
      if (!/\bdrafted\b/i.test(row.textContent || "")) return;
      let id = null;
      const a = row.querySelector("a[href*='/id/']");
      if (a) {
        const m = (a.getAttribute("href") || "").match(/\/id\/(\d+)/);
        if (m) id = m[1];
      }
      if (!id) {
        const el = row.querySelector(
          "[data-player-id], [data-id], [data-playerid]");
        if (el) id = el.getAttribute("data-player-id")
          || el.getAttribute("data-id") || el.getAttribute("data-playerid");
      }
      if (id) ids.add(String(id));
    });
    state.draftedEspnIds = [...ids];
    return state.draftedEspnIds;
  }

  // ---- DOM fallback + periodic rescan -----------------------------------
  const observer = new MutationObserver(() => {
    scanDom();
    checkOnClock();
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });

  function bootstrapScan() {
    scanDom();
    readExpectedPickFromDom();
    setInterval(() => {
      const prevPick = state.expectedOverall;
      readExpectedPickFromDom();
      clearPreDraftIfActive();
      scanDom();
      bootstrapTick = (bootstrapTick || 0) + 1;
      if (state.expectedOverall !== prevPick || bootstrapTick % 3 === 0) {
        state.lastContextKey = null;
        postLiveContext();
      }
    }, 2000);
  }
  let bootstrapTick = 0;

  function readExpectedPickFromDom() {
    const hay = (document.body.innerText || "").replace(/\s+/g, " ");
    const roundM = hay.match(/round\s+(\d+)\s+of\s+(\d+)/i);
    if (roundM) state.currentRound = parseInt(roundM[1], 10);

    const el = document.querySelector(SELECTORS.onClock);
    const text = ((el && el.textContent) || "").replace(/\s+/g, " ").trim();
    const m = text.match(/pick\s*[#:]?\s*(\d{1,3})\s+(.{1,40})$/i);
    if (m) {
      state.expectedOverall = parseInt(m[1], 10);
      const name = (m[2] || "").trim();
      if (name) state.onClockName = name;
      if (state.expectedOverall > 1) state.preDraft = false;
      return;
    }
    const m2 = text.match(/(?:pick)\s*[#:]?\s*(\d{1,3})\b/i)
      || hay.match(/on\s+the\s+clock:?\s*pick\s*(\d{1,3})/i);
    if (m2) {
      state.expectedOverall = parseInt(m2[1], 10);
      if (state.expectedOverall > 1) state.preDraft = false;
    }
    if (/you are on the clock/i.test(hay)) state.preDraft = false;
  }

  function scanDom() {
    const rows = new Set();
    document.querySelectorAll(SELECTORS.pickRow).forEach((el) => rows.add(el));
    document.querySelectorAll(SELECTORS.pickFeed + " *").forEach((el) => {
      const t = (el.textContent || "").replace(/\s+/g, " ").trim();
      if (t.length > 8 && t.length < 180 && t.indexOf(" / ") !== -1) rows.add(el);
    });
    rows.forEach((row) => maybePickFromRow(row));
  }

  function maybePickFromRow(row) {
    const text = (row.textContent || "").replace(/\s+/g, " ").trim();
    if (!text) return;
    if (state.calibrate) log("dom pick row", text);
    const pick = parseDomPick(text);
    if (pick) handlePick(pick, "dom");
    else if (state.calibrate) log("  (skipped: not a completed pick row)");
  }

  function parseDomPick(text) {
    if (/^round\s*\d+/i.test(text)) return null;
    if (/^pick\s*\d+\s*(auto\b|$)/i.test(text)) return null;
    if (/you are on the clock/i.test(text)) return null;

    const slash = text.indexOf(" / ");
    if (slash !== -1) {
      const name = text.slice(0, slash).trim();
      if (!name) return null;
      const rest = text.slice(slash + 3);
      const m = rest.match(/^([A-Za-z]{2,4})\s+([A-Z/]+?)R(\d+),\s*P(\d+)/);
      const out = { name: name };
      if (m) {
        out.position = normPos(m[2]);
        out.round = parseInt(m[3], 10);
        out.pick_in_round = parseInt(m[4], 10);
      }
      return out;
    }

    let m = text.match(/^R(\d+),\s*P(\d+)\s*[-–—]\s*(.+)$/i);
    if (m) {
      return {
        round: parseInt(m[1], 10),
        pick_in_round: parseInt(m[2], 10),
        name: m[3].trim(),
      };
    }

    m = text.match(/^(\d{1,3})[.)]\s+(.+?)(?:\s+(QB|RB|WR|TE|K|DEF|D\/ST|PK)\b)?\s*$/i);
    if (m && m[2].length > 2) {
      const out = { overall: parseInt(m[1], 10), name: m[2].trim() };
      if (m[3]) out.position = normPos(m[3]);
      return out;
    }

    return null;
  }

  function scrapeMyRoster() {
    const players = [];
    const blocks = document.querySelectorAll(
      "[class*='roster'], [class*='Roster'], [class*='my-team'], [class*='myTeam']");
    for (const block of blocks) {
      block.querySelectorAll(
        "[class*='player'], tr, li, [class*='athlete']").forEach((row) => {
        const t = (row.textContent || "").replace(/\s+/g, " ").trim();
        const m = t.match(/^([A-Z]\.?\s+[A-Za-z][A-Za-z .'\-]+|[A-Za-z][A-Za-z .'\-]{3,})\s+(QB|RB|WR|TE|K|DEF|D\/ST)/);
        if (m) players.push({ name: m[1].trim(), position: normPos(m[2]) });
      });
      if (players.length) break;
    }
    return players;
  }

  function normPos(p) {
    p = (p || "").toUpperCase();
    if (p === "D/ST" || p === "DST" || p === "DEF") return "DEF";
    if (p === "PK") return "K";
    return p;
  }

  // Infer the live league size from the picks: a complete round has exactly
  // num_teams picks, so once a round-2 pick appears, round 1's pick count is the
  // size. This is what gets a practice/mock draft the right snake math.
  function observeLeagueSize(picks) {
    for (const pk of picks) {
      // Exact: overall = (round-1)*N + pick_in_round  =>  N is pinned.
      if (pk.overall && pk.round >= 2 && pk.pick_in_round) {
        const span = pk.overall - pk.pick_in_round;
        if (span % (pk.round - 1) === 0 && span / (pk.round - 1) >= 2) {
          state.numTeams = span / (pk.round - 1);
        }
      }
      if (pk.round === 1 && pk.pick_in_round) {
        state.r1max = Math.max(state.r1max, pk.pick_in_round);
      }
      if (pk.round >= 2) state.sawR2 = true;
    }
    // Fallback for feeds without an overall: round-1 pick count once round 2 starts.
    if (!state.numTeams && state.sawR2 && state.r1max >= 2) state.numTeams = state.r1max;
  }

  // ---- turn detection ----------------------------------------------------
  function norm(s) {
    return String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
  }

  // Am I the one on the clock right now? Two robust signals:
  //  1. ESPN's banner shows my name (e.g. "PICK 61 Coleman" with myName=Coleman)
  //  2. A personal cue like "your pick" / "you're on the clock".
  // When true, the current overall IS my pick — the strongest identity signal,
  // so we hand it to the server to lock in my draft slot.
  function checkOnClock() {
    readExpectedPickFromDom();
    clearPreDraftIfActive();
    const el = document.querySelector(SELECTORS.onClock);
    const txt = ((el && el.textContent) || "").toLowerCase();
    const personal = /\byour pick\b|you'?re on the clock|make (your|the) pick|it'?s your turn|on the clock:?\s*you\b/.test(txt);
    const nameMatch = !!(state.myName && state.onClockName
      && norm(state.onClockName).includes(norm(state.myName))
      && norm(state.myName).length >= 2);
    const mineNow = personal || nameMatch;

    if (mineNow) {
      if (state.expectedOverall) state.myPickOverall = state.expectedOverall;
      if (!state.overlayPinnedForTurn) {
        state.overlayPinnedForTurn = true;
        state.lastContextKey = null;   // force a live-context post w/ my_pick_overall
        postLiveContext();
        refreshRecommendation();
      }
    } else {
      state.overlayPinnedForTurn = false;
      state.myPickOverall = null;
    }
  }

  // ---- server I/O --------------------------------------------------------
  function send(type, payload) {
    return new Promise((resolve) => {
      chrome.runtime.sendMessage(Object.assign({ type }, payload), resolve);
    });
  }

  async function handlePick(pick, source) {
    const key = (pick.round != null && pick.pick_in_round != null)
      ? `r${pick.round}p${pick.pick_in_round}`
      : (pick.overall != null ? `o${pick.overall}` : `n:${pick.name || pick.espn_id}`);
    if (state.seenKeys.has(key)) return;
    state.seenKeys.add(key);
    pick.source = source;
    pick.use_llm = state.useLlm;
    pick.expected_overall = state.expectedOverall;
    const res = await send("pick", { pick });
    if (!res || !res.ok) {
      state.seenKeys.delete(key);
      renderError(res && res.error);
      return;
    }
    if (res.data.recommendation) render(res.data.recommendation);
    log("pick sent", pick, "->", res.data.pick);
  }

  async function refreshRecommendation() {
    const res = await send("recommendation", {
      useLlm: state.useLlm,
      expectedOverall: state.expectedOverall,
      draftedNames: allDraftedNames(),
      draftedEspnIds: allDraftedEspnIds(),
    });
    if (res && res.ok) render(res.data);
  }

  function isCaughtUp(rec) {
    if (typeof rec.synced === "boolean") return rec.synced;  // server truth
    if (!state.expectedOverall) return rec.sync && rec.sync.in_sync;
    return rec.current_overall === state.expectedOverall;
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

  function pickLabel(overall, numTeams, roundHint) {
    if (!overall) return "";
    const n = numTeams || state.numTeams || 12;
    const rnd = roundHint || Math.floor((overall - 1) / n) + 1;
    const pir = ((overall - 1) % n) + 1;
    return `Round ${rnd}, Pick ${pir} (overall ${overall})`;
  }

  function render(rec) {
    state.lastRecommendation = rec;
    const el = ensureOverlay();
    el.classList.toggle("ffda-myturn", !!rec.is_my_turn);

    const caughtUp = isCaughtUp(rec);
    const espnPick = rec.expected_overall || state.expectedOverall;
    const numTeams = rec.num_teams || state.numTeams || 12;
    const sync = el.querySelector("#ffda-sync");
    if (caughtUp) {
      sync.textContent = "● synced";
      sync.className = "ffda-ok";
    } else if (espnPick) {
      sync.textContent = `● syncing — ${pickLabel(espnPick, numTeams, state.currentRound)} (${rec.current_overall}/${espnPick})`;
      sync.className = "ffda-warn";
    } else {
      sync.textContent = `● ${(rec.sync && rec.sync.missing_overalls || []).length} missing`;
      sync.className = rec.sync && rec.sync.in_sync ? "ffda-ok" : "ffda-warn";
    }

    const name = rec.llm ? rec.llm.pick_name : (rec.primary && rec.primary.name);
    const rationale = rec.llm ? rec.llm.rationale : rec.engine_rationale;
    const nextPick = rec.my_next_overall || state.myNextOverall;
    const currentLabel = pickLabel(
      espnPick || rec.current_overall, numTeams, state.currentRound);

    let turn;
    if (state.preDraft) {
      turn = `Pre-draft — your first pick: Round 1, Pick ${state.myNextOverall || "?"}`;
    } else if (!caughtUp && espnPick) {
      turn = `Syncing — ${currentLabel}, catching up (${rec.current_overall}/${espnPick})…`;
    } else if (rec.is_my_turn || state.myPickOverall) {
      const cur = state.myPickOverall || rec.current_overall;
      turn = `YOU ARE ON THE CLOCK — ${pickLabel(cur, numTeams, state.currentRound)}`;
    } else {
      const slot = rec.my_slot ? ` (slot ${rec.my_slot})` : "";
      const nextLabel = nextPick
        ? pickLabel(nextPick, numTeams)
        : "?";
      turn = `${currentLabel} · your next: ${nextLabel}${slot}`;
    }

    const myRoster = scrapeMyRoster();
    const rosterLine = myRoster.length
      ? `<div id="ffda-roster">Your picks: ${myRoster.map(
          (p) => esc(p.name) + " " + esc(p.position)).join(" · ")}</div>`
      : "";

    const rows = (rec.shortlist || []).map((c) => `
      <tr>
        <td class="ffda-name">${esc(c.name)}</td>
        <td>${esc(c.position)}</td>
        <td>${Math.round(c.vorp)}</td>
        <td>T${c.tier}</td>
        <td>${Math.round((c.p_available_next || 0) * 100)}%</td>
      </tr>`).join("");

    const showRec = state.preDraft || caughtUp || !!state.myPickOverall;
    el.querySelector("#ffda-body").innerHTML = `
      <div id="ffda-turn">${esc(turn)}</div>
      ${rosterLine}
      ${showRec && name ? `<div id="ffda-primary"><b>Take ${esc(name)}</b>
        ${rec.llm ? '<span class="ffda-badge">AI</span>' : ''}</div>
        <div id="ffda-rationale">${esc(rationale || "")}</div>` : ""}
      ${showRec ? `<table id="ffda-list">
        <tr><th>Player</th><th>Pos</th><th>VORP</th><th>Tier</th><th>P(back)</th></tr>
        ${rows}
      </table>` : `<div class="ffda-error" style="margin-top:6px">Waiting for pick sync…</div>`}`;
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

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "popup:refresh") refreshRecommendation();
    if (msg && msg.type === "popup:calibrate") state.calibrate = !!msg.value;
    if (msg && msg.type === "popup:useLlm") state.useLlm = !!msg.value;
    if (msg && msg.type === "popup:myName") {
      state.myName = (msg.value || "").trim() || null;
      state.lastContextKey = null;  // force a re-post with the new identity
      postLiveContext();
    }
  });

  refreshRecommendation();
})();
