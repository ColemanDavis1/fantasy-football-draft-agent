// Runs in the PAGE (MAIN world) at document_start so it can wrap window.WebSocket
// BEFORE the draft room opens its socket. The draft room receives every pick as
// a websocket frame; we mirror each frame to the content script (ISOLATED world)
// via window.postMessage. This is the preferred, structured capture path —
// far more robust than scraping HTML. We never modify or send frames; read-only.
(function () {
  "use strict";
  const NativeWebSocket = window.WebSocket;
  if (!NativeWebSocket || NativeWebSocket.__ffdaPatched) return;

  function forward(payload) {
    try {
      window.postMessage({ __ffda: true, kind: "ws", payload: payload }, "*");
    } catch (e) {
      /* payload not cloneable; ignore */
    }
  }

  function PatchedWebSocket(url, protocols) {
    const ws = protocols === undefined
      ? new NativeWebSocket(url)
      : new NativeWebSocket(url, protocols);

    forward({ event: "open", url: String(url) });

    ws.addEventListener("message", function (ev) {
      // Forward raw frame data as a string; content.js parses it.
      let data = ev.data;
      if (typeof data !== "string") {
        // Binary frames are rare here; skip (stringify would lose info).
        return;
      }
      forward({ event: "message", url: String(url), data: data });
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
})();
