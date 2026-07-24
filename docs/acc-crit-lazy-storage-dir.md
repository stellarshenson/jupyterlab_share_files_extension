# Acceptance Criteria - Lazy Storage Directory

The storage directory (default `<notebook_root>/uploads/`, configurable via `c.ShareFilesConfig.shares_dir`) is created only when there is something to store. A store object is constructed on every HTTP request, so eager creation put an empty `uploads/shares/` + `uploads/requests/` tree into the workspace of every user who never shared anything.

## Creation

- [x] **Nothing at construction** - constructing `ShareStore`, `RequestStore` or `ConnectionStore` creates no directory
  - log: 2026-07-25 fixed DEF-6, both eager `mkdir` calls removed
- [x] **Nothing at server start** - loading the server extension creates no directory; stores are per-request handler properties, never built at load
- [x] **Nothing on read** - `list`, `exists`, `get`, `get_password`, `resolve_data_path` and the id lookups create nothing on a missing tree
- [x] **Created on first share** - creating a share builds `<storage>/shares/` and the share round-trips
- [x] **Created on first request** - creating a request builds `<storage>/requests/` and the request round-trips
- [x] **Created on first connection** - adding a connection writes `<storage>/connections.json`
- [x] **Created on first upload** - uploading into a request from a cold start works and the upload is listed
  - log: 2026-07-25 cold-start upload test added

## Failure paths must not litter

- [x] **Failed share create leaves nothing** - a source that is missing or unsafe raises before anything is written; no storage tree, no ghost folder
  - log: 2026-07-25 fixed DEF-7, validation hoisted above the mkdir
- [x] **Edge: partly-valid source list** - `["good.txt", "gone.txt"]` raises `NotFoundError` and leaves the workspace untouched (no partial copy)
- [x] **Edge: no-op connection delete** - deleting a key that matches nothing does not write, so it cannot recreate the directory
- [x] **Edge: missing tree reads error correctly** - `get` on an unknown id raises `NotFoundError`, never a raw `FileNotFoundError` and never a false empty success

## Durability

- [x] **Atomic connections write** - `connections.json` is written temp-then-rename so an interrupt cannot leave an unparseable file that reads back as "no connections"
  - log: 2026-07-25 fixed DEF-8
- [ ] **Corrupt connections.json is recoverable** - an unparseable file should be logged and set aside, not silently read as empty
  - log: 2026-07-25 criterion added, open as DEF-9

## Notes

- Read paths guard on `root.exists()`; write paths (`create`, `add_upload`, `_atomic_write_json`, `_copy_into`, `_save`) all `mkdir(parents=True)`, so the tree appears on the first genuine write
- Route-layer coverage is not asserted - `test_routes.py` is skipped (pytest-jupyter fixture timeout), so a panel poll on a fresh workspace is verified at the store layer only
