# Acceptance Criteria - Panel Refresh Resilience

The panel polls the server on a timer. A transient loss of connectivity (offline, tab suspended, server restarting) must keep the last-good view and stay quiet; a genuine server error or code bug must surface. Transient is defined precisely as the API layer's `ServerConnection.NetworkError` (a wrapped failed fetch), not the whole `TypeError` hierarchy and not `navigator.onLine`.

- [x] **Transient keeps last view** - a dropped fetch preserves the last-good shares / requests / connections lists; the panel is not wiped
  - log: 2026-07-15 lists assigned only after the `Promise.all` of the three list calls resolves
- [x] **Log once per streak** - an offline streak logs a single `console.debug`, not one line per poll tick
  - log: 2026-07-15 `_networkOffline` guard
- [x] **Recovery logged once** - when the server is reachable again a single recovery `console.debug` fires and the flag clears
- [x] **Real errors surface** - a real HTTP error (`ServerConnection.ResponseError`) or an ordinary code-bug `TypeError` reaches `console.error`, never swallowed as offline
  - log: 2026-07-15 fixed DEF-1, classifier keys on `ServerConnection.NetworkError` only
- [x] **Flag reset on real error** - a real error clears `_networkOffline` so a later genuine offline streak logs its message again
- [x] **Edge: WAN down, localhost reachable** - with `navigator.onLine === false` but the local server up, a real HTTP 500 still surfaces (not misclassified transient)
- [x] **Edge: peer refresh errors isolated** - a connected-peer refresh failure is swallowed by `_refreshConnection` and never trips the panel-level offline logic
  - log: 2026-07-15 confirmed in Round-2 adversarial review
