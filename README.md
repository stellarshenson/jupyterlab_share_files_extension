# jupyterlab_share_files_extension

[![GitHub Actions](https://github.com/stellarshenson/jupyterlab_share_files_extension/actions/workflows/build.yml/badge.svg)](https://github.com/stellarshenson/jupyterlab_share_files_extension/actions/workflows/build.yml)
[![npm version](https://img.shields.io/npm/v/jupyterlab_share_files_extension.svg)](https://www.npmjs.com/package/jupyterlab_share_files_extension)
[![PyPI version](https://img.shields.io/pypi/v/jupyterlab-share-files-extension.svg)](https://pypi.org/project/jupyterlab-share-files-extension/)
[![Total PyPI downloads](https://static.pepy.tech/badge/jupyterlab-share-files-extension)](https://pepy.tech/project/jupyterlab-share-files-extension)
[![JupyterLab 4](https://img.shields.io/badge/JupyterLab-4-orange.svg)](https://jupyterlab.readthedocs.io/en/stable/)
[![Brought To You By KOLOMOLO](https://img.shields.io/badge/Brought%20To%20You%20By-KOLOMOLO-00ffff?style=flat)](https://kolomolo.com)
[![Donate PayPal](https://img.shields.io/badge/Donate-PayPal-blue?style=flat)](https://www.paypal.com/donate/?hosted_button_id=B4KPBJDLLXTSA)

Peer-to-peer file sharing for JupyterLab. Create a **share** (file drop) or **request** (inbox) from a side panel, copy the link, paste it in chat - recipients open it in their own JupyterLab panel or any plain browser.

## Features

- **Shares** - read-only drops of files and folders; recipients download
- **Requests** - inboxes; recipients upload, organised per uploader
- **Connections** - paste someone's link to subscribe to their share or upload to their request
- **Drag-and-drop** from the file browser - drop zone (new share), share row (add files), request row (upload)
- **Browse inside a share** - double-click a folder to drill in; the `..` row goes back up
- **Open files directly** - double-click a file in the panel, JupyterLab opens it with the right viewer
- **Copy/paste** between the panel and the file browser
- **Right-click context menu** - in the file browser ("Share Files...") and on panel rows ("Copy to Current Folder", "Show in File Browser")
- **Hidden files visible by default** - dotfiles like `.env`, `.gitignore` are shareable; toggle in Settings
- **Standalone HTML page** - link works in any browser, no JupyterLab needed
- **QR code** in the share-link dialog for scanning from a phone
- **Live upload notifications** when someone uploads to your request
- **Self-connect guard** - pasting your own link shows a "you already own this" dialog
- **Symlink-friendly** - sharing `@shared/...` and similar works
- **Delete to trash** - panel deletes go to the OS trash by default (`c.ShareFilesConfig.use_trash`)
- **HTTPS-aware links** - share URLs follow the scheme the browser is on
- **Cloudflare tunnel sharing** - optional: links carry a public Cloudflare hostname, usable outside your network; a cloud icon in the panel header shows when active ([docs/cloudflare_setup.md](docs/cloudflare_setup.md))
- **Settings toggles** - shares, requests, hidden-file visibility, poll interval

## Requirements

- JupyterLab >= 4.0.0
- Python >= 3.9

## Install

Developers (project `Makefile`):

```bash
make install
```

End-users (PyPI):

```bash
pip install jupyterlab_share_files_extension
```

## Configuration

Optional, in `jupyter_server_config.py`:

```python
c.ShareFilesConfig.shares_dir = "uploads"        # default - relative to the notebook root
c.ShareFilesConfig.use_trash = True              # default: True
c.ShareFilesConfig.verify_peer_tls = True        # default: True
```

- **`shares_dir`** - where shares/requests/connections are stored; relative paths resolve against the notebook root (the server's `root_dir`), created on demand<br>
  Must resolve **inside** the notebook root - the extension refuses to start with a `StorageError` otherwise (shares outside the root are unreachable from the file browser and Contents API)
- **`use_trash`** - `False` deletes permanently instead of moving to the OS trash
- **`verify_peer_tls`** - set `False` when peers (e.g. a JupyterHub) use a self-signed certificate; server-side saves/uploads to such peers otherwise fail with a 502
- **`pollIntervalSeconds`** - panel refresh interval, in Settings Editor under **Share Files** (default 15, minimum 2); one tick refreshes all shares, requests and connections

## CLI

`jupyterlab_share_files` - the panel's operations as subcommands printing JSON. A thin client over the same authenticated HTTP API the panel uses, acting as one user via that user's Jupyter/JupyterHub token; scripts and AI agents drive the extension with it.

Environment variables:

- **`SHARE_FILES_BASE_URL`** - base URL of the Jupyter server running the extension<br>
  On JupyterHub this **must be the public user URL** (e.g. `https://hub.example.com/user/<name>/`) so generated links carry the public host; falls back to `JUPYTER_SERVER_URL` (internal on JupyterHub - links would not be shareable)
- **`SHARE_FILES_TOKEN`** - Jupyter or JupyterHub API token; falls back to `JUPYTERHUB_API_TOKEN` / `JUPYTER_TOKEN`
- **`SHARE_FILES_INSECURE`** - `1` skips TLS verification (self-signed hub certificates); off by default

```bash
jupyterlab_share_files list-items
jupyterlab_share_files create-share <name> [paths...]
jupyterlab_share_files create-request <name>
jupyterlab_share_files connect <link>
jupyterlab_share_files disconnect <key>
jupyterlab_share_files close-share <id>
jupyterlab_share_files close-request <id>
jupyterlab_share_files pick-up <key> [names...] [--target-dir DIR]
jupyterlab_share_files send-to-request <key> <paths...> [--uploader NAME]
jupyterlab_share_files list-request-uploads <id>
```

## Cloudflare tunnel sharing

The `cloudflare` subcommand exposes share/request links beyond the hub or local network through a Cloudflare tunnel. Full policy and configuration guide: [docs/cloudflare_setup.md](docs/cloudflare_setup.md); design notes: [docs/CLOUDFLARE_SHARING.md](docs/CLOUDFLARE_SHARING.md).

- **`--token <T> --account_id <A>`** - save credentials to `~/.config/jupyterlab-share-files/config.json` (chmod 600)
- **`--verify`** - report token validity (user-owned and account-owned `cfat_` tokens), bind capability (list tunnels), create capability (proven by creating and deleting a throwaway tunnel)
- **`--setup`** - provision end to end: create/reuse the `share-files` tunnel, route `--hostname` to the server, add a proxied CNAME, enforce HTTPS, save `public_base_url`
- **`--local-base-url <URL>`** - **mandatory with `--setup`**: the URL the `cloudflared` connector reaches the server at; explicit, never inferred; must be `https` (error with guidance otherwise; `localhost` is fine if served over https)
- **`--run`** - launch the `cloudflared` connector in the background after setup
- **`--reset`** - reset the saved token to none (clears account id, tunnel state, `public_base_url`); links revert to the local/hub address on the next request; Cloudflare-side resources untouched

How links change: the server reads `public_base_url` per request (no restart needed) and rewrites only the scheme+host of generated links - the path stays auto-detected from the server's own base URL. Without Cloudflare config, links keep the old behaviour (the host the browser is on).

What is exposed: only the extension's unauthenticated `/public/...` endpoints pass through the tunnel; the hub login, authenticated API and the rest of the private network answer 404 at the Cloudflare edge. Plain `http` to the share hostname is 301-redirected to `https`.

Token policies required: `Account → Cloudflare Tunnel → Edit` and zone-scoped `DNS → Edit` for the hostname's domain.

```bash
jupyterlab_share_files cloudflare --token <cloudflare-api-token> --account_id <account-id> --verify
jupyterlab_share_files cloudflare --setup --hostname share.example.com \
  --local-base-url "https://hub.example.com/user/<name>/" --run
jupyterlab_share_files cloudflare --reset
```

## Security

- The link is the credential (40 bits of entropy); no expiry, no PIN - share over trusted channels (Slack, email)
- HTTPS is inherited from your JupyterHub/Jupyter proxy
- With Cloudflare sharing active, links are reachable from the whole internet - HTTPS only, and only the `/public/...` capability endpoints; everything else stays unreachable

## Uninstall

```bash
pip uninstall jupyterlab_share_files_extension
```
