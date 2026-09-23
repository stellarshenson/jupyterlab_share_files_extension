# Hub API request - reach a record the caller does not own

The lab panel can connect to another person's share or request when it runs standalone: paste the link, and the row appears. A connected share's files and folders save into the workspace, one at a time or the whole share unpacked or as a zip; a connected request takes files and folders dropped onto its row. On a hub-managed lab the same control has nothing to call. The routes below are what blocks it.

## What was measured

Against the live hub on 2026-09-23, with the spawn token of a user who does not own the record:

- `POST shares/<id>/fetch` on another owner's id answers `404 No such share`
- `GET items` lists only the caller's own records, so the id cannot be discovered either
- the recipient page is not a substitute from inside a lab: `http://hub:8080/s/<policy>/<id>` answers the hub's sign-in page

So nothing on the management API reaches a record outside the caller's own items.

## What the lab needs

Four calls, all carrying the caller's own spawn token. The lab is built and tested against exactly this shape with a mock hub, so a change to any of it is a change on both sides.

- `GET records/<id>` - `{id, kind, title, owner, state, url, files, bytes, has_password, expires_at}` as an `items` row carries them, plus `last_upload` and `progress` described below. `401 {"reason": "password_required"}` for a protected record without a grant, `404` for an id the hub does not know, `410 {"reason": "closed"}` for one closed or expired. When the record changes - its files, its password, or its state, closed and expired included - the hub rings the change stream of every caller who has read it through `records/<id>`, so a connected row follows the owner's edits as the owner's own row does
- `POST records/<id>/unlock` with `{"password": "..."}` - answers `{"grant": "...", "expires_at": "..."}`, or `403 {"reason": "password_wrong"}`, or `429` while rate limited. The other three calls carry the grant as the header `X-Fileshare-Grant`; a lapsed grant answers `401` again and the lab unlocks once more with the password it holds
- `POST records/<id>/fetch` with `{"dest": "<workspace-relative dir>"}`, and optionally `"name": "<one entry>"` - the shape `shares/<id>/fetch` already has, writing into the caller's own workspace volume. It creates `dest` and refuses one that exists. With `name` it writes only that file, or that folder with everything under it, at `<dest>/<name>`; a name the record does not hold answers `404 {"reason": "unknown_entry"}`. Shares only
- `POST records/<id>/upload` with `{"paths": ["<workspace-relative path>", ...], "exclude": [...]}` - what a recipient does on the upload page, from the caller's workspace volume, as `shares/<id>/content` add reads it for the owner. Files and folders both. Answers `202` and copies after; while it runs, `GET records/<id>` carries `last_upload: {"state": "running"}` and `progress: {"copied", "total"}` for this caller's upload, then `last_upload: {"state": "done", "count": n}` or `{"state": "refused", "reason": "..."}`. It rings the caller's change stream once a second while the upload runs and when it settles, as an add does. Requests only; a second upload while one runs answers `409 {"reason": "busy"}`

`records/` rather than `shares/` and `requests/` because the caller does not know the kind before asking, and a hub link carries only the id.

## Authorisation

The link is the credential, as it is for a recipient. A caller who presents a valid record id may read it; a password, where the owner set one, is the second factor. The caller's spawn token identifies who is doing the reading, so the hub can log it and apply its own policy, and it is what keeps the bytes off any unauthenticated surface.

Two refusals the lab will show as they are: a record that has expired or been closed, and a record the caller already owns, which the panel answers with "you already own this" rather than a second row.

## What the lab does not need

- no listing of other people's records, and no search - only a lookup by an id the user already has
- no write to a record the caller does not own beyond the upload a recipient already makes, and never to a share
- no zip: the lab packs one itself from what a fetch writes, which is how it already handles the owner's own records

## Why it cannot be worked around

Three other gaps in this area were closed without a hub change, by staging around what the hub already offers: a zip is packed from what `shares/<id>/fetch` writes, a request's uploads are fetched one at a time into a folder the lab creates first, and one entry is moved out of a fetched record. None of that applies here, because every one of those starts from bytes the hub is willing to hand this token. For a record the caller does not own there is no such starting point. The upload has none either: the only way into another person's request today is the recipient page, which answers a lab with the hub's sign-in page.

## Acceptance criteria this unblocks

`ACC-HUBM-173` to `ACC-HUBM-180` in `docs/acc-crit.md`, of which `ACC-HUBM-175` is the one that states the constraint: the hub reads the record, never the lab.
