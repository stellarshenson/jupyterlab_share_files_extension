# Design - Hub Mode

The extension runs in one of two modes, decided once at server start. On a standalone JupyterLab it serves recipients itself from the notebook root. On a lab spawned by galaxahub it mounts no unauthenticated route at all and every panel action goes through the hub's fileshare API, which stages the bytes with its own transfer job and serves recipients from its own app container. The lab never holds a share byte and never carries a recipient request.

## Two modes

|                                              | Standalone                       | Hub                                                             |
| -------------------------------------------- | -------------------------------- | --------------------------------------------------------------- |
| Signal                                       | `SHARE_FILES_PUBLIC_ZONE` absent | `SHARE_FILES_PUBLIC_ZONE=hub`, injected by the hub at spawn     |
| Who can set it                               | nobody needs to                  | only the hub - the name is reserved against users and groups    |
| `api/*` routes (authenticated)               | local store                      | proxied to the hub                                              |
| `public/*` and `static/*` routes             | mounted                          | not mounted - jupyter_server answers 404                        |
| Store directory                              | created on first use             | never created                                                   |
| Per-user Cloudflare tunnel                   | optional                         | never started; the tunnel is the hub's                          |
| Peer connections                             | yes                              | no                                                              |
| Fallback when the hub contract is incomplete | n/a                              | none - stays in hub mode, hub calls fail with `hub_unavailable` |

The mode decision depends on the spawn variable alone. A missing API path or token never remounts the standalone routes - that would be the exact bypass hub mode exists to close.

```mermaid
flowchart LR
  subgraph lab[Hub-spawned lab]
    P[Share Files panel]
    X[Extension - api/* only]
  end
  H[galaxahub fileshare API]
  M[Mediator transfer job - no network]
  V[(Shares volume)]
  W[(Workspace volume)]
  A[Fileshare app container]
  R[Recipient]
  P --> X -- "Authorization: token" --> H
  H --> M
  W --> M --> V
  V --> A --> R
```

## The spawn contract

galaxahub injects three things into every lab it manages; the extension reads them once.

- **`SHARE_FILES_PUBLIC_ZONE=hub`** - selects hub mode (`hub.hub_mode()`)
- **`SHARE_FILES_HUB_API`** - the path of the hub's fileshare API (`/hub/api/fileshare`); joined with the scheme and host of `JUPYTERHUB_API_URL`; an absolute value is used verbatim (`hub.hub_api_base()`)
- **`JUPYTERHUB_API_TOKEN`** - the lab's own token; every hub request carries `Authorization: token <it>`, set in one place (`hub.HubClient.request`)
- **Hub side** - the fileshare handlers accept token authentication (`_accept_token_auth = True`, galaxahub v4.4.56); a call without a token answers 403

## What the lab mounts in hub mode

`routes.setup_route_handlers` builds one of two tables and never mixes them. The hub table (`hub_routes.hub_handlers`) holds only `APIHandler` subclasses, every method wrapped by `tornado.web.authenticated`.

| Panel call                                   | Hub call                                                                                    | Notes                                                                                    |
| -------------------------------------------- | ------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `GET api/info`                               | `GET capabilities`                                                                          | `mode: hub`, grants, refusal reason, serving verdict, password requirement, toggle state |
| `GET api/stream`                             | `GET stream`                                                                                | the change stream, one hub connection per lab (below)                                    |
| `GET api/tunnel`, `POST api/tunnel`          | `GET capabilities`, `GET items`, `PUT <kind>/<id>/cloud` per record                         | the cloud toggle - flips every record and the default                                    |
| `POST api/<kind>/<id>/cloud`                 | `PUT <kind>/<id>/cloud {cloud}`                                                             | one record's Cloudflare switch                                                           |
| `GET api/link-check`                         | `GET capabilities`                                                                          | reachable = `serving`; no probe of the link                                              |
| `GET api/shares`                             | `GET items`                                                                                 | hub rows of kind share                                                                   |
| `POST api/shares`                            | `POST shares {title, paths, password}`, then `PUT shares/<id>/cloud` while the toggle is on | paths only, never bytes; 202 becomes a `staging` row                                     |
| `GET/DELETE api/shares/<id>`                 | `GET items` / `DELETE shares/<id>`                                                          | 204 removes the record and the bytes                                                     |
| `GET api/requests`                           | `GET items` + `GET requests/<id>/uploads`                                                   | one uploader group per request                                                           |
| `POST api/requests`                          | `POST requests {title, password}`, then the same cloud switch                               | 201 `ready`                                                                              |
| `GET/DELETE api/requests/<id>`               | `GET items` / `DELETE requests/<id>`                                                        |                                                                                          |
| `GET/POST api/<kind>/<id>/password`          | `PUT <kind>/<id>/password`                                                                  | the value read back is the one set in this server process                                |
| `POST api/requests/<id>/uploads/<uid>/fetch` | `POST .../fetch {dest}`                                                                     | the lab picks a fresh directory under the folder the panel names                         |
| `GET api/generate-password`                  | none                                                                                        | local                                                                                    |

Not mounted, because the hub has no equivalent: adding or removing files on an existing share (a hub share is a snapshot), removing a single upload, peer connections, tunnel setup and reset.

## Links and the cloud toggle

Every hub record carries its own Cloudflare switch (`cloud`, galaxahub ACC-FILE-2920): off, the record serves on the hub's own address alone; on, the hub composes its link on the tunnel hostname while the group prefers it and the tunnel is registered. The hub mints every record with the switch off, and the tunnel runs only while some record has it on. The lab never composes a link; it restores the address recipients can use.

- **Hub's own address** - the hub composes a row's `url` from the Host header of the request it answers, so a lab-originated call yields the hub's internal address; an origin equal to the hub API origin is replaced by the origin the browser reached the lab on (forwarded headers, else the request host) plus the hub path `/s/<id>`
- **Tunnel address** - any other origin is the hub's choice for a record switched on and is kept as composed
- **Per-record switch** - `POST api/<kind>/<id>/cloud {cloud}` relays `PUT <kind>/<id>/cloud`; the row carries `cloud`, the panel marks a switched-on row with a small cloud beside its meta and offers "Share Through Cloudflare" / "Hub Network Only" in the row's context menu; the link dialog says when a record's link works on the hub network only
- **Toggle** - the header cloud icon is the bulk switch: `POST api/tunnel {active}` flips every record of the user through the hub and stores the default for the next one in the CLI config file (`hub_cloud`, default off); a record created while the toggle is on is switched on right after the hub minted it and answers with the url the hub composed for it
- **Refusal** - the hub refuses a switch on with `cloud_not_configured` while the owner's group policy has Cloudflare off; the toggle relays the 403 and stays off, a create leaves the row off, turns the toggle off and carries `cloud_reason` so the panel says why the link stayed on the hub network; a switch off is never refused
- **State** - `api/tunnel` and `api/info` report `tunnel_configured` (true while the hub answers - the hub decides per record), `tunnel_active` (the stored default) and `tunnel_running` (the hub is serving)

## Change stream

The hub tells a lab when any of its records changed (galaxahub ACC-FILE-2919); the panel fetches on a ring instead of on a timer, so a lab costs the hub nothing while nothing changes.

- **One hub connection per lab** - `hub_stream.RELAY` opens `GET stream` on the hub when the first panel subscribes and closes it when the last one leaves; every ring is fanned out to every open panel stream as one `changed` event, and the open itself rings so a reconnect refetches
- **Raw socket, not the tornado client** - a tornado fetch cannot be cancelled and the hub never ends the stream, so a stream the last panel left behind would hold a slot of the shared client until the hub died; the relay speaks HTTP/1.1 over an asyncio socket, de-chunks the body and closes the socket on cancel. A read that waits longer than three hub keepalives (90s) is a vanished hub and reconnects
- **Panel stream** - `GET api/stream` (authenticated, Server-Sent Events): `retry: 5000` on open, `event: changed` per ring, a keepalive comment every 25s while idle, `event: poll` when the hub has no stream route; the handler ends when the browser hangs up
- **Panel** - one `EventSource` per attached panel in hub mode; the timer is stopped while it stands. A ring schedules one fetch after 300ms so a burst costs one; the open and every reconnect fetch too, so a ring lost while disconnected is covered. `poll` puts the panel back on its timer for the session; a source the browser closed for good (a non-200 answer while the lab restarts behind the proxy) puts it on the timer until the next refresh reopens the stream
- **Older hub** - a hub without the route answers 404 once per subscription cycle: the relay remembers the verdict while a panel listens and asks again when a fresh panel subscribes; a hub that cannot be reached is retried every 5s while a panel listens
- **Standalone** - unchanged: the timer, whose interval setting now says it is the standalone and fallback cadence

## Password policy

A group may require a password on every record (galaxahub ACC-FILE-2927); `capabilities.password_required` rides on `api/info` as `hub.password_required`.

- **Create dialog** - the password field is required and starts with a generated passphrase; an emptied field keeps Create disabled, so no create reaches the hub without one
- **Relay** - the hub's 400 `password_required` on a create or a password removal is relayed with its reason, which the panel names

## Errors

The hub refuses with `{reason, message}` from a closed slug set; the lab relays the status and the slug.

- **403, 400, 404, 429, 503** - status and message kept, `reason` added when the hub named one
- **Hub unreachable** - 502 with `reason: hub_unavailable`; `api/info` still answers 200 with `hub.available: false` so the panel keeps its last view
- **Panel** - `hubReasonText` turns a slug into a sentence, including `password_required`, `cloud_not_configured` and `policy_conflict`; the New menu greys out a refused kind with the reason; a refused share shows `refused: <slug>`

## Enforcement

The invariants are pinned by tests, not by review.

- **Route table** - hub mode registers no pattern under `public/` or `static/`, no `_PublicBase` or `StaticFileHandler`; every method the extension implements on a registered handler carries `authenticated` (`tests/test_hub_mode.py`)
- **Fail closed** - hub mode with the token or the API path missing still mounts no public route (`tests/test_hub_mode.py`)
- **Handlers** - driven through a live jupyter_server against an in-memory hub: shapes, refusal relay, links, the toggle and the per-record switch, the password requirement, fetch, the panel stream (`tests/test_hub_handlers.py`)
- **Stream** - the relay's lifecycle against a scripted hub and the raw read against an in-process SSE server, including that a cancel closes the socket (`tests/test_hub_mode.py`)
- **End to end** - galata against a mock hub over HTTP: 404 on the recipient paths, panel state badges, grants, fetch, link dialog, the toggle and a row's own switch, the refusal under a policy with Cloudflare off, no timer while the stream stands and one fetch per ring, the timer fallback on an older hub, the required password (`ui-tests/tests/hub`, `ui-tests/mock_hub.py`); CI runs it beside the standalone suite
- **Standalone** - the 27-route table with its public and static routes, and every pre-existing test, unchanged

## Residual

- **Process restart** - passwords set through the panel are held in memory; after a restart the dialog shows the link without the value (the hub stores only a hash)
- **Mode is read from the environment** - a user with a shell cannot change it for the running process; a restart goes through the hub, which re-injects it. Persisting a modified extension across respawn is an image-integrity question, not this design's
- **Hub-side backstop** - the hub's proxy could refuse `jupyterlab-share-files-extension/public/*` for every user, not only download-blocked ones; that change belongs to galaxahub
