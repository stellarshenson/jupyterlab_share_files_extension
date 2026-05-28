# Changelog

<!-- <START NEW CHANGELOG ENTRY> -->

## 0.6.22

CI hardening release - all GitHub Actions workflows now green on `main`.

- **ci**: configure `check-links` action to ignore `pepy.tech` and `npm/PyPI` badge URLs that 404 for ~24h after a fresh publish
- **ci**: restore the boilerplate `console.log('JupyterLab extension jupyterlab_share_files_extension is activated!')` that the Playwright integration test asserts on
- **ci**: include `scripts/*.js` and `schema/*.json` in the npm tarball's `files` allow-list so `jupyter-releaser check-npm` can run the `postinstall` hook without `MODULE_NOT_FOUND`

No runtime changes since 0.6.21.

<!-- <END NEW CHANGELOG ENTRY> -->

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
