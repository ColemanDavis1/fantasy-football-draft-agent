# Frontend (Phase 3+)

Reserved for the capture + UI layer:

- **Browser extension / content script** on the ESPN draft page: auto-captures
  picks (websocket events preferred, DOM MutationObserver fallback), detects
  whose turn it is, pre-computes the recommendation during the pick before
  yours, and renders it as an on-page overlay. Talks to the local FastAPI
  server over `localhost`.
- **Local dashboard** (optional): full board, your roster, every opponent's
  roster, and live tendencies for the big-picture view.

Nothing here yet — Phase 1 is backend data + auto-config only.
