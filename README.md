# jupyterlab_share_files_extension

[![GitHub Actions](https://github.com/stellarshenson/jupyterlab_share_files_extension/actions/workflows/build.yml/badge.svg)](https://github.com/stellarshenson/jupyterlab_share_files_extension/actions/workflows/build.yml)
[![npm version](https://img.shields.io/npm/v/jupyterlab_share_files_extension.svg)](https://www.npmjs.com/package/jupyterlab_share_files_extension)
[![PyPI version](https://img.shields.io/pypi/v/jupyterlab-share-files-extension.svg)](https://pypi.org/project/jupyterlab-share-files-extension/)
[![Total PyPI downloads](https://static.pepy.tech/badge/jupyterlab-share-files-extension)](https://pepy.tech/project/jupyterlab-share-files-extension)
[![JupyterLab 4](https://img.shields.io/badge/JupyterLab-4-orange.svg)](https://jupyterlab.readthedocs.io/en/stable/)
[![Brought To You By KOLOMOLO](https://img.shields.io/badge/Brought%20To%20You%20By-KOLOMOLO-00ffff?style=flat)](https://kolomolo.com)
[![Donate PayPal](https://img.shields.io/badge/Donate-PayPal-blue?style=flat)](https://www.paypal.com/donate/?hosted_button_id=B4KPBJDLLXTSA)

If you live in JupyterLab and have ever had to "just send over that dataset" or "could you please upload your CSV somewhere I can grab it" - you know the routine. Email it (too big). Drop it in OneDrive (sync it first, share it, accept the corporate Terms of the Universe). Spin up an S3 bucket (and a meeting). By the time anyone has the file, you've forgotten what you needed it for.

This extension does not try to replace any of that. OneDrive, Dropbox, S3, that one Slack channel called `#files-final-FINAL` - they remain undefeated for serious file logistics. This is the small, embarrassingly specific tool for the other 90% of the time, when two people on JupyterLab just need to pass a folder back and forth and would prefer not to involve an enterprise.

Create a **share** (file drop) or **request** (inbox) from a side panel, copy the link, paste it in chat. Recipients open it in their JupyterLab panel or any plain browser. That is the whole pitch.

## Features

- **Shares** - read-only drops of files and folders; recipients download
- **Requests** - inboxes; recipients upload, organised per uploader
- **Connections** - paste someone's link to subscribe to their share or upload to their request
- **Drag-and-drop** from the file browser - drop zone (new share), share row (add files), request row (upload)
- **Browse inside a share** - double-click a folder to drill in; the `..` row takes you back up
- **Open files directly** - double-click a file in the panel and JupyterLab opens it with the right viewer
- **Right-click context menu** - in the file browser ("Share Files..."), and on panel rows ("Copy to Current Folder", "Show in File Browser")
- **Hidden files visible by default** - dotfiles like `.env`, `.gitignore`, `.ssh/config` are easy to share; toggle in Settings if you want them hidden
- **Standalone HTML page** - link works in any browser, no JupyterLab needed
- **Live upload notifications** when someone uploads to your request
- **Self-connect guard** - pasting your own link shows a clear "you already own this" dialog instead of a silent toast
- **Symlink-friendly** - sharing `@shared/...` and similar works
- **Delete to trash** - panel deletes move files to the OS trash by default (toggle with `c.ShareFilesConfig.use_trash`)
- **HTTPS-aware links** - share URLs follow the scheme the browser is on (HTTPS behind a proxy, HTTP for direct peer-to-peer)
- **Cloudflare tunnel sharing** - optional: links carry a public Cloudflare hostname so recipients outside your network can use them; a cloud icon in the panel header shows when it is active (see [docs/cloudflare_setup.md](docs/cloudflare_setup.md))
- **Settings toggles** - turn shares, requests, or hidden-file visibility on/off independently

## Requirements

- JupyterLab >= 4.0.0
- Python >= 3.9

## Install

Developers install via the project `Makefile`:

```bash
make install
```

End-users install the published package from PyPI:

```bash
pip install jupyterlab_share_files_extension
```

## Configuration

Optional, set in `jupyter_server_config.py`:

```python
c.ShareFilesConfig.shares_dir = "uploads"        # default - relative to the notebook root
c.ShareFilesConfig.shares_dir = "data/uploads"   # any path inside the notebook root
c.ShareFilesConfig.use_trash = True              # default: True
c.ShareFilesConfig.verify_peer_tls = True        # default: True
```

Set `verify_peer_tls = False` when peers (for example a JupyterHub) use a self-signed certificate. Saving from a connected share and uploading to a connected request are server-side fetches to the peer; with verification on and a self-signed peer they fail and the panel reports a 502. With it off, those fetches succeed.

A relative `shares_dir` is resolved against the **notebook root** (the same folder the file browser starts in - the Jupyter server's `root_dir`). The directory is created on demand and does not have to exist beforehand.

`shares_dir` must resolve to a location **inside the notebook root**. The extension refuses to start with a `StorageError` if it does not, because shares outside the root are unreachable via JupyterLab's file browser and Contents API - the panel's drag-out, "Copy to Current Folder", and "Show in File Browser" actions would all break silently.

The panel's refresh interval is set in JupyterLab's Settings Editor under **Share Files** (`pollIntervalSeconds`, default 15, minimum 2). One tick refreshes all of your shares and requests and all connections.

## CLI

The package ships a command-line tool, `jupyterlab_share_files`, that operates the extension - create shares and requests, connect to peers, pick up and send files, and close them. It is a thin client over the same authenticated HTTP API the panel uses, acting as a single user via that user's Jupyter/JupyterHub token, so an AI agent can drive the extension by running it. Each subcommand prints JSON.

It is configured with environment variables:

- `SHARE_FILES_BASE_URL` - the base URL of your Jupyter server running the extension. On JupyterHub this **must be the public user URL** (e.g. `https://hub.example.com/user/<name>/`) so that generated share links carry the public host and `/user/<name>/` prefix. Falls back to `JUPYTER_SERVER_URL` (which on JupyterHub is the internal address - links would then be internal and not shareable).
- `SHARE_FILES_TOKEN` - a Jupyter or JupyterHub API token. Falls back to `JUPYTERHUB_API_TOKEN` / `JUPYTER_TOKEN`.
- `SHARE_FILES_INSECURE` - set to `1` to skip TLS certificate verification (for hubs behind a self-signed certificate). Off by default.

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

The `cloudflare` subcommand exposes share links beyond the hub via a [Cloudflare tunnel](docs/CLOUDFLARE_SHARING.md): `--token` and `--account_id` save the Cloudflare API token and account id to `~/.config/jupyterlab-share-files/config.json` (chmod 600), and `--verify` reports what the token can do with tunnels - whether it is valid (user-owned and account-owned `cfat_` tokens both supported), can bind to (list) existing tunnels, and can set up (create) a new one, the latter proven by creating a throwaway tunnel and deleting it immediately.

```bash
jupyterlab_share_files cloudflare --token <cloudflare-api-token> --account_id <account-id> --verify
```

`--setup` provisions the tunnel end to end: it creates (or reuses) a tunnel named `share-files`, routes `--hostname` to the server address given by the **mandatory** `--local-base-url` (the URL the `cloudflared` connector reaches the server at - given explicitly, never inferred; it must be `https`, otherwise the command errors with guidance, and `localhost` is fine as long as it is https), adds a proxied CNAME in the hostname's zone, switches on the zone's **Always Use HTTPS** (plain `http` to the share hostname is 301-redirected at the Cloudflare edge), and saves `public_base_url` so generated share/request links carry the Cloudflare host - the path part stays auto-detected from the server's own base URL. Only the extension's unauthenticated `/public/...` endpoints are exposed through the tunnel; everything else (hub login, authenticated API, the rest of the private network) answers 404 at the Cloudflare edge. `--run` additionally launches the `cloudflared` connector in the background. `--reset` resets the saved token to none (clearing account id, tunnel state and `public_base_url`); links then revert to the local/hub address on the next request. Cloudflare-side resources are not touched by `--reset`. The token needs `Account → Cloudflare Tunnel → Edit` and zone-scoped `DNS → Edit` for the hostname's domain - see [docs/cloudflare_setup.md](docs/cloudflare_setup.md) for the full policy and configuration guide.

```bash
jupyterlab_share_files cloudflare --setup --hostname share.example.com \
  --local-base-url "https://hub.example.com/user/<name>/" --run
jupyterlab_share_files cloudflare --reset
```

## Security

The link is the credential (40 bits of entropy). HTTPS is inherited from your JupyterHub/Jupyter proxy. Suitable for trusted-channel sharing (Slack, email). No expiry, no PIN.

With Cloudflare sharing enabled, links are reachable from the whole internet - only over HTTPS (plain `http` is redirected at the Cloudflare edge), and only the unauthenticated `/public/...` capability endpoints pass through the tunnel; the hub login, authenticated API, and the rest of your private network stay unreachable.

## Uninstall

```bash
pip uninstall jupyterlab_share_files_extension
```
