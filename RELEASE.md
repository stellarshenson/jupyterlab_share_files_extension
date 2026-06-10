# Release

What a release of `jupyterlab_share_files_extension` consists of and how it is produced. One version number covers every artefact - npm, PyPI and the git tag always agree.

## Delivered functionality

Every release ships the complete extension - there are no feature flags, editions or partial builds. `pip install jupyterlab-share-files-extension` delivers all of:

- **Shares** - read-only drops of files and folders created from the side panel, the file browser context menu or drag-and-drop; recipients download single files, folder ZIPs or everything at once
- **Requests** - inboxes others upload into, organised per uploader, with live upload notifications and per-upload management
- **Connections** - paste a peer's link to subscribe to their share or upload to their request; pick-up to any workspace folder, send-to-request with folder structure preserved
- **Standalone HTML page** - every link works in a plain browser, no JupyterLab needed; QR code in the link dialog for phones
- **Optional password protection** - set at creation or later (context menu / `set-password`); gates the page, manifest, downloads and uploads; xkcdpass passphrase generation; unlock attempts rate limited per resource (`limits` library) with a tunable per-minute cap and cooldown; connecting to a protected link prompts, verifies against the peer and unlocks automatically afterwards
- **Cloudflare tunnel sharing** - links usable from the whole internet through an outbound-only tunnel with edge HTTPS and path-restricted ingress (only `/public/...` is routable); provisioned end to end from the panel popup or `cloudflare setup`; public/private toggle via the cloud icon or `start`/`stop`; per-user deterministic tunnel names; connector daemon guaranteed by the extension (token via `TUNNEL_TOKEN` env, never argv); server-side link reachability check in the link dialog
- **CLI** - `jupyterlab_share_files` exposes every panel operation as a subcommand (shares, requests, connections, passwords, Cloudflare lifecycle), human-readable coloured output by default, `--json` for machines
- **Panel UX** - drag-and-drop everywhere, in-share folder browsing, open-in-JupyterLab, copy/paste with the file browser, filtering, polling with configurable interval, hidden-file toggle, delete-to-trash
- **Hardening** - HTTPS-aware links, self-connect guard, path-traversal-safe storage, secrets chmod-600 and masked in output

## What goes into a release

- **npm package** - `jupyterlab_share_files_extension@<version>` ([registry](https://www.npmjs.com/package/jupyterlab_share_files_extension)): the prebuilt labextension (frontend)
- **PyPI package** - `jupyterlab-share-files-extension <version>` ([registry](https://pypi.org/project/jupyterlab-share-files-extension/)): wheel + sdist carrying the server extension, the `jupyterlab_share_files` CLI, the `tunnel` library and the bundled labextension - `pip install` alone gives the full feature set
- **Git tag** - `RELEASE_v<version>` on the release commit
- **GitHub release** - published against the tag, body mirroring the version's `CHANGELOG.md` section
- **CHANGELOG.md section** - `## [<version>] - <date>` with the content grouped under Added / Changed / Fixed; the canonical summary of the release
- **Journal entry** - `.claude/JOURNAL.md` entry stamped with the released version (rationale and verification record)

## Gates - no publish before all pass

- Journal entry written and `journal-tools check` clean
- CHANGELOG section for the version in place
- `jlpm prettier` + `jlpm run lint:check` exit 0 (same checks CI runs)
- Backend (pytest) and frontend (jest) test suites green

## Process

- Releases run through the `/release-jupyterlab-extension` command: journal → changelog → lint → `make publish` → commit + push → report
- `make publish` bumps the PATCH version, builds, publishes to npm and PyPI, and auto-commits the package metadata
- The release content commit follows conventional-commit format and describes the changes, not the version bump
- Versioning: PATCH auto-increments per publish; MINOR/MAJOR only on explicit decision

## Verification

- npm and PyPI are confirmed live (registry queries) before the release is reported
- CI (GitHub Actions: Build, Check Release) must be green on the release commit
- Cloudflare-affecting releases are verified live against the running tunnel (see `docs/acc-crit-cloudflare-integration.md` verification log)
