# Changelog

<!-- <START NEW CHANGELOG ENTRY> -->

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

<!-- <END NEW CHANGELOG ENTRY> -->

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
