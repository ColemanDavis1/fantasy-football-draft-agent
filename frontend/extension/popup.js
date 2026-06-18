const $ = (id) => document.getElementById(id);

function setStatus(text, cls) {
  const el = $("status");
  el.textContent = text;
  el.className = cls || "";
}

function bg(type, payload) {
  return new Promise((resolve) =>
    chrome.runtime.sendMessage(Object.assign({ type }, payload), resolve));
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function tellContent(type, value) {
  const tab = await activeTab();
  if (tab) chrome.tabs.sendMessage(tab.id, { type, value }).catch(() => {});
}

// Load saved settings.
chrome.storage.sync.get(["serverUrl", "useLlm", "calibrate"], (cfg) => {
  $("serverUrl").value = cfg.serverUrl || "http://localhost:8000";
  $("useLlm").checked = cfg.useLlm !== false;
  $("calibrate").checked = !!cfg.calibrate;
});

$("serverUrl").addEventListener("change", () =>
  chrome.storage.sync.set({ serverUrl: $("serverUrl").value.trim() }));

$("useLlm").addEventListener("change", () => {
  chrome.storage.sync.set({ useLlm: $("useLlm").checked });
  tellContent("popup:useLlm", $("useLlm").checked);
});

$("calibrate").addEventListener("change", () => {
  chrome.storage.sync.set({ calibrate: $("calibrate").checked });
  tellContent("popup:calibrate", $("calibrate").checked);
});

$("check").addEventListener("click", async () => {
  setStatus("Checking…");
  const res = await bg("health");
  if (!res || !res.ok) { setStatus("Cannot reach server.\n" + (res && res.error), "warn"); return; }
  const d = res.data;
  setStatus(
    `Connected.\nLeague: ${d.league_loaded ? "loaded" : "NOT loaded — run config"}\n` +
    `My team: ${d.my_team_id ?? "unset"}\nPlayers: ${d.players_loaded}\n` +
    `Picks: ${d.picks_recorded}\nAI: ${d.llm_available ? "available" : "no key (no-LLM mode)"}`,
    d.league_loaded ? "ok" : "warn");
});

$("reset").addEventListener("click", async () => {
  setStatus("Reloading league + board…");
  const res = await bg("reset");
  setStatus(res && res.ok ? "Reloaded." : "Failed: " + (res && res.error), res && res.ok ? "ok" : "warn");
});

$("refresh").addEventListener("click", () => tellContent("popup:refresh"));

$("correct").addEventListener("click", async () => {
  const overall = parseInt($("cOverall").value, 10);
  const name = $("cName").value.trim();
  if (!overall || !name) { setStatus("Enter both an overall # and a name.", "warn"); return; }
  const res = await bg("correct", { pick: { overall, name, use_llm: false } });
  if (res && res.ok && res.data.pick.ok) {
    setStatus(`Fixed pick ${overall}: ${res.data.pick.player_name}`, "ok");
  } else {
    setStatus("Could not match that name.\n" + (res && res.error || ""), "warn");
  }
});
