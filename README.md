# jupyterlab_share_files_extension

[![GitHub Actions](https://github.com/stellarshenson/jupyterlab_share_files_extension/actions/workflows/build.yml/badge.svg)](https://github.com/stellarshenson/jupyterlab_share_files_extension/actions/workflows/build.yml)
[![npm version](https://img.shields.io/npm/v/jupyterlab_share_files_extension.svg)](https://www.npmjs.com/package/jupyterlab_share_files_extension)
[![PyPI version](https://img.shields.io/pypi/v/jupyterlab-share-files-extension.svg)](https://pypi.org/project/jupyterlab-share-files-extension/)
[![Total PyPI downloads](https://static.pepy.tech/badge/jupyterlab-share-files-extension)](https://pepy.tech/project/jupyterlab-share-files-extension)
[![JupyterLab 4](https://img.shields.io/badge/JupyterLab-4-orange.svg)](https://jupyterlab.readthedocs.io/en/stable/)
[![Brought To You By KOLOMOLO](https://img.shields.io/badge/Brought%20To%20You%20By-KOLOMOLO-00ffff?style=flat)](https://kolomolo.com)
[![Donate PayPal](https://img.shields.io/badge/Donate-PayPal-blue?style=flat)](https://www.paypal.com/donate/?hosted_button_id=B4KPBJDLLXTSA)

Peer-to-peer file sharing for JupyterLab. Create a **share** (file drop) or **request** (inbox) from a side panel, copy the link - recipients open it in their own JupyterLab panel or any plain browser.

## Screenshots

The Share Files panel and the create-share dialog with optional password:

| Side panel                                            | New share with password                                         |
| ----------------------------------------------------- | --------------------------------------------------------------- |
| ![Share Files panel](.resources/jupyterlab-panel.png) | ![Create new share dialog](.resources/jupyterlab-new-share.png) |

The standalone page recipients see in any browser - download view, upload view (dark theme), password gate. System theme by default, Light / Dark / Auto switch:

![Share page](.resources/page-share-light.png)

![Request page, dark theme](.resources/page-request-dark.png)

![Password gate](.resources/page-password-gate.png)

## Features

- **Shares** - read-only drops of files and folders; recipients download
- **Requests** - inboxes; recipients upload, organised per uploader
- **Per-uploader identity** - request uploaders get a server-issued short hash in a browser cookie; the page shows only their own uploads with add/remove control; the owner panel shows `name (hash)` so several "anonymous" uploaders stay distinct
- **Connections** - paste someone's link to subscribe to their share or upload to their request; a connected share saves an entry, or the whole share unpacked or as a zip, into the current folder. A link whose host presents a certificate the system does not trust, a self-signed one among them, opens a dialog with the host, the reason and the SHA-256 fingerprint; Trust keeps that one certificate with the connection until you remove it; a different certificate later marks the row `untrusted`, and Connect Again in its menu asks about the new one. A certificate that could not be used even once trusted, an expired one, is refused at connect instead of offered
- **Drag-and-drop** from the file browser - drop zone (new share), share row (add files or folders), request row (upload)
- **Browse inside a share** - double-click a folder to drill in; the `..` row goes back up
- **Open files directly** - double-click a file in the panel, JupyterLab opens it with the right viewer
- **Hover for details** - hovering a file or folder row shows a tooltip with its full name, path, size and modified date
- **Copy/paste** between the panel and the file browser
- **Right-click context menu** - file browser ("Share Files...") and panel rows ("Download to Current Folder" on a file, on a whole share or request "Download to Current Folder" and "Save as Zip", "Show in File Browser", and on a share "Rename Share...", which keeps its id and link - standalone only, the hub offers no rename yet), each entry with an icon; Tab reaches a share, request or connection row, its Expand/Collapse button and a connected peer's entries, Enter on an entry opens it (a folder drills in, the `..` row goes up), and Shift+F10 or the ContextMenu key opens its menu
- **Optional password protection** - set at creation or later (right-click → Set Password); recipients unlock before any access; one-click xkcdpass passphrase generation; link dialog shows the password with a copy button; attempts rate limited server-side
- **Hidden files visible by default** - dotfiles like `.env`, `.gitignore` are shareable; toggle in Settings
- **Standalone HTML page** - link works in any browser, no JupyterLab needed; Light / Dark / Auto theme
- **QR code** in the share-link dialog for scanning from a phone (right-click copies the image), plus a copy icon embedded in the link field to grab the link again on demand
- **Live upload notifications** when someone uploads to your request
- **Large files stay off the heap** - every transfer is written or read as it moves: a share is served chunk by chunk and a folder's zip is built into the response file by file; an upload from the recipient page streams to a spool file on disk (raw body, name in an `X-Filename` header - the same shape the galaxahub fileshare service takes) with no size limit but the disk's free space; an upload from the panel to a connected request streams from the workspace file; a download from a connected share is relayed to the browser while it is still arriving, and the browser saves it straight to disk
- **Self-connect guard** - pasting your own link shows a "you already own this" dialog
- **Symlink-friendly** - sharing `@shared/...` and similar works in standalone mode; in hub mode see "What the hub cannot read" below
- **Save a whole record** - a share or request row saves itself into the file browser's current folder, as the files under a folder named after the record or as one zip of that name. A second save takes the next free name, and a save that fails part way removes what it wrote
- **Delete to trash** - panel deletes go to the OS trash by default (`c.ShareFilesConfig.use_trash`)
- **HTTPS-aware links** - share URLs follow the scheme the browser is on
- **Cloudflare tunnel sharing** - optional public links beyond your network; cloud icon in the panel header shows state, toggles public/private, opens setup when unconfigured ([docs/cloudflare_setup.md](docs/cloudflare_setup.md))
- **Hub mode** - on a lab spawned by [galaxahub](https://github.com/stellarshenson/galaxahub) the panel works through the hub's fileshare API, a connection through its record's link, and the lab mounts no unauthenticated route; the hub stages and serves the files ([docs/design-hub-public-zone.md](docs/design-hub-public-zone.md))
- **Settings toggles** - shares, requests, hidden-file visibility, poll interval, download limit

![Sharing flow](.resources/sharing-flow.svg)

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
c.ShareFilesConfig.excluded_names = [".ipynb_checkpoints", "__pycache__"]  # replaces the default catalogue
c.ShareFilesConfig.verify_peer_tls = True        # default: True
c.ShareFilesConfig.password_max_attempts_per_minute = 30   # default: 30
c.ShareFilesConfig.password_attempt_cooldown_seconds = 1   # default: 1
```

- **`shares_dir`** - storage for shares/requests/connections; relative paths resolve against the notebook root; created only when you first create a share or request or connect to a link, so the folder never appears in a workspace that has not shared anything; must resolve **inside** the notebook root; otherwise the server extension logs an error and registers no routes - JupyterLab still starts, and every panel call answers 404
- **`use_trash`** - `False` deletes permanently instead of moving to the OS trash
- **`excluded_names`** - names a share never copies, matched with `fnmatch` against one path component. The dropped item's own name is matched in both modes; names inside a folder are matched at every depth only where the lab copies the folder itself, which is a standalone lab. On a hub the patterns are matched against the dropped item's own name only, and the plain names (no pattern, the first 64) are sent to the hub, which leaves them out at every depth. The default catalogue holds `.ipynb_checkpoints`, `__pycache__`, `__MACOSX`, `.DS_Store`, `._*`, `.Trash`, `.Trash-*`, `.Trashes`, `.trashed-*` and `$RECYCLE.BIN`. Setting the option replaces the whole catalogue; `[]` copies everything. A drop where every item is excluded is refused with a sentence naming them
- **`verify_peer_tls`** - on by default: a peer's certificate the system does not trust, a self-signed one among them, is asked about at connect (see Connections); `False` skips the check, and the question, for every peer
- **`password_max_attempts_per_minute`** / **`password_attempt_cooldown_seconds`** - per-resource rate limiting of password attempts (`limits` library); generous defaults (30/minute, 1s); lower the cap or raise the cooldown to harden
- **`pollIntervalSeconds`** - panel refresh interval, Settings Editor → Share Files (default 15, minimum 2)
- **`tunnelAutostart`** - Settings Editor → Share Files (default off); bring the Cloudflare tunnel up at server startup - off, the server starts with private links and the cloud icon switches the tunnel on demand
- **`peerDownloadMaxGb`** - Settings Editor → Share Files (1, 2, 5, 10, 20, 50 or 100 GB; default 10); the largest download from a connected share - a save into the workspace, unpacked, or one item handed to the browser; the server refuses a larger one with a 502 naming the limit and leaves nothing behind; the download lands on the workspace disk as it arrives (a spool file under the store's `tmp` folder), never in the server's memory, and may take 300 s for every GB of the limit; an item handed to the browser is relayed as it arrives and saved by the browser itself, so no memory bounds that path; the CLI's `pick-up` sends no limit and gets the default

## CLI

`jupyterlab_share_files` - the panel's operations as subcommands; a thin client over the same authenticated HTTP API, for scripts and AI agents. Human-readable output by default, `--json` for machine-readable.

- **`SHARE_FILES_BASE_URL`** - base URL of the Jupyter server; on JupyterHub this **must be the public user URL** (e.g. `https://hub.example.com/user/<name>/`) so links carry the public host; falls back to `JUPYTER_SERVER_URL`
- **`SHARE_FILES_TOKEN`** - Jupyter/JupyterHub API token; falls back to `JUPYTERHUB_API_TOKEN` / `JUPYTER_TOKEN`
- **`SHARE_FILES_INSECURE`** - `1` skips TLS verification (self-signed certificates); off by default

```bash
jupyterlab_share_files list-items
jupyterlab_share_files create-share <name> [paths...] [--password PW | --generate-password]
jupyterlab_share_files create-request <name> [--password PW | --generate-password]
jupyterlab_share_files add-files <share-id> <paths...>
jupyterlab_share_files remove-files <share-id> <names...>
jupyterlab_share_files remove-upload <request-id> <uploader-hash> <name>
jupyterlab_share_files set-password <share|request> <id> [PW] [--generate] [--clear]
jupyterlab_share_files generate-password
jupyterlab_share_files connect <link> [--trust-certificate]
jupyterlab_share_files disconnect <key>
jupyterlab_share_files close-share <id>
jupyterlab_share_files close-request <id>
jupyterlab_share_files pick-up <key> [names...] [--target-dir DIR]
jupyterlab_share_files send-to-request <key> <paths...> [--uploader NAME]
jupyterlab_share_files list-request-uploads <id>
jupyterlab_share_files install-claude-skill
```

If a link's host uses a self-signed certificate, `connect` stops and shows the certificate's fingerprint. If you know the host and the certificate, run `connect <link> --trust-certificate`: it connects, prints the host and the certificate's fingerprint, and the connection keeps that certificate until you remove it, as Trust does in the panel.

`install-claude-skill` installs the bundled Claude skill (a usage guide for this CLI) into `~/.claude/skills/jupyterlab_share_files/`, asking for confirmation before writing.

## Cloudflare tunnel sharing

The `cloudflare` command exposes share/request links beyond the hub or local network through a Cloudflare tunnel. Chosen for security: outbound-only connector (no inbound port), HTTPS enforced at the edge, and path-restricted ingress - only the extension's `/public/...` endpoints are routable; everything else answers 404 at the edge. Full guide: [docs/cloudflare_setup.md](docs/cloudflare_setup.md).

- **`setup --token <T> --account-id <A> --hostname <H> --private-base-url <URL>`** - save credentials (chmod-600 config) and provision end to end: create/reuse the tunnel (deterministic name `share-files-<sluggified private base URL>`), route the hostname, add a proxied CNAME, enforce HTTPS, save `public_base_url`, start the connector; `--private-base-url` is required and must be `https`
- **`validate`** - verify every component of the saved config: config completeness, URL sanity, token validity, tunnel existence/status/name on Cloudflare, proxied CNAME, ingress rule, `cloudflared` binary on PATH, daemon/toggle state
- **`info`** - current configuration; tokens masked to last 4 characters, `tunnel_active`, `daemon_running`, Cloudflare-side `tunnel_status`
- **`start`** / **`stop`** - switch between public links (daemon running) and private links; credentials, tunnel and DNS kept; effective on the next request, no restart
- **`reset`** - clear the saved token and derived state; links revert to the local/hub address; Cloudflare-side resources untouched
- **Connector supervision** - the extension keeps `cloudflared tunnel run` alive, retrying up to `c.ShareFilesConfig.cloudflared_retries` times (default 3); autostart is a user setting (default off)
- **Cloud icon** - panel header, always visible, and the only Cloudflare control (no row menu carries one): the filled cloud in the colour of the other header icons = tunnel on (public links), dashed in the same colour = off/unconfigured (private links), the filled cloud in the accent blue, breathing down to nothing and back = switching on or off, for at least one whole breath of 2.4 s however fast the switch lands; nothing is drawn around the icon in any state; click, Enter or Space toggles, or opens the setup popup when unconfigured; a switch on whose connector does not come up is refused with `cloudflared did not start - see <connector log>` and links stay private
- **Reachability check** - when the tunnel is active, the link dialog probes the public link server-side (`api/link-check`; a frontend fetch would be blocked by CORS) and shows reachable/not reachable; configured-but-off shows "Cloudflare sharing is not running" instead
- **Link rewrite** - the server reads `public_base_url` and the toggle per request and rewrites only scheme+host; the path stays auto-detected; without config, links keep the browser's host
- **Token policies required** - `Account → Cloudflare Tunnel → Edit` plus zone-scoped `DNS → Edit` for the hostname's domain

```bash
jupyterlab_share_files cloudflare setup --token <api-token> --account-id <account-id> \
  --hostname share.example.com --private-base-url "https://hub.example.com/user/<name>/"
jupyterlab_share_files cloudflare validate
jupyterlab_share_files cloudflare info
jupyterlab_share_files cloudflare start
jupyterlab_share_files cloudflare stop
jupyterlab_share_files cloudflare reset
```

## Hub mode

A lab spawned by galaxahub carries `SHARE_FILES_PUBLIC_ZONE=hub`, the path of the hub's fileshare API in `SHARE_FILES_HUB_API` and its own `JUPYTERHUB_API_TOKEN`. The extension then registers only its authenticated `api/*` routes - no `public/*`, no `static/*` - and every panel action on the user's own records is a call to the hub. Design: [docs/design-hub-public-zone.md](docs/design-hub-public-zone.md).

- **The hub holds the bytes** - a share names workspace paths; the hub copies the bytes with its own transfer job and the row shows `staging` until the copy lands, or `refused` with the hub's reason. New Share creates an empty share, and files dropped on a share row are added to it, and files copied or cut in the file browser are pasted onto a share row from its menu (a cut is added as a copy): the row shows a spinner and the word `staging` or `adding` while the hub copies, wears an accent layer across itself as wide as the fraction copied, says when the hub refused the add and why, and counts the entries the hub left out
- **Editing a share** - a file or folder row inside a share is removed (the row button, `Delete`, or the row menu; it asks once), renamed in place (`F2` or the row menu; Enter or leaving the field commits, Escape abandons) and moved by dragging it onto a folder row of the same share. In the rename field `folder/name` moves the entry into that folder and `/name` moves it to the top of the share. An added item lands at the top of the share under its own name, and a name the share already holds is refused by name; to replace a file, remove it and add it again. The recipient's link stays the same across every edit
- **What the hub leaves out** - inside a copied folder the hub skips a link whose target is inside the workspace, and any path segment that starts with `.` or `~`, and counts them; the lab also sends the plain names of its excluded-names catalogue (`__pycache__`, `.ipynb_checkpoints` and the like - patterns are applied to the chosen items only), and the hub leaves those out at every depth too
- **What the hub cannot read** - the hub reads the workspace through its own mount of the volume, so a link whose target lies outside the workspace is one it cannot follow: the lab refuses a chosen path that leaves the workspace, by name, before the hub is asked. A folder that itself stays inside but holds such a link is refused by the hub, whole, and the share row reads refused
- **Saving a record out** - a share row asks the hub to write the whole record into the file browser's current folder, through the hub's own `shares/<id>/fetch`; the bytes never pass through the lab. The hub packs no archive, so for Save as Zip the lab packs what the hub wrote and removes the unpacked copy; the hub carries no route for a request's uploads as a set, so the lab fetches them one at a time into a folder it makes first
- **Recipients reach the hub** - the link is the hub's page; the lab answers 404 on its recipient paths
- **Uploads are fetched in** - each upload under a request offers "Download to Current Folder" in its menu; the hub copies it into a fresh folder under the file browser's current directory. The hub writes on its own mount of the volume, so a current directory reached through a link out of the workspace is refused by name, as a chosen path is
- **Connections** - another user's share or request connects by the whole link its owner handed out, on the hub's public address or its Cloudflare hostname; an id alone connects nothing. The hub answers 404 for a record the caller does not own, so the lab reads the record through that link, as a recipient's browser does, and sends it no hub token. A protected record asks for its password once; the lab keeps the password in its 0600 config and the unlock cookie in memory only. Your own record is refused by name. A connected share's files and folders save into the current folder, one at a time or the whole share unpacked or as a zip, and the lab downloads the bytes within the peer download limit. A connected request takes files and folders dropped or pasted onto its row: the lab sends each file on its own, as the request page does, so a folder's files arrive without the folder, and the row wears the progress layer at the fraction sent, then says how many landed or why the hub refused one. The hub signals no change to another user's record, so the panel reads connected rows again on its poll interval. If the owner sets or changes the password later, the row says so, and Connect Again in its menu asks for the new one. A drop or paste onto a row that says closed, password changed or untrusted sends nothing and says why, and disconnecting during an upload sends no further file. A file larger than the request's limit is named with the limit, and nothing is sent; an upload into a request its owner closes meanwhile says it ended, and one into a request that does not answer for now says it has started. A hub whose public address has a self-signed certificate is asked about at connect, as any connection is
- **Cloud icon** - Cloudflare sharing is one switch over every record, not a flag each record carries: the header icon is the only control the panel offers, it flips them all, and it sets the default for the next one. While it reads on, listing the shares brings onto the tunnel any record the hub still holds off, so the link the panel shows is the one the switch promises; while it reads off, no record is touched. On, the link carries the hub's Cloudflare address; off, the hub's own address, reachable on the hub's network only. No tunnel runs on the lab, and the hub runs its tunnel only while some record is switched on. The header icon alone shows the state - rows carry no cloud mark
- **The hub owns the state** - the icon reads the hub's own `tunnel_available` and `tunnel_ready` together with the lab's stored default: the grey filled cloud when the default is on and the hub's tunnel is serving, the breathing blue filled cloud when the default is on and the hub is bringing its tunnel up for a record that asked for one, the grey dashed silhouette when the default is off or when it is on and no share or request asks for a tunnel yet (the tooltip tells these two apart), and no icon at all while the hub does not answer or where the group's policy carries no tunnel. The hub rings the lab's change stream when its tunnel comes up or drops, so the icon moves between on and pending with no click
- **Link check** - for a link on the Cloudflare hostname the link dialog asks the lab server to open it and shows what answered; a link on the hub's own address is not checked and the dialog says it works on the hub's network only, or, for a record switched on while the hub's tunnel is not yet serving, that it moves to the Cloudflare hostname once the hub confirms. The answer is reachable on HTTP 200, otherwise the HTTP status, `no answer within 10 s`, `connection refused`, `no address for the host name`, `a TLS error`, `a closed connection` or `a network error`, and the address opened only when it differs from the link shown; the hub's cached serving verdict plays no part
- **Live panel** - the panel holds one change stream to the lab, which holds one to the hub: a new upload, a share turning ready or refused, a close or an expiry shows without polling
- **Grants** - what the panel may create comes from the hub; a refused kind stays in the menu and answers with the hub's reason in words when you pick it; a group that requires a password gets a create dialog with the password field required and pre-filled
- **Not available** - removing a single upload, downloading a connected entry to the browser, per-user Cloudflare setup
- **Fail closed** - a lab in hub mode with an incomplete contract reports the hub unavailable; it never falls back to serving recipients itself

## Security

- **The link is the credential** - 40 bits of entropy, no expiry; share over trusted channels
- **Optional password** as a second factor - unlock token bound to the password, so changing it instantly locks out everyone holding the old one
- **Brute-force protection** - per-resource rate limiting (`limits` library, in-memory): per-minute cap plus mandatory cooldown, both tunable (defaults 30/minute, 1s)
- **HTTPS** inherited from your JupyterHub/Jupyter proxy
- **Pinned peer certificates** - a certificate you trust at connect is the only one that connection accepts, whatever names it carries; a different certificate stops the connection until you decide on it with Connect Again
- **Cloudflare exposure** is HTTPS-only and limited to the `/public/...` capability endpoints; the hub login, authenticated APIs and the private network stay unreachable
- **Connector token** passed via the `TUNNEL_TOKEN` environment variable, never on the command line - cannot leak through `ps`/`/proc`

## Availability of a link

A link is served by the JupyterLab server that created it, so it works only while that server is running.

- **Owner's server stopped** - the link stops answering; on JupyterHub the idle culler stops unused servers, so a link can go dead without anyone touching it
- **In the panel** a connected peer then shows `offline` (on a hub, `closed` for a record its owner closed); hover the badge for the reason - the panel's own server reads the peer, so the badge names the peer's answer (stopped server, removed record, rejected password), and a page policy such as `default-src 'self'` cannot stop the read
- **For recipients** the page cannot load until the owner's server is back

## Releases

Versioned releases ship to [npm](https://www.npmjs.com/package/jupyterlab_share_files_extension) and [PyPI](https://pypi.org/project/jupyterlab-share-files-extension/) together, tagged `RELEASE_v<version>` on the [GitHub releases page](https://github.com/stellarshenson/jupyterlab_share_files_extension/releases). Full delivered feature list: [RELEASE.md](RELEASE.md); per-version changes: [CHANGELOG.md](CHANGELOG.md).

## Uninstall

```bash
pip uninstall jupyterlab_share_files_extension
```
