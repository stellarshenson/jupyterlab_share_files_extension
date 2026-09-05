# Defects - Share Files Extension

Tracked through `pm-tools`; hand edits lose the id assignment and the log line. `[ ]` open, `[x]` fixed, `[-]` rejected. Ids are `DEF-<CAT>-<N>` and permanent - the number is the one the defect was filed under, so a reference to `DEF-10` still reads true as `DEF-PUBLIC-10`. Run `pm-tools report docs/defects.md` for the standing view.

## Authors

- `@kj` Konrad Jelen

## Panel `PANEL`

The Share Files side panel - refresh loop, row rendering and hover detail

- [x] `DEF-PANEL-1` **poll refresh swallows real server errors as "offline"** - MAJOR; the background poll misreported genuine failures as a network blip, freezing the panel on stale data with nothing logged; cause: `_isTransientNetworkError` matched the whole `TypeError` hierarchy plus `!navigator.onLine`, so an ordinary code-bug `TypeError` and (when the browser reported the WAN down) a real HTTP 500 both classified as transient, and the once-per-streak guard then suppressed all further logging; fix: key solely on `err instanceof ServerConnection.NetworkError` and reset `_networkOffline` in the real-error branch; `src/widget.ts`
  - evidence: jest 28 green, tsc clean; round-2 adversarial confirmation review returned CLEAN
  - test-tags: UNIT
  - repro: stop the server mid-poll so the panel gets a 500; console stays silent and the panel keeps stale rows
  - log: 2026-07-15T00:00:00Z @kj reported: adversarial review (architect + bug-hunter) - real errors logged as `console.debug('offline')`, panel stuck on last-good view, `console.error` never fired
  - log: 2026-07-15T00:00:00Z @kj fixed: narrowed classifier, flag reset on real error; Round-2 confirmation review CLEAN; jest 28 green, tsc clean; see [acc-crit Panel Refresh](acc-crit.md)
  - log: 2026-09-01T19:50:10Z @kj edited repro (added) and test-tags (added)
  - log: 2026-09-01T19:50:23Z @kj edited evidence (added)
  - log: 2026-09-01T19:58:07Z @kj acc-crit link retargeted: the seven scoped acc-crit files were consolidated into docs/acc-crit.md; the criterion text is unchanged

- [x] `DEF-PANEL-3` **hover tooltip "Modified" missing on drilled-in sub-folder rows** - MINOR; top-level share rows showed the Modified date but sub-folder rows silently dropped it within the same share; cause: `_fetchSubEntries` mapped `name`/`type`/`size`/`path` from the Contents API and discarded `last_modified`, so sub-entries had no `mtime`; fix: map `last_modified` -> `mtime` (unix seconds); `src/widget.ts`
  - evidence: tsc clean; last_modified mapped to mtime, verified on drilled-in rows
  - test-tags: UNIT
  - repro: drill into a sub-folder of a share and hover a row; the Modified line is missing
  - log: 2026-07-15T00:00:00Z @kj reported/fixed: adversarial review; tsc clean; see [acc-crit Hover Tooltip](acc-crit.md)
  - log: 2026-09-01T19:50:10Z @kj edited repro (added) and test-tags (added)
  - log: 2026-09-01T19:50:23Z @kj edited evidence (added)
  - log: 2026-09-01T19:58:07Z @kj acc-crit link retargeted: the seven scoped acc-crit files were consolidated into docs/acc-crit.md; the criterion text is unchanged
- [x] `DEF-PANEL-4` **connected-share rows omit the directory `/` suffix** - MINOR; peer directories rendered like files because the connected renderer left off the trailing slash; cause: `_renderConnectedShareEntries` set `name.textContent = entry.name` whereas `_renderEntryRow` appends `/` for directories; fix: append `/` for directory entries; `src/widget.ts`
  - evidence: jest 28 green; directory rows render the trailing slash in both renderers
  - test-tags: UNIT
  - repro: connect to a peer share holding a directory; its row has no trailing slash
  - log: 2026-07-15T00:00:00Z @kj reported/fixed: adversarial review; jest 28 green
  - log: 2026-09-01T19:50:10Z @kj edited repro (added) and test-tags (added)
  - log: 2026-09-01T19:50:23Z @kj edited evidence (added)

## Public sharing `PUBLIC`

The unauthenticated public/\* surface - recipient pages, manifests, downloads and uploads

- [x] `DEF-PUBLIC-2` **public manifest leaks owner filesystem metadata** - MAJOR; the unauthenticated public share and request manifest exposed each entry's owner workspace-relative `path` and `mtime` to anyone with the link (internet-public over the Cloudflare tunnel); cause: `store.get()` stamps owner `path`/`mtime` on entries for the authenticated owner panel and the two public manifest handlers returned it verbatim - the panel's `includePath=false` hid `path` only in the peer tooltip, the data still crossed the wire; fix: `_strip_owner_fields()` removes top-level and per-entry `path`/`mtime` (share entries and request uploader entries) at both public manifest handlers, the authenticated owner view unchanged; `jupyterlab_share_files_extension/routes.py`
  - evidence: tests/test_public_manifest_privacy.py - owner carries path/mtime, public strips them; pytest 195 passed incl 3 new
  - test-tags: UNIT
  - repro: open a share link and fetch its /manifest; entries carry the owner's path and mtime
  - log: 2026-07-15T00:00:00Z @kj reported: adversarial review (architect, whole-repo) - `mtime` newly added by the hover-tooltip feature rode onto the same public payload as the pre-existing `path`
  - log: 2026-07-15T00:00:00Z @kj fixed: server-side strip at the public boundary; regression test `tests/test_public_manifest_privacy.py` (owner carries fields, public strips them); pytest 195 passed incl. 3 new; see [acc-crit Hover Tooltip](acc-crit.md)
  - log: 2026-09-01T19:50:10Z @kj edited repro (added) and test-tags (added)
  - log: 2026-09-01T19:50:24Z @kj edited evidence (added)
  - log: 2026-09-01T19:58:08Z @kj acc-crit link retargeted: the seven scoped acc-crit files were consolidated into docs/acc-crit.md; the criterion text is unchanged

- [ ] `DEF-PUBLIC-10` **peer-controlled slug escapes the workspace root on Save All** - CRITICAL; `ConnectionSaveHandler` takes `share_slug` from the _remote peer's_ manifest and passes it to `_resolve_unique_target(dest_root, share_slug)`, which does a bare `target_dir / name`; per-entry names are validated but the slug is not, and the follow-up `wrap_dir.relative_to(workspace_root)` check is lexical so a non-normalised `../../` path passes and the handler returns `200 {"ok": true}`; a connected peer can write a tree anywhere the Jupyter process can write; note `_safe_name` is already imported into `routes.py` and never called; fix: validate the slug through `_safe_name` and re-check after `resolve()`; `jupyterlab_share_files_extension/routes.py`
  - test-tags: MANUAL
  - repro: connect to a peer whose manifest sets share_slug to ../../escape, then press Save All
  - log: 2026-07-25T00:00:00Z @kj reported: adversarial review (architect) - verified live, wrote a file outside the workspace root; NOT fixed, needs its own change
  - log: 2026-09-01T19:49:55Z @kj edited severity
  - log: 2026-09-01T19:50:10Z @kj edited repro (added) and test-tags (added)
  - log: 2026-09-01T19:50:53Z @kj edited text
- [ ] `DEF-PUBLIC-11` **planted directory can shadow a share's content** - MEDIUM; `_resolve_workspace_target_dir` blocks only a target equal to the shares dir itself, so a save into `uploads/shares` is accepted; combined with DEF-10 a peer chooses the folder name, and `BaseStore._path_for` resolves content by returning the first `iterdir()` hit ending in `-<id>`, so a planted `x-<id>` can shadow the real share and be served on the public download route; `jupyterlab_share_files_extension/routes.py`
  - test-tags: MANUAL
  - repro: create uploads/shares/x-<id>/ by hand, then open the public download route for <id>
  - log: 2026-07-25T00:00:00Z @kj reported: adversarial review (architect); folds into the DEF-10 fix
  - log: 2026-09-01T19:50:10Z @kj edited repro (added) and test-tags (added)
- [ ] `DEF-PUBLIC-12` **public handlers ignore use_trash** - MINOR; `_Base` passes the configured `use_trash` to its stores but `_PublicBase` does not, and `BaseStore.__init__` defaults it to `False` while the config trait defaults to `True`; an uploader removing their own file from the public page deletes it permanently, while the owner removing the same file from the panel sends it to trash - same operation, two policies; `jupyterlab_share_files_extension/routes.py`
  - test-tags: MANUAL
  - repro: with use_trash on, upload to a request then remove it from the public page; the file is gone, not trashed
  - log: 2026-07-25T00:00:00Z @kj reported: adversarial review (architect), not fixed
  - log: 2026-09-01T19:50:10Z @kj edited repro (added) and test-tags (added)

- [x] `DEF-PUBLIC-16` **public manifests and pages were cacheable** - MEDIUM; three consequences: a request manifest varies by cookie (it filters `uploaders` to the caller's own pool) yet carried an ETag, no `Cache-Control` and no `Vary: Cookie`, so a shared cache keyed on URL alone could serve one uploader's file list to another; a manifest reused under heuristic freshness shows a stale file list; and the recipient page bakes `password_required` into its HTML, so a page cached before the owner set a password skips the prompt; fix: `_UncachedPublicMixin` (no ETag, `Cache-Control: no-store, no-cache, max-age=0`) on both manifest and both page handlers, downloads deliberately left cacheable; clients also send `cache: 'no-store'` (`MANIFEST_FETCH` in `src/api.ts`, `manifestInit()` in `static/standalone.html`) against peers still running an older server; `jupyterlab_share_files_extension/routes.py`
  - evidence: tests/test_manifest_cache.py and ui-tests/tests/manifest-cache.spec.ts, verified bidirectionally - 4 pass with the fix, 3 fail without
  - test-tags: UNIT, E2E
  - repro: load a request page as uploader A, then as B through a shared cache; B sees A's file list
  - log: 2026-08-05T00:00:00Z @kj reported: while investigating DEF-14 - the ORIGINAL hypothesis was that tornado's 304 reached the client and failed its `!r.ok` check
  - log: 2026-08-05T00:00:00Z @kj corrected: that hypothesis is WRONG and is recorded here so it is not re-derived. Verified in a real browser against the live server: a forced revalidation (`cache: 'no-cache'`) returns **200** to JavaScript - the browser consumes the 304 its own cache solicited and resolves with the stored 200. Only a request that sets `If-None-Match` itself sees a 304, and no client does. The fix stands on the privacy/staleness reasons above, not on the 304 story
  - log: 2026-08-05T00:00:00Z @kj fixed: `tests/test_manifest_cache.py` + `ui-tests/tests/manifest-cache.spec.ts` (Galata, verified bidirectionally - 4 pass with the fix, 3 fail without)
  - log: 2026-09-01T19:50:10Z @kj edited repro (added) and test-tags (added)
  - log: 2026-09-01T19:50:24Z @kj edited evidence (added)
- [x] `DEF-PUBLIC-17` **recipient page dead-ended on a 401** - MEDIUM; `loadShare`/`loadRequest` turned any non-ok manifest response into "Share unavailable" with no password prompt and no recovery, so a page whose embedded `password_required` flag was stale (owner added a password after the page was rendered or cached) was a dead end; fix: both loaders branch on `r.status === 401` and render the password gate; `jupyterlab_share_files_extension/static/standalone.html`
  - evidence: both loaders branch on r.status 401 and render the gate; adversarial review round-2 clean; shipped in v1.2.40
  - test-tags: UNIT
  - repro: open a share page, set a password on it as owner, reload; Share unavailable and no prompt
  - log: 2026-08-05T00:00:00Z @kj reported/fixed: adversarial review (architect)
  - log: 2026-09-01T19:50:10Z @kj edited repro (added) and test-tags (added)
  - log: 2026-09-01T19:50:24Z @kj edited evidence (added)

## Storage `STORE`

The on-disk share, request and connection stores under shares_dir

- [x] `DEF-STORE-6` **storage dir created for users who never share** - MAJOR; `uploads/shares/` + `uploads/requests/` appeared in every workspace at server start, even for users who never created a share or request; cause: `BaseStore.__init__` and `ConnectionStore.__init__` called `self.root.mkdir(parents=True, exist_ok=True)`, and a store is constructed on every HTTP request; fix: removed both eager mkdirs, `ConnectionStore._save` creates the dir on first write, `ConnectionStore.remove` early-returns on a no-op delete so it cannot recreate it; `jupyterlab_share_files_extension/storage.py`
  - evidence: TestLazyStorageDir green; a fresh workspace that never shared carries no uploads/ directory
  - test-tags: UNIT
  - repro: start the server in a workspace that has never shared; uploads/shares and uploads/requests exist
  - log: 2026-07-25T00:00:00Z @kj reported: user asked that the uploads folder be created only when needed
  - log: 2026-07-25T00:00:00Z @kj fixed: read paths already guarded on `root.exists()`, write paths already mkdir with `parents=True`; `TestLazyStorageDir` added; see [acc-crit Lazy Storage](acc-crit.md)
  - log: 2026-09-01T19:50:10Z @kj edited repro (added) and test-tags (added)
  - log: 2026-09-01T19:50:24Z @kj edited evidence (added)
  - log: 2026-09-01T19:58:08Z @kj acc-crit link retargeted: the seven scoped acc-crit files were consolidated into docs/acc-crit.md; the criterion text is unchanged
- [x] `DEF-STORE-7` **failed share create left an orphan folder and the storage tree** - MEDIUM; creating a share from a stale file-browser selection (file renamed or deleted since the context menu opened) raised `NotFoundError` but had already created `uploads/shares/<slug>-<id>/`, leaving a manifest-less ghost folder that never appears in the panel (`list` only iterates `*.json`) and is never cleaned up; cause: `ShareStore.create` mkdir'd the content dir before validating `source_paths`; fix: validate every source first, create the directory only once all sources check out; `jupyterlab_share_files_extension/storage.py`
  - evidence: two regression tests (missing source, partly-valid list) plus a rollback test monkeypatching \_copy_into to raise
  - test-tags: UNIT
  - repro: delete a selected file, then create a share from the stale selection; an empty share dir is left
  - log: 2026-07-25T00:00:00Z @kj reported: adversarial review (architect, whole-repo) - found empirically, defeated the DEF-6 fix
  - log: 2026-07-25T00:00:00Z @kj fixed: validation hoisted above the mkdir; two regression tests (missing source, partly-valid list)
  - log: 2026-07-25T00:00:00Z @kj extended: Round-2 review noted hoisting widens the check-to-copy window, so a failure _after_ the mkdir (source deleted mid-copy, permissions, ENOSPC, manifest write) still left a ghost; `create` now rolls back with `shutil.rmtree(share_dir, ignore_errors=True)` and re-raises; regression test monkeypatches `_copy_into` to raise
  - log: 2026-09-01T19:50:10Z @kj edited repro (added) and test-tags (added)
  - log: 2026-09-01T19:50:24Z @kj edited evidence (added)
- [x] `DEF-STORE-8` **connections.json written non-atomically** - MEDIUM; `connections.json` was truncated in place before the new content was written, so an interrupt (container OOM, JupyterHub culler, restart) mid-write left a 0-length or half-written file; `_load` swallows the decode error and returns `[]`, so the panel showed no connections and the next `add` persisted only the new entry - silent loss of the user's whole connection list; cause: `ConnectionStore._save` used a plain `open(path, "w")` while every other writer used `_atomic_write_json`; fix: route `_save` through `_atomic_write_json` (temp + `os.replace`), which also creates the parent dir; `jupyterlab_share_files_extension/storage.py`
  - evidence: \_save routed through \_atomic_write_json (temp + os.replace); bug-hunter review clean
  - test-tags: UNIT
  - repro: kill the server mid-write of connections.json; the file is truncated and the panel shows none
  - log: 2026-07-25T00:00:00Z @kj reported/fixed: adversarial review (bug-hunter); `_atomic_write_json` type hint widened to accept a list
  - log: 2026-09-01T19:50:10Z @kj edited repro (added) and test-tags (added)
  - log: 2026-09-01T19:50:24Z @kj edited evidence (added)
- [ ] `DEF-STORE-9` **corrupt connections.json is indistinguishable from an empty one** - MINOR; `ConnectionStore._load` catches `OSError`/`JSONDecodeError` and returns `[]`, so an unreadable file looks like "no connections" and the next `add` overwrites it; the DEF-8 fix makes corruption far less likely but does not make it recoverable; suggested fix: log and rename to `connections.json.corrupt` instead of collapsing both cases to `[]`; `jupyterlab_share_files_extension/storage.py`
  - test-tags: MANUAL
  - repro: corrupt connections.json by hand and reload the panel; it reads as empty and the next add overwrites
  - log: 2026-07-25T00:00:00Z @kj reported: adversarial review (bug-hunter + architect), not fixed - out of scope for the lazy-creation change
  - log: 2026-09-01T19:50:11Z @kj edited repro (added) and test-tags (added)
- [ ] `DEF-STORE-13` **"refuses to start" on a bad shares_dir is not what happens** - MINOR; README and `config.py` promise the extension refuses to start when `shares_dir` resolves outside the notebook root, but `jupyter_server` catches the `StorageError` and logs a warning; worse, `setup_route_handlers` runs before the validating `resolve_shares_dir`, so routes are registered and every API call then 500s while `apply_autostart` never runs; `jupyterlab_share_files_extension/__init__.py`
  - test-tags: MANUAL
  - repro: set shares_dir outside the notebook root and start the server; it starts, then every API call 500s
  - log: 2026-07-25T00:00:00Z @kj reported: adversarial review (architect), not fixed
  - log: 2026-09-01T19:50:11Z @kj edited repro (added) and test-tags (added)

## Peer connections `PEER`

Connecting to another user's link and keeping its state fresh

- [ ] `DEF-PEER-14` **pasted link connects, then shows "offline" - the owner's server was stopped** - MAJOR; ROOT CAUSE CONFIRMED; a public link is served BY the owner's single-user JupyterLab, so it dies with that server. On JupyterHub the link goes Cloudflare edge -> hub -> `/user/<name>/`, and when the owner's server is stopped (idle culler, restart, crash) the hub answers **403 with no `Access-Control-Allow-Origin` header**; the browser therefore rejects the cross-origin response before JavaScript can read its status and `fetch` rejects with a bare `TypeError: Failed to fetch`, which `_refreshConnection` turned into a silent "offline" badge. Connecting still succeeds because `ConnectionsHandler.post` probes **server-side** (`_peer_fetch`, no CORS) - hence "it connects, then goes offline"; `src/widget.ts`, architectural
  - test-tags: MANUAL
  - repro: connect to a peer link, stop the peer's server, wait one poll; the badge goes offline
  - log: 2026-08-05T00:00:00Z @kj reported: user - "paste a share or request link doesn't connect, it is offline - especially when I use cloudflare", then "session died (cloudflare) up again"
  - log: 2026-08-05T00:00:00Z @kj first hypothesis WRONG: tornado's 304 reaching the client (see DEF-16). Disproven in a real browser
  - log: 2026-08-05T00:00:00Z @kj root-caused: read the live tunnel ingress - path-restricted to `^(/user/[^/]+)?/jupyterlab-share-files-extension/public/.*` with `service: https://jupyterhub.lab.stellars-tech.eu`, so every public request is routed by the hub to the user's server. Verified on the wire: hub cannot route -> `403` with NO CORS header, `/hub/login` through the tunnel -> `404`. Verified in a real cross-origin browser fetch: owner's server running -> `{ok: true, status: 200}`; not routable -> `TypeError: Failed to fetch`
  - log: 2026-08-05T00:00:00Z @kj STILL OPEN: only the symptom is addressed (see DEF-15). An extension cannot stop the hub culling the server that hosts the link, so this stays open until the Public Zone Service lands. The badge now names the cause - "the peer's server is not answering. It is most likely stopped (JupyterHub stops idle servers); a share link only works while its owner's server is running" - plus a console warning with the link and error. The underlying architecture (public content served by a cullable per-user server) is what `docs/design-hub-public-zone.md` proposes to fix with a hub-level Public Zone Service; that remains unimplemented and is the real remedy
  - log: 2026-09-01T19:50:11Z @kj edited repro (added) and test-tags (added)
- [x] `DEF-PEER-15` **"offline" was an unattributable verdict** - MEDIUM; every peer-refresh failure (CORS rejection, mixed content, DNS, edge challenge page, expired unlock token, 404) collapsed into one badge with no log, so no user report of DEF-14 could be diagnosed; cause: bare `catch {}` in `_refreshConnection`, ratified by an acc-crit bullet that read "swallowed ... never trips the panel-level logic"; fix: log once per streak with the link and error, keep the reason in `offlineReasons` and show it in the badge tooltip; `src/widget.ts`
  - evidence: reason kept in offlineReasons and shown in the badge tooltip; jest specs cover offlineReason for TypeError, 401, 404 and placeholder inputs
  - test-tags: UNIT
  - repro: break a peer connection any way at all; one badge appears and nothing is logged
  - log: 2026-08-05T00:00:00Z @kj reported/fixed: adversarial review (architect) - "isolated from the panel-level logic" must not mean "unlogged"
  - log: 2026-09-01T19:50:11Z @kj edited repro (added) and test-tags (added)
  - log: 2026-09-01T19:50:24Z @kj edited evidence (added)

## Logging `LOGS`

Frontend console output - levels, prefixes and noise

- [x] `DEF-LOGS-5` **unconditional drop debug log and log-prefix drift** - MINOR; a `console.log('[share-files] lm-drop ...')` fired on every drag-drop, and the `[share-files]` prefix diverged from the `Share Files:` prefix used by the poll logging; fix: removed the `lm-drop` log, unified the `getPaths` warn prefix to `Share Files:`; `src/widget.ts`
  - evidence: lm-drop log removed and the getPaths warn prefix unified to 'Share Files:'; adversarial review clean
  - test-tags: MANUAL
  - repro: drag any file onto the panel; [share-files] lm-drop fires on every drop
  - log: 2026-07-15T00:00:00Z @kj reported/fixed: adversarial review; index.ts/request.ts prefixes left untouched (out of review scope)
  - log: 2026-09-01T19:50:11Z @kj edited repro (added) and test-tags (added)
  - log: 2026-09-01T19:50:24Z @kj edited evidence (added)
