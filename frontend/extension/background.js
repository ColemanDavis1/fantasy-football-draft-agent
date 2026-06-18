// Service worker. The single place that talks to the local server, so content
// scripts never hit cross-origin/CORS issues (extension-origin fetches with
// host_permissions are allowed). Content scripts send messages; we fetch.

const DEFAULT_SERVER = "http://localhost:8000";

async function serverBase() {
  const { serverUrl } = await chrome.storage.sync.get("serverUrl");
  return (serverUrl || DEFAULT_SERVER).replace(/\/$/, "");
}

async function call(path, options) {
  const base = await serverBase();
  const resp = await fetch(base + path, options);
  if (!resp.ok) throw new Error(`${path} -> ${resp.status}`);
  return resp.json();
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      switch (msg.type) {
        case "pick":
          sendResponse({ ok: true, data: await call("/pick", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(msg.pick),
          }) });
          break;
        case "correct":
          sendResponse({ ok: true, data: await call("/pick/correct", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(msg.pick),
          }) });
          break;
        case "recommendation":
          sendResponse({ ok: true, data: await call(
            "/recommendation?use_llm=" + (msg.useLlm ? "true" : "false")) });
          break;
        case "state":
          sendResponse({ ok: true, data: await call("/state") });
          break;
        case "sync":
          sendResponse({ ok: true, data: await call("/sync") });
          break;
        case "health":
          sendResponse({ ok: true, data: await call("/health") });
          break;
        case "reset":
          sendResponse({ ok: true, data: await call("/session/reset", { method: "POST" }) });
          break;
        default:
          sendResponse({ ok: false, error: "unknown message type" });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e) });
    }
  })();
  return true; // async sendResponse
});
