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

## Panel display

- [x] `DEF-3` **hover tooltip "Modified" missing on drilled-in sub-folder rows** - LOW; top-level share rows showed the Modified date but sub-folder rows silently dropped it within the same share; cause: `_fetchSubEntries` mapped `name`/`type`/`size`/`path` from the Contents API and discarded `last_modified`, so sub-entries had no `mtime`; fix: map `last_modified` -> `mtime` (unix seconds); `src/widget.ts`
  - 2026-07-15 reported/fixed: adversarial review; tsc clean; see [acc-crit Hover Tooltip](acc-crit-hover-tooltip.md)
- [x] `DEF-4` **connected-share rows omit the directory `/` suffix** - LOW; peer directories rendered like files because the connected renderer left off the trailing slash; cause: `_renderConnectedShareEntries` set `name.textContent = entry.name` whereas `_renderEntryRow` appends `/` for directories; fix: append `/` for directory entries; `src/widget.ts`
  - 2026-07-15 reported/fixed: adversarial review; jest 28 green

## Logging

- [x] `DEF-5` **unconditional drop debug log and log-prefix drift** - LOW; a `console.log('[share-files] lm-drop ...')` fired on every drag-drop, and the `[share-files]` prefix diverged from the `Share Files:` prefix used by the poll logging; fix: removed the `lm-drop` log, unified the `getPaths` warn prefix to `Share Files:`; `src/widget.ts`
  - 2026-07-15 reported/fixed: adversarial review; index.ts/request.ts prefixes left untouched (out of review scope)
