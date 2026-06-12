# Changelog

<!-- <START NEW CHANGELOG ENTRY> -->

## [1.2.34] - 2026-06-12

Embedded copy icon in the link dialog.

### Changed

- The link-dialog Copy control is now a copy icon embedded inside the link input at its right edge (browser-URL-bar style) instead of the detached button shipped in 1.2.33 - no button chrome, subtle hover highlight, glyph flips to a green check for 1.2 s after copying (red on failure); design language documented in `docs/acc-crit-link-dialog-copy.md`

<!-- <END NEW CHANGELOG ENTRY> -->

## [1.2.33] - 2026-06-12

Copy button on the link dialog.

### Added

- The Share-link dialog shows a compact Copy button next to the link itself (same style as the password Copy) - the auto-copy at creation is lost as soon as anything else hits the clipboard, so the dialog now offers the link again on demand

## [1.2.32] - 2026-06-12

Per-uploader identity on request pages.

### Added

- Request uploaders get a stable server-issued short hash stored in an httpOnly cookie (`sf_uploader_<id>`); the standalone page shows a "Your uploads" list scoped to that identity, with per-file Remove buttons and name prefill
- `DELETE public/request/<id>/upload?name=...` - uploaders remove their own files; identity comes from the cookie only, so nobody can touch another uploader's pool
- Panel/CLI uploads to a connected request persist and replay the peer-minted uploader hash, so folder batches and repeat sends land under one identity
- Acceptance criteria document `docs/acc-crit-request-uploader-identity.md`

### Changed

- The uploader's typed name is now a relabelable display label (stored in a `.uploader.json` sidecar); identity is the hash, so many uploaders may share a name and renaming keeps the same pool
- Owner panel shows request uploaders as `name (hash)` to distinguish same-named uploaders; pre-identity uploads remain visible and removable
- `remove-upload` CLI command takes the uploader hash (shown by `list-request-uploads`) instead of the name
- Removed the "Request created - link copied" success toast; the link still lands on the clipboard and the new row is the feedback

### Fixed

- Concurrent uploads to the same pool no longer race on the sidecar write (unique temp file + atomic rename)

<!-- <END NEW CHANGELOG ENTRY> -->

## [1.2.31] - 2026-06-11

Bundled Claude skill and installer.

### Added

- `install-claude-skill` CLI command - installs the bundled `jupyterlab_share_files` Claude skill (a usage guide for this CLI) into `~/.claude/skills/`, asking for confirmation before writing
- The Claude skill now ships inside the package as a resource

## [1.2.30] - 2026-06-11

Zip-build spinner on the standalone page.

### Added

- Standalone download page shows a "Compressing to ZIP..." spinner overlay while the server builds the archive, for both "Download All as ZIP" and per-folder "Download ZIP" (single-file downloads are unaffected)

<!-- <END NEW CHANGELOG ENTRY> -->

## [1.2.29] - 2026-06-10

CLI file manipulation subcommands.

### Added

- `add-files <share-id> <paths...>` - copy workspace paths into an existing share (files are copied into the share's isolated pool, so later edits to the source do not change what recipients download)
- `remove-files <share-id> <names...>` - remove entries from a share by name
- `remove-upload <request-id> <uploader> <name>` - remove a single uploaded file from one of your requests

<!-- <END NEW CHANGELOG ENTRY> -->

## [1.2.28] - 2026-06-10

Proper Generate button.

### Changed

- "Generate" button in the create-share and Set/Change-password dialogs is now a standard dialog button (same style and font as Cancel/Save), sized to its label so it is no longer cropped, with the focus ring kept inside the dialog edge
- Name and password fields in those dialogs share one 32px height so they line up evenly with the button

## [1.2.27] - 2026-06-10

Smaller Generate button.

### Changed

- "Generate" button in the create-share and Set/Change-password dialogs shrunk to a small inline text button matching the share-link dialog's Copy button - centered beside the field instead of stretched to its height

## [1.2.26] - 2026-06-10

Plain compact dialog buttons.

### Changed

- "Generate" button in the create-share and Set/Change-password dialogs is now a plain compact button matching the input height, instead of a full-size JupyterLab dialog-action button that got cropped
- Password "Copy" button on the share-link dialog reduced to a small inline text button

## [1.2.24] - 2026-06-10

Standalone page themes, dialog polish, README diagram and screenshots.

### Added

- Standalone share/request page: light and dark themes with a Light / Dark / Auto switch - Auto (default) follows the system `prefers-color-scheme`, the choice persists in a cookie and applies before first paint
- README sharing-flow diagram (`.resources/sharing-flow.svg`, legible on GitHub light and dark) and screenshots of the panel, create-share dialog and standalone pages

### Changed

- "Generate" password button sized compactly so it no longer gets cropped in the create/change-password dialogs
- Link dialog shows the password directly below the link (was between the copied-confirmation and reachability lines)
- README rewritten in terse technical-documentation style - overview sentences plus factual bullets; screenshots before the feature list, flow diagram after it

## [1.2.22] - 2026-06-10

Tunnel autostart off by default, comprehensive validate.

### Added

- `cloudflare validate` now verifies every component the configuration carries instead of just the token: config completeness (missing keys named), private-URL https and public-URL/hostname match, tunnel existence/status/name on Cloudflare, the proxied CNAME routing the hostname to the tunnel, the path-restricted ingress rule, the `cloudflared` binary, and the local daemon/toggle/autostart state

### Changed

- `tunnelAutostart` defaults to OFF (was on) - a freshly started server never exposes links publicly without an explicit action; the tunnel comes up via the cloud icon, `cloudflare start`, or `cloudflare setup` itself

## [1.2.20] - 2026-06-10

Optional password protection for shares and requests, brute-force rate limiting, connector-token hardening.

### Added

- Optional password on shares and requests: set it in the create dialog (or `--password` / `--generate-password` on the CLI), change or clear it later via right-click → "Set Password..." / "Change Password..." or `set-password`; with a password set, the standalone page, manifest, downloads and uploads all require unlocking first
- Passphrase generation via `xkcdpass` - "Generate" buttons in the dialogs, `generate-password` CLI command and `api/generate-password`
- The share-link popup shows the password (when set) next to the link with its own Copy button
- Password attempts are rate limited per resource via the `limits` library: a per-minute cap plus a mandatory cooldown between attempts, generous by default (30/minute, 1s) and tunable via `c.ShareFilesConfig.password_max_attempts_per_minute` / `password_attempt_cooldown_seconds`
- Connecting to a password-protected link prompts for the password, verifies it against the peer at connect time and stores it with the connection - manifest refresh, pick-up, send-to-request and panel downloads unlock automatically from then on
- README documents the security rationale for choosing Cloudflare (outbound-only tunnel, edge HTTPS, path-restricted ingress) and the new hardening features; acceptance criteria AC-CF26-AC-CF31

### Changed

- The `cloudflared` connector receives its token via the `TUNNEL_TOKEN` environment variable instead of the command line, so it can no longer leak through `ps` / `/proc/<pid>/cmdline` on shared hosts
- Unlock tokens are HMAC-bound to the password and expire after 6 hours - changing the password instantly invalidates everyone who unlocked with the old one

## [1.2.19] - 2026-06-10

Reset from the link dialog, icon colour, own-link TLS fix.

### Added

- "Reset Cloudflare sharing settings" link at the bottom of the share-link popup (shown while a tunnel is configured): closes the popup and runs the same reset as `cloudflare reset` via the new `POST api/tunnel/reset` - credentials, tunnel state and base URLs cleared, Cloudflare-side resources kept; the cloud icon returns to its "click to set up" state

### Changed

- ALL tunnel/Cloudflare behaviour centralised in the new `tunnel` library module - the CLI (`cloudflare` subcommands) and the HTTP API (`api/tunnel*`) are thin dispatchers into it (one implementation, two frontends; new shared entry points `tunnel_start`/`tunnel_stop`/`set_tunnel_autostart`/`tunnel_state`/`tunnel_info`/`validate_config`)
- Dashed cloud silhouette uses the same colour as the other header icons (was too faint) - the dash and missing fill alone signal the off/unconfigured state

### Fixed

- Link reachability check no longer fails with "TLS verification failed" behind a self-signed hub certificate: the probed URL is the server's own, so the probe skips certificate validation - the question is reachability, not trust

## [1.2.15] - 2026-06-10

Cloudflare sharing configurable straight from the panel.

### Added

- The cloud icon is always visible in the panel header; while no tunnel is configured, clicking it opens a "Set up Cloudflare sharing" popup with the same inputs as `cloudflare setup` - API token (password field), account id, public hostname, private base URL (prefilled from the page's own address)
- Each popup field carries a hint where to take the value from: token policies in the dashboard, account id on the domain Overview page, hostname as a subdomain of a Cloudflare-managed domain, private URL from the browser bar (https required)
- `POST api/tunnel/setup` - runs the full setup server-side; the blocking Cloudflare API calls execute in a thread executor so the server stays responsive; in-progress notification with success/error outcome

### Changed

- CLI setup sequence refactored into `setup_and_start()`, shared verbatim by `cloudflare setup` and the popup endpoint (including the daemon-restart-on-token-change behaviour)

## [1.2.13] - 2026-06-10

Per-user tunnel names, link dialog polish, UI consistency.

### Added

- Tunnel name derived from the private base URL (`share-files-<sluggified URL>`, e.g. `share-files-hub-example-com-user-alice`) - deterministic so repeated setups reuse the same tunnel, unique per user/server on a shared Cloudflare account; saved to config and shown by `cloudflare info`
- Spinner in the link dialog while the reachability probe is in flight
- Settings Editor entry carries the panel's share icon (`jupyter.lab.setting-icon`)

### Changed

- Link dialog order: the link itself first, then the copy confirmation, then the reachability outcome
- Cloud-off icon is a true unfilled silhouette (fill/stroke moved onto the path - JupyterLab's `.jp-icon3[fill]` CSS re-filled it); thicker stroke, sparser dashes
- Drop zone and filter input use the same themed input background (`--neutral-fill-input-rest`) as the connect input

### Fixed

- Setup restarts the connector when the tunnel token changed - a daemon still serving the old tunnel left the new hostname dead (edge 530)
- `stop_connector` waits for the processes to actually exit, so `ensure_connector` no longer races a dying daemon and skips the relaunch

## [1.2.6] - 2026-06-10

Public/private link toggle for the Cloudflare tunnel, link reachability check in the dialog, CLI ergonomics.

### Added

- Tunnel toggle: `cloudflare start` / `stop` (and the cloud icon, now left of the filter icon) switch between public links (tunnel active, daemon running) and private links (daemon stopped) - per request, no restart; credentials, tunnel and DNS kept
- Cloud icon states: green filled = tunnel on, dim dashed silhouette = off, blinking blue = connecting; click toggles via the new `api/tunnel` endpoint
- `tunnelAutostart` setting (Settings Editor, default on) - bring the tunnel up at server startup; off starts with private links and no daemon
- Link dialog reachability check: server-side probe (`api/link-check`, kind+id only - no SSRF surface) shows "Link is reachable" / "not reachable" for the displayed link
- `cloudflare validate` also reports `cloudflared_available`/`cloudflared_path` - the extension launches the connector itself, a missing binary means the tunnel can never come up
- `cloudflare info` reports `private_base_url`, `tunnel_active` and `tunnel_autostart`
- Bare `jupyterlab_share_files` prints the full command reference (exit 0) instead of a usage error; help and human output conservatively coloured on a TTY (`NO_COLOR` honoured)

### Changed

- `--local-base-url` renamed `--private-base-url`
- Refresh icon spins only on an explicit click - background polls run without icon feedback
- Connect input background prefers `--neutral-fill-input-rest`
- `docs/acc-crit-cloudflare-integration.md` rewritten in terse technical-documentation style (AC-CF18-AC-CF22 added)

## [1.2.3] - 2026-06-10

Re-release of 1.2.2 - no functional changes.

## [1.2.2] - 2026-06-10

Cloudflare CLI restructured into four orthogonal subcommands; the extension now owns the connector daemon.

### Added

- `cloudflare setup --token --account-id --hostname --local-base-url` - save credentials and provision everything in one command
- `cloudflare validate` - end-to-end check of the saved config (token validity, bind, create rights proven by a test tunnel created and removed)
- `cloudflare info` - current configuration with tokens masked to their last 4 characters (account id in full), `daemon_running` and Cloudflare-side `tunnel_status`
- `cloudflare reset` - clear the saved token and setup state (Cloudflare-side resources kept)
- Connector daemon guaranteed by the extension: ensured at server startup and after setup, retrying `c.ShareFilesConfig.cloudflared_retries` times (new trait, default 3); failure logged as error
- Global `--json` flag - machine-readable JSON; human-readable `key: value` output by default
- Comprehensive `cloudflare --help` with examples

### Changed

- README rewritten in terse technical-documentation style; `docs/cloudflare_setup.md` updated to the subcommand structure; `ACCEPTANCE_CONNECTED_ENTRIES.md` renamed `acc-crit-basic-sharing.md`

### Removed

- `--run`, `--verify`, `--setup`, `--info`, `--reset` mode flags (replaced by the subcommands); `docs/CLOUDFLARE_SHARING.md` and `docs/UX_DESIGN.md`

## [1.2.1] - 2026-06-09

Cloudflare tunnel sharing: share/request links can now carry a public Cloudflare hostname and work for recipients outside the hub or local network.

### Added

- `jupyterlab_share_files` CLI - the panel's ten operations (create/close shares and requests, connect, pick up, send, list) as subcommands printing JSON, so scripts and AI agents can drive the extension
- `cloudflare` subcommand: `--token`/`--account_id` save credentials (chmod 600), `--verify` proves token rights (bind + create, account-owned `cfat_` tokens supported), `--setup` provisions the tunnel end to end, `--run` launches the connector, `--reset` returns to the unconfigured state
- Tunnel provisioning routes the hostname to the server address given by the mandatory `--local-base-url` (https required, never inferred), restricts the ingress to the extension's unauthenticated `/public/...` endpoints (everything else 404s at the Cloudflare edge), upserts a proxied CNAME, and enforces HTTPS via the zone's Always Use HTTPS
- `public_base_url` - written by `--setup`, read by the server per request (mtime-cached, no restart) to rewrite the scheme+host of generated links; also available as a `ShareFilesConfig` trait override
- Cloud icon in the panel header when a public base URL is active (`api/info` now reports `public_base_url`)
- In-progress notification while a share/request link is being created
- Own Cloudflare links are recognised by the self-connect guard
- `docs/cloudflare_setup.md` - required token policies and configuration guide
- Recorded-response test suite replaying real Cloudflare API envelopes (`tests/fixtures/cloudflare_responses.json`, secrets redacted)

### Removed

- MCP server (`jupyterlab-share-files-mcp`) and the `mcp` dependency - agents use the CLI instead; the HTTP client functions moved into `cli.py`

## [1.1.4] - 2026-06-02

### Added

- Copy/paste between the panel and the file browser: native `filebrowser:copy`/`cut` mirror into an extension clipboard; panel entries (local and connected) gain Copy, shares and connected requests gain Paste

## [1.1.3] - 2026-05-31

### Added

- QR code in the share-link dialog so a phone on the same network can scan a link directly

## [1.1.2] - 2026-05-31

### Added

- `c.ShareFilesConfig.verify_peer_tls` - saves/uploads to peers behind a self-signed certificate fail with a clean 502 and guidance instead of an unhandled error

### Fixed

- Double-clicking a connected (remote) file now opens it in JupyterLab instead of downloading; right-click offers Download and Save

## [1.1.1] - 2026-05-30

### Added

- MCP server `jupyterlab-share-files-mcp` for agent access (removed again in 1.2.0 in favour of the CLI)
- Drag files out of a connected (remote) share into the file browser
- `pollIntervalSeconds` setting for the panel refresh tick

## [1.0.37] - 2026-05-29

### Fixed

- Connection links are persisted verbatim and never reconstructed, fixing connections shown offline while available
- Connected-share downloads no longer navigate with credentials (removes the cross-user spawn prompt); per-row disconnect icon added

## [1.0.36] - 2026-05-29

### Added

- Drag share entries onto the file browser's current view (including empty area) and onto dock tabs to open them

## [1.0.35] - 2026-05-29

### Fixed

- Drag-source hardened against the native HTML5 drag race; frontend test parity for self-connect detection

## [1.0.34] - 2026-05-29

### Fixed

- Self-connect detection on JupyterHub compares the full `/user/<name>/` prefix, so other users' links on the same host connect correctly

## [1.0.33] - 2026-05-29

### Changed

- Minimal on-disk manifest (`{id, name}` + derived fields at read time); atomic manifest writes; thread-safe concurrent uploads

## [1.0.32] - 2026-05-29

### Changed

- `shares_dir` must resolve inside the notebook root; the extension refuses to start otherwise

## [1.0.29] - [1.0.31] - 2026-05-29

### Added

- Filter input for shares/requests, finalised as a funnel-icon toolbar toggle (1.0.29-1.0.31)

## [1.0.21] - [1.0.26] - 2026-05-29

### Added

- Drag panel entries to the file browser to copy (1.0.24), working drag-out with sidecar manifest layout (1.0.26)
- Drill into shared folders, hidden-files visibility setting, gentler drop targets (1.0.21)

### Fixed

- No text-select on entry rows; drop-zone matches row treatment (1.0.22)

## [1.0.16] - 2026-05-29

### Added

- Navigate into shares, double-click to open files, self-connect dialog

## [1.0.5] - [1.0.12] - 2026-05-28

### Added

- Copy-to-current-folder and show-in-file-browser actions on panel entries (1.0.5)
- Trash-aware deletes and HTTPS-aware links (1.0.7)
- Visible refresh spin and tactile press feedback (1.0.12)

### Fixed

- Panel overflow, link-popup font size, section-header box-sizing (1.0.8, 1.0.10)

## 0.6.22

CI hardening release - all GitHub Actions workflows now green on `main`.

- **ci**: configure `check-links` action to ignore `pepy.tech` and `npm/PyPI` badge URLs that 404 for ~24h after a fresh publish
- **ci**: restore the boilerplate `console.log('JupyterLab extension jupyterlab_share_files_extension is activated!')` that the Playwright integration test asserts on
- **ci**: include `scripts/*.js` and `schema/*.json` in the npm tarball's `files` allow-list so `jupyter-releaser check-npm` can run the `postinstall` hook without `MODULE_NOT_FOUND`

No runtime changes since 0.6.21.

## 0.6.21

First publicly published release with the full peer-to-peer file sharing feature set.

### Features

- Side panel on the right rail with three foldable sections: **My Shares**, **My Requests**, **Connected**
- **Shares (file drops)** - create read-only file/folder snapshots that anyone with the link can download
- **Requests (inboxes)** - create landing zones that anyone with the link can upload to, organised per uploader
- **Connections** - paste another peer's link to subscribe to their share or upload to their request
- File browser context-menu **"Share Files..."** for the selected items
- Drag-and-drop from the file browser onto the drop-zone (new share), an existing share row (add files), or a connected request (upload)
- Hover-revealed inline **copy-link** icon (flashes green + opens a popup with the selectable URL) and **delete** icon (red on hover, opens confirmation)
- Self-contained standalone HTML page for non-JupyterLab recipients (download buttons for shares, drag-drop upload zone for requests)
- Live upload notifications via JupyterLab's notification API
- Folder support with directory structure preserved on share and on upload
- Symlink-friendly - sharing `@shared/...` and similar works transparently
- **Settings → Settings Editor → Share Files** to toggle `enableShares` and `enableRequests` independently (both default on)

### Storage

- Default storage at `<server_root_dir>/uploads/` with human-readable `<slug>-<id>/` folder layout
- Configurable via `c.ShareFilesConfig.shares_dir` in `jupyter_server_config.py`
- Filesystem-as-source-of-truth - delete a share folder from the file browser and the panel cleans up on next poll
- Backward-compatible lookup for legacy plain-id folder names

### UI

- Theme-aware - inherits JupyterLab font family, font sizes, `--jp-layout-color*`, `--jp-ui-font-color*`, `--jp-brand-color1`, `--jp-success-color1`, `--jp-error-color1`
- Mirrors `jupyterlab_claude_code_extension` dimensions and conventions (24px header, 24px row height, uppercase `--jp-ui-font-size0` titles, `▾`/`▸` caret twisties)
- Refresh icon spins during in-flight refreshes via the shared `jp-ShareFilesPanel-spin` keyframe
- Secondary text uses `--jp-ui-font-color2` (mid-tone) for consistent toned-down look without opacity hacks

### Security

- 8-character base32 token (40 bits of entropy) is the credential - no extra password by default
- Public endpoints extend plain `tornado.web.RequestHandler` (not `JupyterHandler`) so they work behind JupyterHub without 500-ing on identity prepare()
- HTTPS inherits from the JupyterHub or Jupyter server's proxy (no extra TLS configuration in the extension)
- Path traversal blocked syntactically - `..` and absolute paths rejected by `_is_safe_relative`; symlinks are followed as a deliberate feature
- Self-connect refused both frontend (compares `window.location.host`) and backend (returns 400)
- DELETE endpoints use query parameters instead of request bodies so that proxies that strip DELETE bodies (Traefik, JupyterHub) do not break removal

### Tests

- 48 backend unit tests covering ShareStore, RequestStore, ConnectionStore, helpers, configurable shares_dir, legacy folder layout, path safety, manifest re-scan
- 9 frontend jest tests for API URL builders, type shapes, host parsing
- End-to-end pytest-jupyter route tests skipped (fixture timeout in CI); storage tests cover the same logic at the layer where it matters

### Build

- `license-webpack-plugin` runtime patch applied via npm `postinstall` script (`scripts/patch-license-webpack-plugin.js`) - fixes `filename.split('=')[1].trim()` crash on webpack 5 `provide module` identifiers

## 0.1.x

Initial scaffolding from the JupyterLab extension copier template. Not published.
