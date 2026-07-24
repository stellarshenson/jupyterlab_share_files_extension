# Defects - Share Files Extension

`[ ]` open, `[x]` fixed. Dated notes under each track how it evolved. `DEF-N` is a stable inline id for cross-linking from tasks and acceptance criteria.

## Panel refresh

- [x] `DEF-1` **poll refresh swallows real server errors as "offline"** - HIGH; the background poll misreported genuine failures as a network blip, freezing the panel on stale data with nothing logged; cause: `_isTransientNetworkError` matched the whole `TypeError` hierarchy plus `!navigator.onLine`, so an ordinary code-bug `TypeError` and (when the browser reported the WAN down) a real HTTP 500 both classified as transient, and the once-per-streak guard then suppressed all further logging; fix: key solely on `err instanceof ServerConnection.NetworkError` and reset `_networkOffline` in the real-error branch; `src/widget.ts`
  - 2026-07-15 reported: adversarial review (architect + bug-hunter) - real errors logged as `console.debug('offline')`, panel stuck on last-good view, `console.error` never fired
  - 2026-07-15 fixed: narrowed classifier, flag reset on real error; Round-2 confirmation review CLEAN; jest 28 green, tsc clean; see [acc-crit Panel Refresh](acc-crit-panel-refresh.md)

## Public sharing

- [x] `DEF-2` **public manifest leaks owner filesystem metadata** - HIGH; the unauthenticated public share and request manifest exposed each entry's owner workspace-relative `path` and `mtime` to anyone with the link (internet-public over the Cloudflare tunnel); cause: `store.get()` stamps owner `path`/`mtime` on entries for the authenticated owner panel and the two public manifest handlers returned it verbatim - the panel's `includePath=false` hid `path` only in the peer tooltip, the data still crossed the wire; fix: `_strip_owner_fields()` removes top-level and per-entry `path`/`mtime` (share entries and request uploader entries) at both public manifest handlers, the authenticated owner view unchanged; `jupyterlab_share_files_extension/routes.py`
  - 2026-07-15 reported: adversarial review (architect, whole-repo) - `mtime` newly added by the hover-tooltip feature rode onto the same public payload as the pre-existing `path`
  - 2026-07-15 fixed: server-side strip at the public boundary; regression test `tests/test_public_manifest_privacy.py` (owner carries fields, public strips them); pytest 195 passed incl. 3 new; see [acc-crit Hover Tooltip](acc-crit-hover-tooltip.md#public-manifest-privacy)

## Storage

- [x] `DEF-6` **storage dir created for users who never share** - HIGH; `uploads/shares/` + `uploads/requests/` appeared in every workspace at server start, even for users who never created a share or request; cause: `BaseStore.__init__` and `ConnectionStore.__init__` called `self.root.mkdir(parents=True, exist_ok=True)`, and a store is constructed on every HTTP request; fix: removed both eager mkdirs, `ConnectionStore._save` creates the dir on first write, `ConnectionStore.remove` early-returns on a no-op delete so it cannot recreate it; `jupyterlab_share_files_extension/storage.py`
  - 2026-07-25 reported: user asked that the uploads folder be created only when needed
  - 2026-07-25 fixed: read paths already guarded on `root.exists()`, write paths already mkdir with `parents=True`; `TestLazyStorageDir` added; see [acc-crit Lazy Storage](acc-crit-lazy-storage-dir.md)
- [x] `DEF-7` **failed share create left an orphan folder and the storage tree** - MEDIUM; creating a share from a stale file-browser selection (file renamed or deleted since the context menu opened) raised `NotFoundError` but had already created `uploads/shares/<slug>-<id>/`, leaving a manifest-less ghost folder that never appears in the panel (`list` only iterates `*.json`) and is never cleaned up; cause: `ShareStore.create` mkdir'd the content dir before validating `source_paths`; fix: validate every source first, create the directory only once all sources check out; `jupyterlab_share_files_extension/storage.py`
  - 2026-07-25 reported: adversarial review (architect, whole-repo) - found empirically, defeated the DEF-6 fix
  - 2026-07-25 fixed: validation hoisted above the mkdir; two regression tests (missing source, partly-valid list)
  - 2026-07-25 extended: Round-2 review noted hoisting widens the check-to-copy window, so a failure _after_ the mkdir (source deleted mid-copy, permissions, ENOSPC, manifest write) still left a ghost; `create` now rolls back with `shutil.rmtree(share_dir, ignore_errors=True)` and re-raises; regression test monkeypatches `_copy_into` to raise
- [x] `DEF-8` **connections.json written non-atomically** - MEDIUM; `connections.json` was truncated in place before the new content was written, so an interrupt (container OOM, JupyterHub culler, restart) mid-write left a 0-length or half-written file; `_load` swallows the decode error and returns `[]`, so the panel showed no connections and the next `add` persisted only the new entry - silent loss of the user's whole connection list; cause: `ConnectionStore._save` used a plain `open(path, "w")` while every other writer used `_atomic_write_json`; fix: route `_save` through `_atomic_write_json` (temp + `os.replace`), which also creates the parent dir; `jupyterlab_share_files_extension/storage.py`
  - 2026-07-25 reported/fixed: adversarial review (bug-hunter); `_atomic_write_json` type hint widened to accept a list
- [ ] `DEF-9` **corrupt connections.json is indistinguishable from an empty one** - LOW; `ConnectionStore._load` catches `OSError`/`JSONDecodeError` and returns `[]`, so an unreadable file looks like "no connections" and the next `add` overwrites it; the DEF-8 fix makes corruption far less likely but does not make it recoverable; suggested fix: log and rename to `connections.json.corrupt` instead of collapsing both cases to `[]`; `jupyterlab_share_files_extension/storage.py`
  - 2026-07-25 reported: adversarial review (bug-hunter + architect), not fixed - out of scope for the lazy-creation change
- [ ] `DEF-13` **"refuses to start" on a bad shares_dir is not what happens** - LOW; README and `config.py` promise the extension refuses to start when `shares_dir` resolves outside the notebook root, but `jupyter_server` catches the `StorageError` and logs a warning; worse, `setup_route_handlers` runs before the validating `resolve_shares_dir`, so routes are registered and every API call then 500s while `apply_autostart` never runs; `jupyterlab_share_files_extension/__init__.py`
  - 2026-07-25 reported: adversarial review (architect), not fixed

## Public sharing (continued)

- [ ] `DEF-10` **peer-controlled slug escapes the workspace root on Save All** - HIGH security; `ConnectionSaveHandler` takes `share_slug` from the _remote peer's_ manifest and passes it to `_resolve_unique_target(dest_root, share_slug)`, which does a bare `target_dir / name`; per-entry names are validated but the slug is not, and the follow-up `wrap_dir.relative_to(workspace_root)` check is lexical so a non-normalised `../../` path passes and the handler returns `200 {"ok": true}`; a connected peer can write a tree anywhere the Jupyter process can write; note `_safe_name` is already imported into `routes.py` and never called; `jupyterlab_share_files_extension/routes.py`
  - 2026-07-25 reported: adversarial review (architect) - verified live, wrote a file outside the workspace root; NOT fixed, needs its own change
- [ ] `DEF-11` **planted directory can shadow a share's content** - MEDIUM; `_resolve_workspace_target_dir` blocks only a target equal to the shares dir itself, so a save into `uploads/shares` is accepted; combined with DEF-10 a peer chooses the folder name, and `BaseStore._path_for` resolves content by returning the first `iterdir()` hit ending in `-<id>`, so a planted `x-<id>` can shadow the real share and be served on the public download route; `jupyterlab_share_files_extension/routes.py`
  - 2026-07-25 reported: adversarial review (architect); folds into the DEF-10 fix
- [ ] `DEF-12` **public handlers ignore use_trash** - LOW; `_Base` passes the configured `use_trash` to its stores but `_PublicBase` does not, and `BaseStore.__init__` defaults it to `False` while the config trait defaults to `True`; an uploader removing their own file from the public page deletes it permanently, while the owner removing the same file from the panel sends it to trash - same operation, two policies; `jupyterlab_share_files_extension/routes.py`
  - 2026-07-25 reported: adversarial review (architect), not fixed

## Panel display

- [x] `DEF-3` **hover tooltip "Modified" missing on drilled-in sub-folder rows** - LOW; top-level share rows showed the Modified date but sub-folder rows silently dropped it within the same share; cause: `_fetchSubEntries` mapped `name`/`type`/`size`/`path` from the Contents API and discarded `last_modified`, so sub-entries had no `mtime`; fix: map `last_modified` -> `mtime` (unix seconds); `src/widget.ts`
  - 2026-07-15 reported/fixed: adversarial review; tsc clean; see [acc-crit Hover Tooltip](acc-crit-hover-tooltip.md)
- [x] `DEF-4` **connected-share rows omit the directory `/` suffix** - LOW; peer directories rendered like files because the connected renderer left off the trailing slash; cause: `_renderConnectedShareEntries` set `name.textContent = entry.name` whereas `_renderEntryRow` appends `/` for directories; fix: append `/` for directory entries; `src/widget.ts`
  - 2026-07-15 reported/fixed: adversarial review; jest 28 green

## Logging

- [x] `DEF-5` **unconditional drop debug log and log-prefix drift** - LOW; a `console.log('[share-files] lm-drop ...')` fired on every drag-drop, and the `[share-files]` prefix diverged from the `Share Files:` prefix used by the poll logging; fix: removed the `lm-drop` log, unified the `getPaths` warn prefix to `Share Files:`; `src/widget.ts`
  - 2026-07-15 reported/fixed: adversarial review; index.ts/request.ts prefixes left untouched (out of review scope)
