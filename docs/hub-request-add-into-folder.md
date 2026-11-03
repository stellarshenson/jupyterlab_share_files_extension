# Share Files extension - Hub content add into a folder v1

Status: DRAFT for the hub author, 2026-11-03.

## Contents

- [1. Request](#1.-Request)
- [2. Hub behaviour today](#2.-Hub-behaviour-today)
- [3. Requested dest field](#3.-Requested-dest-field)
- [4. Detection by the lab](#4.-Detection-by-the-lab)
- [5. Lab changes](#5.-Lab-changes)

## 1. Request

The owner of the Share Files extension wants an item dropped on a folder row of a hub share in the extension's panel, or dropped on the share while that folder is open in the panel, to land in that folder, as a drop does in the JupyterLab file browser. The lab cannot do this alone, because the content add `POST shares/<id>/content {action: add, paths, password, exclude}` takes no target folder, and the only workaround fails in two cases (section 2).

## 2. Hub behaviour today

This section gives the hub's answers measured on the DEV hub on 2026-11-03, run `wf_7a047de9-2d5`. Every add landed its item at the top of the share under the base name of its path, or landed nothing.

| Add case, on a share holding only `docs/a.txt`                                                                 | Hub answer                                   | `last_add`                    | Landed               |
| -------------------------------------------------------------------------------------------------------------- | -------------------------------------------- | ----------------------------- | -------------------- |
| No extra field (control)                                                                                       | 202                                          | `done` after 2.257 s          | `new.txt` at the top |
| Field `dest`, `folder`, `to`, `target`, `prefix`, `path` or `dir`, each set to `docs` and to `docs/` (14 adds) | 202                                          | `done` after 2.126 to 2.512 s | `new.txt` at the top |
| `paths: ["docs/new.txt"]`, no such file in the workspace                                                       | 202                                          | `refused`, `bad_filename`     | nothing              |
| `paths: ["<fixture>/pre/docs/new.txt"]`, a workspace file in a folder named `docs`                             | 202                                          | `done` after 2.132 s          | `new.txt` at the top |
| `paths: [{path: "<fixture>/new.txt", name: "docs/new.txt"}]`                                                   | 400 `each path must be inside the workspace` | unchanged                     | nothing              |
| `paths: [{src: "<fixture>/new.txt", dest: "docs/new.txt"}]`                                                    | 400 `each path must be inside the workspace` | unchanged                     | nothing              |

- **Unknown field** - the hub answers 202 to an add that carries a field it does not know, and ignores the field
- **No schema** - GET of `/hub/api/fileshare` and of `openapi.json`, `openapi.yaml`, `openapi`, `docs`, `redoc`, `swagger.json`, `swagger`, `api-docs`, `schema`, `spec` and `help` under it answered 404
- **Only self-description** - `action: move` answered 400 `action must be one of: add, remove, rename`

The only working method is an add, then a `rename` into the folder after the add settles.

| Add then rename, step                                                          | Hub answer                                       | Share after the step                                              |
| ------------------------------------------------------------------------------ | ------------------------------------------------ | ----------------------------------------------------------------- |
| Add `x.txt`                                                                    | 202 in 13 ms, `last_add` `running` 0.013 s later | unchanged                                                         |
| Rename `x.txt` → `docs/x.txt` while `last_add` is `running`                    | 409 `busy`, no `Retry-After`                     | unchanged                                                         |
| The add settles                                                                | `last_add` `done`, 2.29 s after the add          | `x.txt` at the top                                                |
| Rename `x.txt` → `docs/x.txt`                                                  | 204 in 21 ms                                     | `docs/x.txt`, 2.329 s after the add                               |
| Add `x.txt` again                                                              | 202, `last_add` `done` after 2.162 s             | `x.txt` at the top                                                |
| Add `other/x.txt` while `x.txt` is at the top                                  | 400 `name_taken` in 6 ms                         | unchanged                                                         |
| Rename `x.txt` → `docs/x.txt` while `docs/x.txt` exists                        | 400 `name_taken`                                 | unchanged, `x.txt` at the top                                     |
| Rename `x.txt` → `newdir/x.txt`, with no folder `newdir`                       | 204                                              | `newdir/x.txt`                                                    |
| Add folder `sub`, then rename `sub` → `docs/sub`                               | 202, `last_add` `done` after 2.307 s, then 204   | `docs/sub/inner.txt` and `docs/sub/deep/d.txt`, moved in one call |
| Add folder `sub` again, then rename `sub` → `docs/sub` while `docs/sub` exists | 202, then 400 `name_taken`                       | `sub` at the top, `docs/sub` unchanged                            |

- **Name at the top** - an item whose name the top of the share already holds cannot reach a folder, because the add is refused `name_taken`
- **Name in the folder** - an item whose name the folder already holds stays at the top, because the rename is refused `name_taken`

## 3. Requested dest field

This section gives the requested change: an optional field `dest` on the content add, `POST shares/<id>/content {action: add, paths, password, exclude, dest}`. The fetch calls `POST shares/<id>/fetch` and `POST requests/<id>/uploads/<upload_id>/fetch` already take a field named `dest`.

- **Base** - in the fetch calls `dest` is a workspace folder; in the add it is a share folder
- **Meaning** - `dest` is a folder path relative to the share, its segments joined by `/`, such as `docs` or `docs/sub`
- **Default** - an absent or empty `dest` means the top of the share, as today
- **New folder** - the hub creates a `dest` folder that does not exist yet, as a rename into a new folder does today
- **Landing place** - each path lands at `<dest>/<base name>`, and a folder keeps its tree below that
- **Name check** - the hub checks names against the contents of `dest`, so it no longer refuses an add into a folder over a name held at the top
- **Name taken** - a name `dest` already holds is refused 400 `name_taken`, as a name at the top is today
- **Unsafe dest** - an absolute `dest`, or one holding a `..` segment or an empty segment (`docs//sub`, `docs/`), is refused 400 with a new slug, proposed `bad_dest`
- **Dest is a file** - a `dest` in which any segment names a file of the share is refused 400 with a new slug, proposed `dest_not_folder`
- **State** - `busy`, `last_add`, `progress` and `skipped` behave as today
- **Listing** - `GET items` lists the landed files with the `dest` prefix, such as `docs/new.txt`

## 4. Detection by the lab

This section asks the hub to announce the field, because the lab cannot detect it from an answer. The hub answers 202 today to an add with a field it does not know and lands the item at the top (section 2), so a hub without `dest` would put the item at the top with no refusal.

- **Proposal** - `GET capabilities` answers `add_dest: true` on a hub that honours `dest`
- **Older hub** - with `add_dest` absent or false, the lab sends no `dest`, and every add lands at the top, as today

## 5. Lab changes

This section lists what the lab does once `GET capabilities` answers `add_dest: true`.

- **Dest** - the lab sends the folder open in the panel, or the folder row under the pointer at the drop, as `dest`
- **Drop target** - the lab marks that folder row as the drop target in hub mode, as the JupyterLab file browser marks a folder row
- **Rows in transit** - the lab draws the rows of items still copying in that folder
- **Criterion** - the lab reopens `ACC-PROG-234-FOLDER-ROWS`, its acceptance criterion for rows in transit in a folder
