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
    if (picks.length) forward({ event: "picks", source: "react", picks: picks });

    if (context.pick_order || context.teams || context.my_team_id != null) {
      forward({ event: "context", context: context });
    }

    const clock = readCurrentPickFromDom();
    const myNext = readMyNextPickFromDom(clock.overall);
    const myName = detectMyRosterName();
    if (clock.overall || clock.name || myNext || myName) {
      forward({ event: "clock", current_overall: clock.overall,
                on_clock_name: clock.name, my_next_overall: myNext,
                my_roster_name: myName });
    }

    const domPicks = scanPickHistoryText();
    if (domPicks.length) {
      forward({ event: "picks", source: "dom-scan", picks: domPicks });
    }
  }

  // ESPN shows "on the clock in: 1 Pick" — derive my next overall from that.
  function readMyNextPickFromDom(currentOverall) {
    const hay = (document.body.innerText || "").replace(/\s+/g, " ");
    const m = hay.match(/on the clock in:\s*(\d+)\s*pick/i);
    if (m && currentOverall) {
      return currentOverall + parseInt(m[1], 10);
    }
    // Highlighted upcoming cell in the draft strip (often green).
    const cells = document.querySelectorAll(
      "[class*='pick'], [class*='Pick'], [class*='draft'], td, li, div");
    for (const el of cells) {
      const cls = String(el.className || "");
      if (!/active|selected|current|upcoming|my|next|highlight|onclock/i.test(cls)) {
        continue;
      }
      const text = (el.textContent || "").replace(/\s+/g, " ").trim();
      const pm = text.match(/(?:pick\s*)?(\d{1,3})\b/i);
      if (pm && parseInt(pm[1], 10) > (currentOverall || 0)) {
        return parseInt(pm[1], 10);
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

  function readCurrentPickFromDom() {
    // ESPN header: "ON THE CLOCK: PICK 25 Coleman"
    const banner = document.querySelector(
      "[class*='onTheClock'], [class*='on-the-clock'], [class*='clock'], [class*='Clock']");
    const out = { overall: null, name: null };
    // Name only from the dedicated banner (body text is too noisy to trust).
    const bannerText = ((banner && banner.textContent) || "").replace(/\s+/g, " ").trim();
    const bm = bannerText.match(/pick\s*[#:]?\s*(\d{1,3})\s+(.{1,40})$/i);
    if (bm) {
      out.overall = parseInt(bm[1], 10);
      out.name = (bm[2] || "").trim() || null;
    }
    if (out.overall == null) {
      const hay = (bannerText || document.body.innerText || "").replace(/\s+/g, " ");
      const m = hay.match(/(?:on\s+the\s+clock|pick)\s*[#:]?\s*(\d{1,3})\b/i);
      if (m) out.overall = parseInt(m[1], 10);
    }
    return out;
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
