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

### Sharing files

- **Shares (file drops)** - "Here are my files, grab them via this link." Create a named, read-only snapshot of one or more files or folders. Anyone with the link can download
- **File browser context menu** - right-click any file or folder and pick "Share Files..." - the selected items become a new share, the link is auto-copied to clipboard, and a popup shows it for verification
- **Drag-and-drop to create** - drag from the JupyterLab file browser onto the panel's bottom drop-zone to start a new share
- **Extend a share by drag** - drop more files onto an existing share row to add them; folders are copied recursively, structure preserved
- **Remove items** - hover any file inside an expanded share and click the small [x] to remove just that item from the share
- **Delete share** - hover any share row to reveal a trash icon, or right-click for the context menu

### Requesting files

- **Requests (inboxes)** - "Send me files here." Create a named landing zone; anyone with the link can upload one or many files or folders
- **Per-uploader organisation** - uploads are placed under subfolders named after the uploader (or `anonymous` if they did not provide a name)
- **Live upload notifications** - JupyterLab pops a notification when someone uploads to your request, so you know without having to refresh
- **Delete request** - same hover trash icon as shares; deleting also removes all uploads

### Connecting to other people's links

- **Connect by paste** - paste someone else's share link into the panel's connect field to subscribe to it; the share's contents appear in the Connected section
- **Click-to-download** - click any file in a connected share to open the direct download URL in a new browser tab; folders download as ZIP
- **Drag-to-upload** - if the connection is a remote request, drag files from your file browser onto the request row to upload them to the other peer
- **Self-connect refused** - the panel and backend both refuse links pointing back to your own server, so you never get a circular connection
- **Disconnect** - drop a connection from the panel without affecting the remote side

### Working with files in the panel

- **Copy to Current Folder** - right-click any file or folder shown in the panel (My Shares, My Requests uploads) and pick "Copy to Current Folder" - it gets copied via JupyterLab's Contents API into whatever directory the file browser is currently showing. Toast confirms the destination
- **Show in File Browser** - right-click and pick "Show in File Browser" to switch focus to the file browser tab, navigate to the entry's parent directory, and select it - useful when you want to inspect or move the file with standard JupyterLab tools
- **Workspace-aware** - both actions work for any entry inside the workspace; if you have configured `shares_dir` to point outside the workspace, the right-click menu just does not appear for those entries (graceful fallback)

### Links and recipients

- **Standalone HTML page** - every share or request link opens a self-contained page in any browser (no login, no JS framework, no extension required on the recipient side)
- **Copy-link popup** - hover any share or request to reveal a copy-link icon; clicking flashes the icon and opens a popup with the selectable, copyable URL
- **8-character base32 token** - the share/request ID; the link itself is the credential (no extra password by default)

### Storage and configuration

- **Local filesystem layout** - files live under `<server_root_dir>/uploads/` by default in a human-readable `<slug>-<id>` folder structure, so you can find a share visually in the file browser
- **Configurable storage path** - admins can set `c.ShareFilesConfig.shares_dir` in `jupyter_server_config.py` to redirect storage anywhere on disk
- **Symlink-friendly** - sharing files from symlinked locations like `@shared/...` works transparently; the share contains a real copy of whatever the symlink points to
- **Filesystem is the source of truth** - if you delete a share folder directly from the file browser, the panel picks it up on the next poll and cleans the entry

### UI

- **Side panel** - dedicated right-rail panel with three foldable sections: My Shares, My Requests, Connected
- **Auto-refresh** - polls every 15 seconds; refresh icon in the header spins while polling and can be clicked for an immediate refresh
- **Drag-target highlight** - dropping files onto a valid target (share row, request row, drop-zone) shows a clear blue outline before commit
- **Toggleable features** - turn file sharing or file requests off individually under **Settings → Settings Editor → Share Files**; both default to on
- **Theme-aware UI** - panel inherits JupyterLab's font family, font sizes, theme colours and layout dimensions; mirrors the file browser visual conventions so it reads as a native sidebar

### Security

- **Link is the credential** - 40 bits of token entropy (8-char base32) protects against guessing; share links carry no other auth
- **Inherits HTTPS** - if your JupyterHub or Jupyter server is HTTPS-terminated (e.g. behind Traefik, nginx, or the hub's built-in proxy), links are encrypted end-to-end
- **No accidental cross-user access** - the public endpoints only expose data from the share/request directories; the rest of your workspace is unreachable through the link
- **Path traversal blocked** - file and folder names from incoming uploads are sanitised; `..` components are stripped, absolute paths are rejected
- **No automatic expiry** - links live until you delete the share; suitable for trusted-channel sharing (Slack, email, etc.)

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
