# Acceptance Criteria - File Hover Tooltip

Hovering a file or folder row in the Share Files panel shows a tooltip with the entry's name, size and modified date (and, for owned rows, its workspace path). The modified date is the filesystem `mtime` the server stamps on each entry. The owner-only fields must never cross the public boundary.

## Tooltip display

- [x] **Owner tooltip** - hovering a row in an owned share or request shows name, path, size and modified date
  - log: 2026-07-15 implemented (v1.2.37)
- [x] **Peer tooltip omits path** - a connected peer's share row tooltip shows name, size and modified - never the peer's server path
  - log: 2026-07-15 `_entryTooltip(entry, false)`
- [x] **Sub-folder rows show Modified** - drilled-in sub-entries show the Modified line like top-level rows
  - log: 2026-07-15 fixed DEF-3, map Contents API `last_modified` -> `mtime`
- [x] **Directory suffix** - directory rows render with a trailing `/` in both the owned and the connected renderer
  - log: 2026-07-15 fixed DEF-4
- [x] **Edge: unknown timestamp** - `mtime` absent or `<= 0` hides the Modified line, never renders "Invalid Date"
  - log: 2026-07-15 guarded in `_entryTooltip`

## Public manifest privacy

The unauthenticated public manifest is the recipient-facing surface; it must expose only display fields, never the owner's on-disk layout or timestamps.

- [x] **No owner path on public manifest** - the public share and request manifest never carries a top-level or per-entry `path`
  - log: 2026-07-15 fixed DEF-2, `_strip_owner_fields` at both public handlers
- [x] **No mtime on public manifest** - the public manifest never carries entry `mtime`
  - log: 2026-07-15 stripped server-side
- [x] **Display fields preserved** - public entries keep `name`, `type` and `size` so the recipient page still lists files
- [x] **Owner view unchanged** - the authenticated `api/shares` / `api/requests` responses still carry `path` (copy-to-browser) and `mtime` (Modified)
- [x] **Edge: request uploader entries** - the per-uploader entries in a request manifest are stripped the same way
  - log: 2026-07-15 covered in `tests/test_public_manifest_privacy.py`

## API

- `GET public/share/<id>/manifest` -> entries `[{name, type, size}]`; no `path`, no `mtime`
- `GET public/request/<id>/manifest` -> `uploaders[].entries [{name, type, size}]`; no `path`, no `mtime`
- `GET api/shares` (authenticated owner) -> entries `[{name, type, size, mtime, path?}]` (unchanged)
