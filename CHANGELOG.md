# Changelog

<!-- <START NEW CHANGELOG ENTRY> -->

## [1.2.56] - 2026-09-24

### Changed

- While a tunnel comes up, the header cloud icon itself breathes in the accent blue, fading to half strength and back over 2.4 s; the ring that pulsed around it, which read as a box, is removed. Where the system asks for reduced motion, the icon stays still at full strength inside a thin ring

## [1.2.55] - 2026-09-24

### Added

- Every entry of the panel's menus shows an icon, at the size JupyterLab's own menus use; Save as Zip has a zipped-folder icon

### Changed

- Hub mode: a file of a share and an upload under a request save into the current folder from the row's menu; the row no longer carries a download-icon button
- The whole-record saves on a share, request or connected share read "Save to Current Folder" and "Save as Zip", without "Record"

## [1.2.54] - 2026-09-24

### Changed

- The Connect button is the right end of the link input, with no gap between them. It is a faded outline and cannot be pressed while the input is empty or holds only spaces, is filled in the colour of the theme's icons once a link is in, and keeps its fill, faded, while a connection is being made
- The disabled Connect button is faded by opacity, so it also looks disabled in the Dark High Contrast theme

### Fixed

- Pressing Connect from the keyboard keeps the focus in the link input; before, the focus dropped to the page, and a password or trust dialog returned it there

## [1.2.53] - 2026-09-24

### Added

- A link whose host presents a certificate the system does not trust, a self-signed one among them, opens a dialog at connect with the host, the reason and the SHA-256 fingerprint; Do Not Trust is the default. Trust keeps that one certificate with the connection until it is removed, and every fetch for the connection accepts that certificate only. A different certificate later marks the row "untrusted", and Connect Again asks about the new one. A certificate the lab could not use even once trusted, an expired one, is named at connect and not offered. Standalone and hub mode alike
- `jupyterlab_share_files connect <link> --trust-certificate` trusts such a certificate from the command line, as Trust does in the panel, and prints the host and the fingerprint; without the flag, `connect` stops, shows the fingerprint and names the flag

### Changed

- `c.ShareFilesConfig.verify_peer_tls = False` is no longer needed for a self-signed peer or hub; it now skips the check, and the question, for every peer
- The Connect button is filled in the colour of the theme's icons instead of the accent blue, and it stays whole in a narrow sidebar
- Hub mode: a connection to another user's share or request reads the record through the link its owner handed out, as a recipient's browser does, and sends it no hub token: the page, its unlock form, `/d/<path>`, `/archive` and `/u`. The hub answers 404 for a record the caller does not own, by design, so the `records/<id>` routes requested for 1.2.50 are withdrawn, and a connection now works on the live hub
- Hub mode: a connection needs the whole link; an id alone, or a `/hub/s/` address, is refused with a sentence asking for the link
- Hub mode: a connected share's bytes pass through the lab, streamed to disk within the peer download limit; a connected request's files go one per request under their own names, so a folder's files arrive without the folder, and an upload refusal names its reason
- Hub mode: the panel reads connected rows again on its poll interval, and once a second while the lab sends an upload into one, because the hub signals no change to another user's record
- Hub mode: a drop or paste onto a connected request whose row says closed, password changed or untrusted sends nothing and names the row's reason; disconnecting during an upload says that a file already on its way is still sent and no further file is
- Hub mode: a file larger than the connected request's limit, as its page states it, is named with the limit, and nothing is sent; an upload into a request its owner closes meanwhile says it ended; an upload into a request that does not answer for now says it has started, and its end is said when the record answers again
- Dependencies moved to the newest release inside their ranges (`@jupyterlab/*` 4.6.4, webpack 5.111.1, prettier 3.9.9); the Makefile follows the canonical 1.41, which increases the version on `make publish` only and runs the tests before it publishes

### Fixed

- The `password changed` row label wraps inside its row in a narrow sidebar instead of spilling over the rows above and below
- `jupyterlab_share_files connect` lists a share's entry names by reading them through the lab, so they appear for a protected, trusted or hub connection too; the bundled skill no longer claims that `connect` asks for a password
- `disconnect`, `pick-up` and `send-to-request` reach a standalone connection by its key; the key holds the peer's `https://`, so it was not found before
- A hub paste that is refused no longer also says it was pasted as a copy, and the cut stays on the panel clipboard for the next try; a paste that ends after a newer cut or copy leaves that one in place

## [1.2.50] - 2026-09-23

### Added

- Hub mode: a connection to another user's share or request, by its hub link on the hub's address, its Cloudflare hostname or under `/hub`, or by its id alone. A protected record asks for its password once, your own record is refused by name, and the hub reads the record with the lab's token. A connected share saves an entry, or the whole share unpacked or as a zip, into the current folder; a connected request takes dropped or pasted files and folders, wears the progress layer while the hub copies them, and says how many landed or why the hub refused them. It is built against the `records/<id>` routes asked for in `HUB-API-REQUEST-read-a-record-you-do-not-own.md`, which the hub does not serve yet; until it does, a connect answers with the hub's 404
- A whole share or request saves into the file browser's current folder, as a folder of its files or as one zip, through `POST api/<shares|requests>/<id>/save`; a second save takes the next free name, and a save that fails part way removes what it wrote. A standalone connected share gains the same zip option
- If the owner sets or changes a connected record's password later, the row reads `password changed`, and Connect Again in its menu asks for the new one
- The header cloud icon grows one ring when the tunnel arrives after its wait, and not under `prefers-reduced-motion` (`DEF-PANEL-104`)

### Changed

- A hub fetch waits 300 s per GB of the record, the upload or the share, and at least 300 s, instead of the 30 s API timeout (`DEF-HUB-110`, `DEF-HUB-120`)
- Every save writes into its own hidden staging folder and renames it to the free name when it lands, so two saves of one record into one folder cannot delete each other's files (`DEF-HUB-122`)
- A new connection opens the Connected section and scrolls its row into view (`DEF-PANEL-115`), and a whole-record or zip save names the path it wrote (`DEF-PANEL-114`)

### Fixed

- Hub mode: a chosen path, or a current folder for a save, that leads out of the workspace through a link is refused by name before the hub is asked, instead of coming back refused or failed (`DEF-HUB-105`, `DEF-HUB-106`)
- A file dated before 1980 no longer breaks a zip save (`DEF-STORE-111`)
- A refused upload into a connected request says why on the row and in the notification, in the uploader's words, instead of quoting the uploader's group limit (`DEF-HUB-112`)
- An upload the hub settles before the panel's next read is announced (`DEF-HUB-113`), and a read sent before the upload was accepted no longer announces an upload that has not landed (`DEF-HUB-118`)
- A connected record whose password the owner set or changed no longer reads as a group-policy refusal or as `offline` (`DEF-HUB-109`, `DEF-HUB-119`, `DEF-HUB-123`); a stored password the hub refused is not sent again to use up the unlock limit (`DEF-HUB-121`), and a refused unlock no longer erases a password a connect has just stored (`DEF-HUB-124`)
- Focus after a delete lands on the neighbouring row when the list changed above it (`DEF-TESTS-107`)
- Tests: the hub cloud toggle test waits for the switch-off to reach the records (`DEF-TESTS-108`), a test proves the upload tint moves (`DEF-TESTS-117`), and the concurrent save test fails when the staging name is shared (`DEF-TESTS-125`)

## [1.2.49] - 2026-09-22

### Changed

- Hub mode: Cloudflare sharing is one switch over every record, not a flag each record carries. While the switch is on, listing the shares brings onto the tunnel any record whose own switch is off - a record whose switch-on never landed used to keep a hub-network link while the panel said sharing was on - and switching off writes the switch before it reads the records, reads it again after every write so a write that lands after the press is taken back, and keeps the press when the press moved anything
- The header cloud icon breathes while a tunnel comes up - a slow ease in and out over 2.4 s, the wait being about 90 seconds - and sits still on the panel's accent once the tunnel is up. The breath is drawn on the icon's ring, so the glyph holds full strength and stays over the 3:1 contrast a non-text control has to clear

### Fixed

- Hub mode: a share whose link carried the hub's own address while the panel reported sharing on (`DEF-HUB-101`)
- The galata test server serves only this build's labextension and galata's own helper, and reads the extension manager without network. The image these suites run in carries about forty other labextensions, whose frontends exhausted the browser's six connections per origin and left the panel's first `api/info` queued for 16 s while the server answered every request in under 7 ms, failing tests for the image rather than the code (`DEF-TESTS-102`)
- The hub-mode integration suite runs as its own GitHub job. As a step of the standalone job it never ran when that suite failed

## [1.2.48] - 2026-09-22

### Added

- Server option `c.ShareFilesConfig.excluded_names` - the catalogue of names a share never copies, matched with `fnmatch` against one path component - the dropped item's own name in both modes, and at every depth inside a folder only where the lab copies that folder itself (a standalone lab; on a hub the patterns are matched against the dropped item's own name only, and the plain names - no pattern, the first 64 - are sent to the hub, which leaves them out at every depth). The default holds the checkpoint and cache folders (`.ipynb_checkpoints`, `__pycache__`), the macOS artefacts (`__MACOSX`, `.DS_Store`, `._*`) and the trash folders (`.Trash`, `.Trash-*`, `.Trashes`, `.trashed-*`, `$RECYCLE.BIN`); setting the option replaces the whole catalogue and `[]` copies everything. A drop where every item is excluded is refused with a sentence naming them

- Hub mode: a share is edited after it was created, through the hub's `POST shares/<id>/content`. New Share creates an empty share; files and folders dropped on a share row are added; a file or folder inside a share is removed, renamed in place with `F2` or the row menu, and moved by a drag onto a folder row of the same share. The lab's routes are `POST`, `DELETE` and `PUT api/shares/<id>/items`
- Hub mode: the share row shows a spinner with the percentage copied while the hub stages a share or copies an add, and, after the item count, `add refused` with the hub's reason when it refused the last add - read from the hub's own `last_add` and `progress` row fields
- Hub mode: the plain names of the excluded-names catalogue are sent to the hub as `exclude`, so the hub leaves them out at every depth below a shared folder
- Hub mode: an add of a name the share already holds, or of two items with one name, is refused by name before the hub is asked

### Changed

- One wording for saving a file out of the panel: `Save to Current Folder` on an own share entry (was `Copy to Current Folder`), on a connected peer's entry, and on an upload under a request on a hub (was `Fetch to current folder`)
- Hub mode: the header cloud icon no longer blinks while Cloudflare sharing is on and no share or request asks the hub for a tunnel - it shows the still silhouette inside a thin ring with `Cloudflare sharing on - no tunnel up yet`, and blinks only while the hub brings a tunnel up
- **Breaking** - hub mode needs a hub that serves content editing and the change stream and reports `last_add` and `progress` on the share row (galaxahub 4.4.151 or later). The timer the panel used against a hub without the stream route, and the `poll` event that announced it, are gone
- **Breaking** - data written by releases before uploader identity and stored links is no longer read: a folder under a request without its uploader sidecar file is not listed as an uploader, a connection stored without its link answers `Connection has no link - disconnect it and connect again` on save and upload, reconnecting no longer writes a link into such a connection (remove it and connect again), and a pasted link without the extension's namespace in its path is refused
- **Breaking** - hub mode speaks the hub's tunnel API and no longer works with a hub that serves the old `cloud` routes. A record's switch is `PUT <shares|requests>/<id>/tunnel` with `{"tunnel": <boolean>}`, a hub row's state is read from its `tunnel` field, and the lab's own route is `api/<shares|requests>/<id>/tunnel`, taking and answering `tunnel`. The hub's `<id>/cloud` routes answer 404
- Hub mode: the header cloud icon follows the hub's `tunnel_available` and `tunnel_ready` together with the lab's stored default, so the hub alone owns the state. The 120 s confirmation wait after a switch on is gone, and `tunnel_waiting`, `tunnel_reason` and the reasons `cloud_not_confirmed`, `cloud_not_switched_off` and `cloud_not_switched_on` with it; the hub rings the lab's change stream when its tunnel comes up or drops, so the icon moves between on and pending with no click. `api/info` and `api/tunnel` report `tunnel_available`, `tunnel_ready` and `tunnel_default`
- The lab's stored hub default moved from `hub_cloud` to `hub_tunnel`; the earlier key is not read, so a lab that had the toggle on switches it on once more
- The Cloudflare switch lives in the panel header alone: the row menus of a share and a request no longer carry "Hub Network Only" or "Share Through Cloudflare". The header icon still flips every record and sets the default for the next one
- The header's cloud icon takes the colours the panel's other header icons use - the accent when on, the themed grey when off - instead of a success green no other control carries. "On" and "switching" share that accent, so the glyph separates them: the filled cloud is on, the blinking dashed silhouette is a switch in flight, which is what reads where the blink is suppressed
- The drop zone reads with one gap on all four sides: its margin and its padding each carry a single length
- The hub's policy refusal of a Cloudflare switch is read under its current name `tunnel_not_available`
- A refused New Request stays clickable and answers with the hub's reason, because a greyed menu entry carries its reason in a caption Lumino never renders
- A transfer in flight is drawn across its own row instead of beside it: a translucent accent layer lying over the row, from the row's left edge to the fraction copied, with the row's name, meta and buttons readable through it and its leading edge at full accent strength. Both surfaces carry it - a share row in the panel while the hub copies, and a file's card on the recipient's upload page. The layer is the only place the fraction is stated, so it carries the progressbar role and its value for a screen reader, and the ` staging 42%` text is gone. A row with nothing in flight carries no layer

### Fixed

- Hub mode: the link dialog dropped its password line and its reachability line for every hub link. A record's public address now carries the group's policy id (`/s/<policy id>/<record id>`, the Cloudflare address included) and the lab parsed only the older one-segment form (DEF-HUB-98)
- The recipient's upload page kept its progress layer after a transfer ended, so a finished card wore a full accent wash and a refused one a partial wash under its red status line; the layer is removed when the transfer ends, as it already was on a panel row the hub has settled (DEF-PUBLIC-99)

## [1.2.47] - 2026-09-18

Disk-bound transfers with a configurable download limit, the r10 to r12 adversarial review arcs, and hardening of the Cloudflare switch and configuration write.

### Added

- Settings Editor: "Largest download from a connected share (GB)" - 1, 2, 5, 10, 20, 50 or 100 GB, default 10; the panel sends it with every save and download (`max_gb`) and the server enforces it; a request without it, the CLI's `pick-up` among them, gets 10 GB

- A recipient's upload streams to disk as it arrives: the request page sends the file as a raw body with its name in an `X-Filename` header (percent-encoded UTF-8, the shape the galaxahub fileshare service takes), the server writes it to a spool file under the store's `tmp` folder and moves it into the uploader's folder when complete; the 512 MiB body limit of jupyter_server no longer applies - a body larger than the disk's free space is refused with 413 `Not enough space` before it is read, a disk that fills mid-way is answered 500 `Could not store the upload` on the chunk it refuses (the connection closes under the rest of the body, so the upload stops), and a client that drops mid-way leaves nothing behind

### Changed

- A share is served chunk by chunk (a single file with its length, a folder or "download all" as a zip written into the response file by file) instead of being read, or zipped, whole into memory first; a member that vanishes while its folder is zipped closes the connection instead of finishing a cut archive as complete; the recipient page hands zip downloads to the browser directly, which shows progress from the first byte, instead of fetching them into a blob behind a "Compressing" overlay; every download anchor of the page carries a `download` attribute, so a refused or broken-off download is a failed entry in the browser's download list and the page stays
- An upload from the panel to a connected request streams from the workspace file (raw body, `X-Filename`, `Content-Length`) instead of reading it into memory as a multipart body, and may take 300 s for every GB of the file, at least 300 s, instead of a flat 300 s; a peer running an older version answers 400 `no file` to the new shape
- A download from a connected share is relayed to the browser while the peer's body is still arriving, with the length the peer announced and, for a folder, the `.zip` name the peer's archive carries; a peer that fails after the first byte closes the browser's download instead of appending an error, and a browser that cancels ends the peer fetch at its next chunk
- The panel's Download of a connected peer's file is saved by the browser itself (an anchor click) instead of being fetched into a blob; a failure shows in the browser's download list, not as a notification
- A file that arrived through a spool (a save, a relayed download, an upload) lands with the mode a file the lab writes gets, not the spool's private 0600
- Peer downloads are written to disk as they arrive - a spool file under the store's `tmp` folder - and never held in the server's memory; a plain file moves from the spool into the workspace, an item handed to the browser streams from it with its length; the spool is removed on every path
- A download may take 300 s for every GB of its limit and at least 300 s (the 10 GB default allows 3000 s) instead of a flat 300 s; the fixed 1 GiB cap on a shared client is replaced by one client per download carrying the request's limit
- The limit's messages read in GB: `The peer's download is larger than the 10 GB limit`, `The share is larger than 10 GB when unpacked`; a folder or Save All zip, which announces no length, ends past the limit the same way a dropped connection does, and the message says so: `The peer's download passed the 10 GB limit, or the peer closed the connection before it finished`
- A folder member dated outside zip's range (before 1980, after 2107) is zipped with its date clamped, as every archiver does, instead of failing the folder
- Cloudflare: `cloudflare setup` no longer prints or returns the `run_command` line that carried the connector token in clear; the standalone switch runs the connector start and stop off the server's event loop, refuses with `cloudflared did not start - see <connector log>` and keeps links private when no connector came up, and the connector log names a binary that could not be launched; the configuration file is written beside itself and moved into place (0600 from the first byte), so a disk that fills mid-write keeps the previous configuration
- Panel: the cloud icon shows the switch in flight when switching off as well as on; delete dialogs name the share or request; a failed Connect keeps the pasted link; "link copied" is said only when the clipboard took the link; Enter on a focused entry opens it (a folder drills in, the `..` row goes up); the setup dialog's inputs are labelled; the hub-mode Fetch button no longer hovers in delete's red; the offline badge reads at 4.5:1
- Recipient page: a failed upload or remove shows the server's sentence instead of a status code or an alert; an unlock refused for a reason other than the password names it (a removed link, an unreachable share); the name field is labelled, the theme buttons carry `aria-pressed`, upload and remove outcomes are live regions, each Remove button names its file; file sizes and footer read at 4.5:1; a long name stops before the theme switch on a phone; download buttons carry no underline

### Fixed

- Hub mode: a hub that stopped answering during the Cloudflare confirmation wait and came back with its tunnel connector still starting (a redeployment) had its switched-on records flipped back off at the 120 s bound; the bound now restarts once when the hub answers again, so a redeployed hub gets a full bound to register its tunnel (DEF-HUB-71)
- Connecting a link whose owner had removed the share or request stored the connection and showed it offline; it is now refused with `The owner has removed this share or request.` (DEF-PEER-73)
- Screen readers spoke the row twisty glyph before every row name and a row could not be expanded from the keyboard; the twisty is a button named `Expand` or `Collapse` with `aria-expanded`, Enter and Space toggle the row and the focus stays on it (DEF-PANEL-74)
- A folder download whose first walked member could not be read, or whose name zipfile cannot encode, ended with an empty answer or tornado's HTML page; it answers 500 `Could not read the folder`
- Downloading a shared file or folder whose name carries a character outside latin-1 (ż, CJK) answered tornado's HTML 500; the download headers use the RFC 5987 `filename*=UTF-8''` form
- 32 of the panel's notifications (could not connect, could not save, could not delete, new upload, saved, fetched, item(s) added) landed only in the notification bell and never showed as a toast: JupyterLab keeps a notification without `autoClose` out of the toasts

## [1.2.46] - 2026-09-17

Cloud confirmation, keyboard access, peer-save hardening and same-origin peer reads. 1.2.45 was never published; its version field came from a local reinstall.

### Added

- Hub mode: a Cloudflare switch on is confirmed against the hub's items (at most every 5 s for at most 120 s) before the panel reports it; not confirmed, the records and the default go back off and the header icon carries the reason; `api/info` and `api/tunnel` report `tunnel_waiting` and `tunnel_reason`
- Link dialog: a link on the Cloudflare hostname is opened from the lab server and the dialog shows what answered (HTTP status, no answer, connection refused, no address, TLS error, closed connection, network error); a link on the hub's own address is not checked and the dialog says it works on the hub's network only
- Keyboard: Tab reaches every share, request and connection row and a connected peer's entries, arrow keys move, Enter and Space act, Shift+F10 or the ContextMenu key opens the row menu, Delete removes a row after confirmation; focus survives re-renders and lands on the section header when a section's last row is deleted; section headers fold and unfold with Enter and Space and carry `role=button` and `aria-expanded`
- Galata: an opt-in live-hub suite (`jlpm test:livehub`) runs five tests against the real galaxahub with the operator's credentials; the standalone suite's server carries galaxalab's Content-Security-Policy so the cross-origin regression stays covered
- Server-side unlock token cache for password-protected peers, reused until 60 s before the token's expiry, so the panel's polls stay inside the peer's password cooldown

### Changed

- Peer reads: the panel reads a connected peer's manifest and downloads its files through the lab's own server (`api/connections/<key>/manifest`, `api/connections/<key>/download`), never straight from the browser, so a page policy of `default-src 'self'` cannot break connections; the offline badge and the download notification name the peer's answer (stopped server, removed record, rejected password)
- Peer save: one download is capped at 1 GiB on a dedicated client, the manifest and entry names are gated before any write, a corrupt archive is named in one readable message, and a partial save is removed on every failure path
- Peer answers keep their meaning at every step: a 401 says the password changed, a 404 says the owner removed the share or request, and other codes read `Remote unavailable (<code>)`, at the unlock as well as at the fetch
- Galata harness: the server configuration copies the working-tree build under `.galata-root` instead of serving it through a symlink, which tornado 6.5.10 refuses

### Fixed

- Connected peers on another origin showed offline under a `default-src 'self'` Content-Security-Policy (DEF-PEER-72)
- Deleting the only row of a section dropped keyboard focus to the document body (DEF-PANEL-70)
- Connecting a password-protected peer showed it offline with "too many password attempts" on the first poll
- Screen readers spoke the section header's twisty glyph before its name

<!-- <END NEW CHANGELOG ENTRY> -->

## [1.2.44] - 2026-09-05

### Changed

- Republish of 1.2.43 with identical code; the release command was invoked again on a clean tree

## [1.2.43] - 2026-09-05

Hub mode for galaxahub-spawned labs, reconciled with galaxahub v4.4.56.

### Added

- Hub mode: on a lab spawned by galaxahub (`SHARE_FILES_PUBLIC_ZONE=hub`) the extension mounts only its authenticated `api/*` routes and every panel action goes through the hub's fileshare API; the hub stages and serves the files, the lab answers 404 on its recipient paths and starts no tunnel
- Live panel: the lab holds one change stream to the hub and the panel one to the lab, so a new upload, a share turning ready or refused, a close or an expiry shows without polling; an older hub without the stream puts the panel back on its timer
- Per-record Cloudflare switch: the header cloud icon flips every share and request and sets the default for the next one, a row's context menu flips one; switched-on rows carry a cloud mark and the link dialog says when a link works on the hub network only
- Password policy: a group that requires a password gets a create dialog with the field required and pre-filled with a generated passphrase
- Galata suite for hub mode against a mock hub, run in CI beside the standalone suite

### Changed

- Dependency ranges: floor at the tested minor, ceiling at the next major, for the Python and npm dependencies
- Makefile updated to the canonical 1.37
- Acceptance criteria consolidated into one `docs/acc-crit.md`

### Fixed

- A hub connection left behind by a closed panel could exhaust the server's HTTP client queue; the hub stream is now read over a socket the lab closes itself
- A panel whose stream the browser closed for good (a non-200 answer while the lab restarts) falls back to its timer instead of never refreshing again
- The link dialog opened from the context menu shows the same hub-network-only line as the row button

## [1.2.40] - 2026-08-05

Explain why a peer is offline, and stop caching public responses.

### Changed

- A connected peer shown as "offline" now says why - the badge carries the reason as a tooltip and the console logs it. Most often the peer's server is simply stopped: a share link is served by its owner's JupyterLab, so it only works while that server is running, and JupyterHub stops idle servers

### Fixed

- Public share and request manifests, and the recipient pages, are no longer stored by any cache. A request manifest shows only the caller's own uploads yet was cacheable with no `Vary`, so a shared cache could serve one uploader's file list to another; a cached page could also skip the password prompt after the owner set a password
- The recipient page now shows the password prompt when a link turns out to be protected, instead of dead-ending on "Share unavailable" - including while the page is already open

## [1.2.39] - 2026-07-25

Storage folder created only when needed.

### Changed

- The storage folder (`uploads/` by default) is no longer created when the server starts - it appears only when you create your first share or request, or connect to a link, so a workspace that has never shared anything stays clean

### Fixed

- Creating a share from a stale file-browser selection (a file renamed or deleted since the menu opened) no longer leaves an empty storage tree and an invisible, never-cleaned-up folder behind; the share is rolled back if any part of it fails
- `connections.json` is now written atomically - an interrupted write could previously leave an unreadable file that was silently treated as "no connections", losing every connection you had

## [1.2.38] - 2026-07-15

Adversarial review fixes: public-manifest privacy and resilient panel refresh.

### Fixed

- Public share and request manifests no longer expose the owner's workspace path or file modification times - recipients see only each entry's name, type and size
- Background panel refresh no longer misreports a real server error as an offline blip - only a genuine dropped connection keeps the last-good view quietly, while real HTTP errors and code bugs are logged
- Hover tooltip now shows the Modified date on drilled-in sub-folder rows, matching top-level rows
- Connected peer directory rows render with a trailing `/` like owned rows
- Removed a debug log that fired on every drag-and-drop

## [1.2.37] - 2026-07-11

File hover tooltip.

### Added

- Hovering a file or folder row in a share or request shows a tooltip with its full name, path, size and modified date; on connected peer shares the remote path is omitted

## [1.2.36] - 2026-06-12

QR native context menu fix and PNG rendering.

### Changed

- The QR code now renders as PNG (canvas-drawn) instead of the GIF data URL from `createDataURL` - "Copy Image" yields a PNG that pastes cleanly everywhere

### Fixed

- Right-click over the QR code now actually opens the browser's native menu in Chrome - JupyterLab's Dialog kills `contextmenu` with a capture-phase handler on the dialog node, so the 1.2.35 img-level workaround never fired; a document-level capture listener now lets the event through for the QR image only

## [1.2.35] - 2026-06-12

QR copy menu and tunnel-aware link dialog.

### Added

- The QR code in the link dialog answers the browser's native context menu (right-click -> Copy Image) instead of the JupyterLab menu
- The link dialog shows "Cloudflare sharing is not running - link works on this network only" when a tunnel is configured but switched off

### Changed

- The link reachability check appears only when Cloudflare sharing is configured and active - without a public link there is nothing to probe

## [1.2.34] - 2026-06-12

Embedded copy icon in the link dialog.

### Changed

- The link-dialog Copy control is now a copy icon embedded inside the link input at its right edge (browser-URL-bar style) instead of the detached button shipped in 1.2.33 - no button chrome, subtle hover highlight, glyph flips to a green check for 1.2 s after copying (red on failure); design language documented in `docs/acc-crit-link-dialog-copy.md`

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
