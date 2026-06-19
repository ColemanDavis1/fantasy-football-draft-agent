// Runs in the PAGE (MAIN world) at document_start.
// Mirrors draft data to content.js via window.postMessage:
//   1. WebSocket frames (preferred when ESPN uses them)
//   2. fetch/XHR JSON responses from ESPN APIs
//   3. React fiber state (poll) — most reliable for the current ESPN draft room
(function () {
  "use strict";

  function forward(payload) {
    try {
      window.postMessage({ __ffda: true, kind: "capture", payload: payload }, "*");
    } catch (e) {
      /* payload not cloneable */
    }
  }

  // ---- WebSocket mirror -------------------------------------------------
  const NativeWebSocket = window.WebSocket;
  if (NativeWebSocket && !NativeWebSocket.__ffdaPatched) {
    function PatchedWebSocket(url, protocols) {
      const ws = protocols === undefined
        ? new NativeWebSocket(url)
        : new NativeWebSocket(url, protocols);

      forward({ event: "open", url: String(url), transport: "ws" });

      ws.addEventListener("message", function (ev) {
        let data = ev.data;
        if (typeof data !== "string") return;
        forward({ event: "message", url: String(url), transport: "ws", data: data });
      });

      return ws;
    }

    PatchedWebSocket.prototype = NativeWebSocket.prototype;
    PatchedWebSocket.CONNECTING = NativeWebSocket.CONNECTING;
    PatchedWebSocket.OPEN = NativeWebSocket.OPEN;
    PatchedWebSocket.CLOSING = NativeWebSocket.CLOSING;
    PatchedWebSocket.CLOSED = NativeWebSocket.CLOSED;
    PatchedWebSocket.__ffdaPatched = true;
    window.WebSocket = PatchedWebSocket;
  }

  // ---- fetch / XHR mirror -----------------------------------------------
  function tryForwardJson(url, data, transport) {
    if (!data || typeof data !== "object") return;
    const merged = new Map();
    for (const p of extractPicks(data)) merged.set(pickKey(p), p);
    const picks = [...merged.values()];
    if (picks.length) forward({ event: "picks", source: transport, url: url, picks: picks });
  }

  const nativeFetch = window.fetch;
  if (nativeFetch && !nativeFetch.__ffdaPatched) {
    window.fetch = function (...args) {
      const url = typeof args[0] === "string" ? args[0] : (args[0] && args[0].url) || "";
      return nativeFetch.apply(this, args).then((resp) => {
        if (/espn\.com/i.test(url)) {
          resp.clone().json()
            .then((data) => tryForwardJson(url, data, "fetch"))
            .catch(() => {});
        }
        return resp;
      });
    };
    window.fetch.__ffdaPatched = true;
  }

  const xhrOpen = XMLHttpRequest.prototype.open;
  const xhrSend = XMLHttpRequest.prototype.send;
  if (!XMLHttpRequest.prototype.__ffdaPatched) {
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__ffdaUrl = String(url || "");
      return xhrOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      this.addEventListener("load", function () {
        const url = this.__ffdaUrl || "";
        if (!/espn\.com/i.test(url)) return;
        if (this.responseType && this.responseType !== "" && this.responseType !== "text") return;
        try {
          tryForwardJson(url, JSON.parse(this.responseText), "xhr");
        } catch (e) { /* ignore */ }
      });
      return xhrSend.apply(this, arguments);
    };
    XMLHttpRequest.prototype.__ffdaPatched = true;
  }

  // ---- pick extraction from arbitrary JSON / React props ----------------
  function pickPlayerId(obj) {
    return obj.playerId ?? obj.player_id ?? obj.espnPlayerId
      ?? (obj.player && (obj.player.id ?? obj.player.playerId));
  }

  function isPickLike(obj) {
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return false;
    const playerId = pickPlayerId(obj);
    if (playerId == null) return false;
    const overall = obj.overallPickNumber ?? obj.overall_pick_number ?? obj.overall;
    const round = obj.roundId ?? obj.round_id ?? obj.roundNumber ?? obj.round;
    const pir = obj.roundPickNumber ?? obj.round_pick_number ?? obj.roundPick;
    // A pick row needs an overall OR a round+pick the server can derive one from.
    return (overall != null && Number(overall) > 0)
      || (round != null && pir != null);
  }

  function normalizePick(obj) {
    const overall = obj.overallPickNumber ?? obj.overall_pick_number ?? obj.overall;
    return {
      espn_id: String(pickPlayerId(obj)),
      team_id: obj.teamId ?? obj.team_id ?? null,
      overall: overall != null ? Number(overall) : null,
      round: obj.roundId ?? obj.round_id ?? obj.roundNumber ?? obj.round ?? null,
      pick_in_round: obj.roundPickNumber ?? obj.round_pick_number ?? obj.roundPick ?? null,
    };
  }

  function pickKey(p) {
    if (p.overall != null) return "o" + p.overall;
    if (p.round != null && p.pick_in_round != null) return "r" + p.round + "p" + p.pick_in_round;
    return "id" + p.espn_id;
  }

  function extractPicks(root, maxDepth) {
    maxDepth = maxDepth || 14;
    const found = [];
    const seen = new WeakSet();

    function walk(val, depth) {
      if (val == null || depth > maxDepth) return;
      if (typeof val !== "object") return;
      if (seen.has(val)) return;
      seen.add(val);

      if (isPickLike(val)) {
        found.push(normalizePick(val));
        return;
      }
      if (Array.isArray(val)) {
        if (val.length > 0 && val.length <= 400 && val.every(isPickLike)) {
          for (const p of val) found.push(normalizePick(p));
          return;
        }
        for (const item of val) walk(item, depth + 1);
        return;
      }
      for (const k of Object.keys(val)) {
        if (k === "child" || k === "sibling" || k === "return" || k === "stateNode") continue;
        walk(val[k], depth + 1);
      }
    }

    walk(root, 0);
    return found;
  }

  // ---- React fiber scan -------------------------------------------------
  function reactFiberKey(el) {
    return Object.keys(el).find((k) =>
      k.startsWith("__reactFiber$") || k.startsWith("__reactInternalInstance$"));
  }

  function scanReact() {
    const preDraft = detectPreDraft();
    // Union picks from EVERY pick-bearing structure in the tree, not just the
    // single largest array. ESPN spreads the board across a full-history list
    // and smaller recent-picks widgets; taking only the biggest dropped the
    // newest picks and left the server lagging.
    const merged = new Map();
    const context = { pick_order: null, teams: null, my_team_id: null };
    const roots = [
      document.getElementById("espnfitt"),
      document.getElementById("root"),
      document.querySelector("[data-reactroot]"),
      document.body,
    ];

    function absorb(val) {
      if (val == null) return;
      for (const p of extractPicks(val, 10)) merged.set(pickKey(p), p);
      extractContext(val, context);
    }

    for (const el of roots) {
      if (!el) continue;
      const key = reactFiberKey(el);
      if (!key) continue;
      let fiber = el[key];
      while (fiber && fiber.return) fiber = fiber.return;

      const seenFibers = new Set();
      function walkFiber(f, depth) {
        if (!f || depth > 100 || seenFibers.has(f)) return;
        seenFibers.add(f);

        if (f.memoizedProps) absorb(f.memoizedProps);
        if (f.memoizedState) {
          let state = f.memoizedState;
          while (state) {
            if (state.memoizedState != null) absorb(state.memoizedState);
            if (state.queue && state.queue.lastRenderedState != null) {
              absorb(state.queue.lastRenderedState);
            }
            state = state.next;
          }
        }
        walkFiber(f.child, depth + 1);
        walkFiber(f.sibling, depth);
      }
      walkFiber(fiber, 0);
    }

    const picks = [...merged.values()].sort(
      (a, b) => (a.overall || 0) - (b.overall || 0));
    if (!preDraft && picks.length) {
      forward({ event: "picks", source: "react", picks: picks });
    }

    if (context.pick_order || context.teams || context.my_team_id != null) {
      forward({ event: "context", context: context });
    }

    const clock = readCurrentPickFromDom(preDraft);
    const rosterName = detectMyRosterName();
    const firstPick = preDraft ? readMyFirstPickFromDom() : null;
    const myOnClock = !preDraft && clock.overall && clock.name && rosterName
      && namesMatch(clock.name, rosterName);
    const myNext = preDraft ? (firstPick && firstPick.overall) : (
      myOnClock ? null : readMyNextPickFromDom(clock.overall, rosterName));
    const myPick = myOnClock ? clock.overall : null;

    if (clock.overall || clock.name || myNext || myPick || rosterName || preDraft) {
      forward({ event: "clock", current_overall: clock.overall,
                current_round: clock.round,
                on_clock_name: clock.name, my_next_overall: myNext,
                my_pick_overall: myPick, my_roster_name: rosterName,
                pre_draft: preDraft,
                my_first_round: firstPick && firstPick.round,
                my_first_pick_in_round: firstPick && firstPick.pick_in_round });
    }

    if (preDraft) return;  // no completed picks yet — skip all pick scraping

    const domMerged = new Map();
    for (const p of scanPickHistoryText().concat(scanDraftBoardStrip())) {
      const key = p.overall != null ? "o" + p.overall
        : (p.round != null ? "r" + p.round + "p" + p.pick_in_round : "n" + p.name);
      domMerged.set(key, p);
    }
    const domPicks = [...domMerged.values()];
    if (domPicks.length) {
      forward({ event: "picks", source: "dom-scan", picks: domPicks });
    }
  }

  function namesMatch(a, b) {
    const na = String(a || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
    const nb = String(b || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
    return na.length >= 2 && nb.length >= 2 && (na.includes(nb) || nb.includes(na));
  }

  // ESPN shows "on the clock in: 1 Pick" — derive my next overall from that.
  function readMyNextPickFromDom(currentOverall, rosterName) {
    const hay = (document.body.innerText || "").replace(/\s+/g, " ");
    const m = hay.match(/on the clock in:\s*(\d+)\s*pick/i);
    if (m && currentOverall) {
      return currentOverall + parseInt(m[1], 10);
    }
    // Draft strip: find cell with my name that's an upcoming pick (not on clock).
    if (rosterName) {
      const cells = document.querySelectorAll(
        "[class*='pick'], [class*='Pick'], [class*='draft'], td, li, div, span");
      for (const el of cells) {
        const text = (el.textContent || "").replace(/\s+/g, " ").trim();
        if (!namesMatch(text, rosterName)) continue;
        const pm = text.match(/\b(\d{1,3})\b/);
        if (pm) {
          const n = parseInt(pm[1], 10);
          if (n > (currentOverall || 0)) return n;
        }
      }
    }
    return null;
  }

  // Best-effort: read my team label from the left roster column ("COLEMAN").
  function detectMyRosterName() {
    const blocks = document.querySelectorAll(
      "[class*='roster'], [class*='Roster'], [class*='team'], [class*='Team']");
    for (const block of blocks) {
      const header = block.querySelector("h2, h3, h4, [class*='header'], [class*='name']");
      const text = ((header && header.textContent) || block.textContent || "")
        .split("\n")[0].replace(/\s+/g, " ").trim();
      if (text.length >= 2 && text.length <= 40 && /^[A-Za-z]/.test(text)
          && !/players|queue|draft|round|pick|search|filter|available/i.test(text)) {
        const players = block.querySelectorAll("[class*='player'], tr, li");
        if (players.length >= 1 && players.length <= 20) {
          return text.split(/\s+/).slice(0, 3).join(" ");
        }
      }
    }
    return null;
  }

  // Walk visible text for completed-pick rows ESPN renders as
  // "Amon-Ra St. Brown / DET WR R1, P13 - Manager".
  function scanPickHistoryText() {
    const merged = new Map();
    const re = /([A-Za-z][A-Za-z .'\-]+?)\s+\/\s+([A-Z]{2,4})\s+([A-Z/]+?)R(\d+),\s*P(\d+)/g;
    const sources = document.querySelectorAll(
      "[class*='history'], [class*='History'], [class*='pick'], [class*='Pick'], "
      + "[class*='draft'], [class*='Draft'], aside, [class*='feed']");
    const texts = [document.body.innerText];
    sources.forEach((el) => texts.push(el.innerText || ""));
    for (const raw of texts) {
      let m;
      while ((m = re.exec(raw.replace(/\s+/g, " "))) !== null) {
        const round = parseInt(m[4], 10);
        const pir = parseInt(m[5], 10);
        const key = "r" + round + "p" + pir;
        merged.set(key, {
          name: m[1].trim(),
          position: m[3].replace(/\/.*/, "").trim(),
          team: m[2],
          round: round,
          pick_in_round: pir,
        });
      }
    }
    return [...merged.values()];
  }

  // Best-effort scan for draft-room identity: the live pick order (a numeric
  // permutation of team ids), the team list, and — if exposed — my own team id.
  // Defensive: anything not found stays null and the server falls back to config.
  function extractContext(root, out, maxDepth) {
    maxDepth = maxDepth || 10;
    const seen = new WeakSet();
    function walk(val, depth) {
      if (val == null || depth > maxDepth || typeof val !== "object") return;
      if (seen.has(val)) return;
      seen.add(val);
      if (Array.isArray(val)) {
        for (const item of val) walk(item, depth + 1);
        return;
      }
      for (const k of Object.keys(val)) {
        if (k === "child" || k === "sibling" || k === "return" || k === "stateNode") continue;
        const v = val[k];
        if (!out.pick_order && /^(pick|draft)order$/i.test(k) && Array.isArray(v)
            && v.length >= 2 && v.every((n) => Number.isInteger(n))) {
          out.pick_order = v.slice();
        }
        if (out.my_team_id == null && /^(user|my|current)teamid$/i.test(k)
            && Number.isInteger(v)) {
          out.my_team_id = v;
        }
        if (!out.teams && /^teams$/i.test(k) && Array.isArray(v) && v.length >= 2
            && v.every((t) => t && typeof t === "object"
              && (t.id != null || t.teamId != null))) {
          out.teams = v.map((t) => ({
            team_id: t.id ?? t.teamId,
            name: t.name ?? t.teamName ?? t.location
              ?? [t.location, t.nickname].filter(Boolean).join(" ") ?? null,
            draft_slot: t.draftSlot ?? t.draft_slot ?? null,
          }));
        }
        walk(v, depth + 1);
      }
    }
    walk(root, 0);
  }

  function detectPreDraft() {
    const hay = (document.body.innerText || "").replace(/\s+/g, " ");

    // Draft is clearly underway — never treat as pre-draft.
    if (/on\s+the\s+clock:?\s*pick\s*\d+/i.test(hay)) return false;
    if (/you are on the clock/i.test(hay)) return false;
    const roundOf = hay.match(/round\s+(\d+)\s+of\s+\d+/i);
    if (roundOf && parseInt(roundOf[1], 10) > 1) return false;
    const history = hay.match(/R\d+,\s*P\d+/gi);
    if (history && history.length >= 2) return false;
    const clockPick = readCurrentPickFromDom(false);
    if (clockPick.overall && clockPick.overall > 1) return false;

    // Pre-draft: countdown or "your first pick" before any clock banner.
    if (/draft is about to start|drafting in|waiting for draft to begin/i.test(hay)) {
      return true;
    }
    if (/your first pick:?\s*round\s*1/i.test(hay) && !/on\s+the\s+clock/i.test(hay)) {
      return true;
    }
    // Empty rosters, no pick history, no clock yet.
    if (!/on\s+the\s+clock/i.test(hay) && /empty/i.test(hay) && !/R\d+,\s*P\d+/i.test(hay)) {
      return true;
    }
    return false;
  }

  function readMyFirstPickFromDom() {
    const hay = (document.body.innerText || "").replace(/\s+/g, " ");
    const m = hay.match(
      /your first pick:?\s*round\s*(\d+),?\s*pick\s*(\d+)/i);
    if (m) {
      const round = parseInt(m[1], 10);
      const pir = parseInt(m[2], 10);
      return { round: round, pick_in_round: pir,
               overall: round === 1 ? pir : null };
    }
    // Draft strip: highlighted cell "6 Coleman" before draft starts.
    const rosterName = detectMyRosterName();
    if (rosterName) {
      const cells = document.querySelectorAll(
        "[class*='pick'], [class*='Pick'], [class*='draft'], td, li, div, span");
      for (const el of cells) {
        const text = (el.textContent || "").replace(/\s+/g, " ").trim();
        if (!namesMatch(text, rosterName)) continue;
        const pm = text.match(/^(\d{1,2})\s+/);
        if (pm) {
          const n = parseInt(pm[1], 10);
          if (n >= 1 && n <= 20) return { round: 1, pick_in_round: n, overall: n };
        }
      }
    }
    return null;
  }

  function readCurrentPickFromDom(preDraft) {
    const out = { overall: null, name: null, round: null };
    const hay = (document.body.innerText || "").replace(/\s+/g, " ");
    const roundM = hay.match(/round\s+(\d+)\s+of\s+(\d+)/i);
    if (roundM) out.round = parseInt(roundM[1], 10);

    // Strongest: "ON THE CLOCK: PICK 63 Coleman"
    const om = hay.match(
      /on\s+the\s+clock:?\s*pick\s*(\d{1,3})\s+([A-Za-z][A-Za-z\s.'-]{1,35})/i);
    if (om) {
      out.overall = parseInt(om[1], 10);
      out.name = om[2].trim();
      return out;
    }
    const banner = document.querySelector(
      "[class*='onTheClock'], [class*='on-the-clock'], [class*='clock'], [class*='Clock']");
    const bannerText = ((banner && banner.textContent) || "").replace(/\s+/g, " ").trim();
    // Only trust banner text — never fall back to full body (picks up RK 93 etc).
    const bm = bannerText.match(/pick\s*[#:]?\s*(\d{1,3})\s+(.{1,40})$/i);
    if (bm) {
      out.overall = parseInt(bm[1], 10);
      out.name = (bm[2] || "").trim() || null;
    } else {
      const m = bannerText.match(/pick\s*[#:]?\s*(\d{1,3})\b/i);
      if (m) out.overall = parseInt(m[1], 10);
    }
    if (preDraft && !out.overall) out.overall = 1;
    return out;
  }

  // Parse the horizontal draft-board cells (completed picks + upcoming AUTO cells).
  function scanDraftBoardStrip() {
    const merged = new Map();
    const cells = document.querySelectorAll(
      "[class*='pick'], [class*='Pick'], [class*='draft'], [class*='Draft'], "
      + "td, li, [class*='cell'], [class*='slot']");
    for (const cell of cells) {
      const text = (cell.textContent || "").replace(/\s+/g, " ").trim();
      if (!text || text.length > 160 || /\bAUTO\b/i.test(text)) continue;
      // "23 Ja'Marr Chase WR" — require position suffix so we skip RK columns
      let m = text.match(
        /^(\d{1,3})\s+([A-Za-z][A-Za-z .'\-]+?)\s+(QB|RB|WR|TE|K|DEF|D\/ST)\s*$/);
      if (!m) {
        // "Ja'Marr Chase / CIN WR R1, P10" embedded in a cell
        m = text.match(/([A-Za-z][A-Za-z .'\-]+?)\s+\/\s+([A-Z]{2,4})\s+([A-Z/]+?)R(\d+),\s*P(\d+)/);
        if (m) {
          const key = "r" + m[4] + "p" + m[5];
          merged.set(key, {
            name: m[1].trim(), team: m[2],
            position: m[3].replace(/\/.*/, "").trim(),
            round: parseInt(m[4], 10), pick_in_round: parseInt(m[5], 10),
          });
        }
        continue;
      }
      const overall = parseInt(m[1], 10);
      const name = m[2].trim();
      if (name.length < 3 || /^(round|pick|auto|rd)$/i.test(name)) continue;
      const out = { overall: overall, name: name };
      if (m[3]) out.position = m[3];
      merged.set("o" + overall, out);
    }
    return [...merged.values()];
  }

  // Poll React state — ESPN's draft room keeps the full pick list here even
  // when the DOM pick feed uses unfamiliar markup.
  setInterval(scanReact, 2000);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => setTimeout(scanReact, 1000));
  } else {
    setTimeout(scanReact, 1000);
  }
})();
