# Acceptance Criteria - Per-Uploader Identity on Request Pages

Uploaders to a request get a stable server-issued short hash stored in a browser cookie (`sf_uploader_<id>`); on the request page they see and manage (add, remove) only their own uploads. Identity = hash, the typed name is a relabelable display label; no cookie = new identity; many uploaders may share a name.

- [x] **Server-issued hash** - first upload with no cookie mints a 6-char base32 hash server-side, sets it as an `httpOnly` `SameSite=Lax` cookie (365 days) and returns it as `me.hash`; client-supplied identity is never trusted
  - log: 2026-06-12 implemented
- [x] **Stable identity** - subsequent uploads with the cookie land in the same pool (`requests/<id>/<hash>/`); the hash never changes for a browser
  - log: 2026-06-12 implemented
- [x] **Name is a label** - display name stored in a `.uploader.json` sidecar per pool, last write wins; renaming relabels the existing pool, never forks it
  - log: 2026-06-12 implemented
- [x] **Scoped manifest** - public manifest returns only the cookie owner's uploader entry plus `me: {hash, name}`; without a cookie `uploaders` is `[]` and `me` absent; other uploaders never exposed
  - log: 2026-06-12 implemented
- [x] **Own uploads visible** - request page shows a "Your uploads" list (name, size) populated from the manifest, refreshed after every upload and remove
  - log: 2026-06-12 implemented
- [x] **Own remove** - each listed upload has a Remove button; `DELETE public/request/<id>/upload?name=...` keyed by the cookie hash only
  - log: 2026-06-12 implemented
- [x] **Name prefill** - the name field prefills from `me.name` so a returning uploader keeps their label
  - log: 2026-06-12 implemented
- [x] **Owner view** - panel shows each uploader as `name (hhhh)` (4-char hash suffix) so same-named uploaders stay distinguishable; owner remove keyed by full hash
  - log: 2026-06-12 implemented
- [x] **Connection uploads share identity** - panel/CLI uploads to a connected request replay the persisted `uploader_hash` as a Cookie header; the hash minted on the first upload is captured from the response and stored on the connection
  - log: 2026-06-12 implemented
- [x] **Sidecar invisible** - `.uploader.json` (and crashed `.tmp` leftovers) never listed as entries, never counted, not removable
  - log: 2026-06-12 implemented
- [x] **Password independence** - identity cookie orthogonal to the unlock token; gated requests require both
  - log: 2026-06-12 implemented
- [x] **Edge: no cookie on manifest** - `uploaders: []`, no `me`, page shows "Nothing uploaded yet from this browser."
  - log: 2026-06-12 implemented
- [x] **Edge: remove without cookie** - 403 `no uploader identity`
  - log: 2026-06-12 implemented
- [x] **Edge: forged cookie** - values not matching `[A-Z2-7]{4,16}` (traversal, slashes, lowercase, overlong) treated as absent
  - log: 2026-06-12 implemented
- [x] **Edge: cookie of another request** - cookie name is scoped per request id; a hash for request A is invisible to request B
  - log: 2026-06-12 implemented
- [x] **Edge: cleared cookie / other browser** - fresh identity; prior uploads invisible and unremovable from the new identity
  - log: 2026-06-12 implemented
- [x] **Edge: same name, different hash** - distinct pools; owner sees two rows `anonymous (xxxx)` / `anonymous (yyyy)`
  - log: 2026-06-12 implemented
- [x] **Edge: concurrent same-filename uploads** - O_EXCL suffixing (`-2`, `-3`) unchanged; concurrent sidecar writes race-safe via unique temp + atomic rename
  - log: 2026-06-12 implemented, race found by concurrency tests and fixed
- [x] **Edge: pool emptied** - last file removed deletes the sidecar and the pool dir; the cookie persists, next upload recreates the pool under the same hash
  - log: 2026-06-12 implemented
- [x] **Edge: legacy uploads** - pre-identity dirs (keyed by name, no sidecar) stay visible with `hash = name = dir`; owner remove still works
  - log: 2026-06-12 implemented
- [x] **Edge: upload named `.uploader.json`** - O_EXCL collides with the sidecar, file lands as `.uploader-2.json`; sidecar never overwritten by an upload
  - log: 2026-06-12 implemented
- [ ] **Live verification** - Playwright: two browser contexts upload to one request, each sees only its own files and can remove them; owner panel shows both `name (hash)` rows
  - log: 2026-06-12 criterion added, pending live run

## API

- `POST public/request/<id>/upload?uploader=<name>` multipart files -> `{ok, count, me: {hash, name}}`; mints hash + `Set-Cookie sf_uploader_<id>` when absent; 400 no file / invalid filename, 404 unknown request
- `DELETE public/request/<id>/upload?name=<file>` -> `{ok}`; identity from cookie only; 403 no/invalid cookie, 400 missing name or invalid item, 404 not found
- `GET public/request/<id>/manifest` -> `uploaders` filtered to the caller's own pool, plus `me: {hash, name}` when known
- `GET api/requests/<id>` (owner, authenticated) -> full `uploaders: [{hash, name, entries}]`
- `DELETE api/requests/<id>/uploads?uploader=<hash>&name=<file>` (owner) - unchanged shape, `uploader` is now the hash
- `POST api/connections/<key>/upload` - persists/replays `uploader_hash` on the connection transparently
