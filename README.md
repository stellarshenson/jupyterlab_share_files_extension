# jupyterlab_share_files_extension

[![GitHub Actions](https://github.com/stellarshenson/jupyterlab_share_files_extension/actions/workflows/build.yml/badge.svg)](https://github.com/stellarshenson/jupyterlab_share_files_extension/actions/workflows/build.yml)
[![npm version](https://img.shields.io/npm/v/jupyterlab_share_files_extension.svg)](https://www.npmjs.com/package/jupyterlab_share_files_extension)
[![PyPI version](https://img.shields.io/pypi/v/jupyterlab-share-files-extension.svg)](https://pypi.org/project/jupyterlab-share-files-extension/)
[![Total PyPI downloads](https://static.pepy.tech/badge/jupyterlab-share-files-extension)](https://pepy.tech/project/jupyterlab-share-files-extension)
[![JupyterLab 4](https://img.shields.io/badge/JupyterLab-4-orange.svg)](https://jupyterlab.readthedocs.io/en/stable/)
[![Brought To You By KOLOMOLO](https://img.shields.io/badge/Brought%20To%20You%20By-KOLOMOLO-00ffff?style=flat)](https://kolomolo.com)
[![Donate PayPal](https://img.shields.io/badge/Donate-PayPal-blue?style=flat)](https://www.paypal.com/donate/?hosted_button_id=B4KPBJDLLXTSA)

> [!TIP]
> This extension is part of the [stellars_jupyterlab_extensions](https://github.com/stellarshenson/stellars_jupyterlab_extensions) metapackage. Install all Stellars extensions at once: `pip install stellars_jupyterlab_extensions`

Peer-to-peer file sharing for JupyterLab. Create a named **share** (a read-only drop) or a **request** (an inbox), copy the link, paste it in chat. The recipient opens the link either in their own JupyterLab side panel or in any plain browser - no account, no extension required on their side.

Think AirDrop, except the link is the discovery mechanism and the JupyterLab server is the peer.

## Features

- **Side panel** - dedicated right-rail panel with three foldable sections (My Shares, My Requests, Connected) and a refresh button that spins while polling
- **Shares (file drops)** - "Here are my files, grab them via this link." Drag files or folders from the file browser into the panel to create or extend a share. Recipients download via the link
- **Requests (inboxes)** - "Send me files here." Anyone with the link can upload files or folders, organized per uploader in your local storage
- **Connections** - paste someone else's link into your panel to subscribe to their share (browse and click to download) or their request (drag your local files to upload)
- **Drag-and-drop** - drag from the file browser onto the bottom drop-zone (new share), an existing share row (add files), or a connected request (upload)
- **Per-row inline actions** - hover over any share or request to reveal a copy-link icon and a delete icon. The copy-link icon flashes green on click and opens a popup with the selectable link
- **Folder support** - shares and requests work for both individual files and entire folders. Directory structure is preserved
- **Standalone web page** - every link opens a self-contained HTML page for non-JupyterLab users (download buttons for shares, drag-drop upload zone for requests). No login, no JS framework, no special browser needed
- **Symlink-friendly** - sharing files from symlinked locations like `@shared/...` works transparently
- **Live upload notifications** - when someone uploads to your request, a JupyterLab notification pops up
- **Toggleable features** - turn off sharing or requests individually in JupyterLab Settings (both on by default)
- **Theme-aware UI** - panel inherits JupyterLab's font, font-size, theme variables and colour scheme; designed to look at home next to the file browser

## How it works

Files are stored under `<server_root_dir>/uploads/` by default:

```
<workspace>/uploads/
  shares/
    <slug>-<id>/
      manifest.json
      data/                # copies of shared files
        ...
  requests/
    <slug>-<id>/
      manifest.json
      uploads/
        <uploader>/        # subfolder per uploader
          ...
  connections.json         # links you have connected to
```

Each share and request is identified by an 8-character base32 token. The folder name is `<slug>-<id>` so you can find a share visually in the file browser while routes still resolve by ID. The token is the secret - anyone with the link gets access.

Links are served unauthenticated from your own Jupyter server (HTTPS if your hub uses HTTPS), so:

- Your server must be running for the link to work (same as AirDrop needing your phone on)
- Anyone with the link can access - no password, no expiry by default
- Suitable for closed JupyterHub teams where the link travels via trusted channels (Slack, email, etc.)

## Settings

Open **Settings → Settings Editor → Share Files** to toggle:

- **Enable file sharing** (default on) - hides My Shares section, the Share Files context menu, and the New Share command
- **Enable file requests** (default on) - hides My Requests section and the New Request command

Toggles apply live. Connections remain available even with both features off, so you can still consume other people's shares and uploads.

## Installation

Requires JupyterLab 4.0.0 or higher.

```bash
pip install jupyterlab_share_files_extension
```

## Configuration

By default, shares and requests live in `<server_root_dir>/uploads/`. To change the location, add to your `jupyter_server_config.py`:

```python
c.ShareFilesConfig.shares_dir = "/path/to/your/storage"
```

Relative paths resolve against the server root. The directory is created on demand - no setup step required.

## Uninstall

```bash
pip uninstall jupyterlab_share_files_extension
```
