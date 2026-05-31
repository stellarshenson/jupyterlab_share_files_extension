# Acceptance Criteria - Connected (Remote) Share Entries

Behaviour for entries inside a connected peer's share, as shown in the panel's
CONNECTED section. These files live on the peer's server, so "open" and "save"
both materialise a local copy first.

## Preconditions

- The extension (this version or newer) is installed and the Jupyter server has
  been **restarted** so the new server-side code is loaded.
- For peers behind a self-signed certificate (a typical internal JupyterHub),
  `c.ShareFilesConfig.verify_peer_tls = False` is set in `jupyter_server_config.py`.
  Otherwise the server-side fetch to the peer fails TLS verification.
- A connection to a peer share exists and the peer's server is online.

## Criteria

**AC1 - Double-click a file opens it (not download).**
Given a connected share is expanded, when the user double-clicks a file entry,
then the file is saved into the file browser's current folder and opened in a
JupyterLab tab. It is not downloaded through the browser.

**AC2 - Double-click a folder saves it.**
Given a connected share is expanded, when the user double-clicks a folder entry,
then the folder is saved (extracted) into the current folder and revealed in the
file browser. Folders do not open in a tab.

**AC3 - Drag an entry into the file browser copies it.**
Given a connected share is expanded, when the user drags a file or folder entry
onto the file browser (a folder row or the current-directory empty area), then
the entry is saved into that destination - a file as a file, a folder extracted
recursively. The cursor reads as a copy.

**AC4 - Right-click offers Download and Save.**
Given a connected share is expanded, when the user right-clicks an entry, then a
menu offers "Download" (browser download, no credentials) and "Save to Current
Folder" (server-side save into the file browser's current directory).

**AC5 - Self-signed peers do not 500.**
Given `verify_peer_tls = True` (default) and a self-signed peer, when a save is
attempted, then the server returns a clear 502 with guidance to set
`verify_peer_tls = False` - not an unhandled 500. With `verify_peer_tls = False`
the save succeeds.

**AC6 - No credentialed peer navigation.**
Browser-side downloads use `fetch(..., {credentials:'omit'})`; server-side saves
use the server's own outbound request. No top-window navigation to a peer URL
occurs, so an offline owner can never trigger JupyterHub's spawn-as-owner screen.

## Verification log

Verified against the live peer
`https://jupyterhub.lab.stellars-tech.eu/user/test.user/.../public/share/ZMNUPES2`
(self-signed hub) as user `konrad.jelen`.

- **AC5 root cause (Playwright, deployed server):** POST to the save endpoint
  returned `500 Unhandled error`. Server log showed
  `ssl.SSLCertVerificationError` from `client.fetch(..., raise_error=False)` -
  `raise_error=False` does not suppress connection-level SSL errors.
- **AC5 fix (direct, new code, live peer):** the new `_Base._peer_fetch` against
  the peer manifest returned `PeerUnavailable` when `verify_peer_tls=True` (the
  handler maps this to a clean 502) and HTTP `200` when `verify_peer_tls=False`
  (save proceeds). Confirmed `validate_cert=False` is the working path.
- **Peer endpoints reachable (Playwright, credentials omitted):** `/manifest`,
  `/download/<name>`, `/download-all` all returned `200` (json / octet-stream /
  zip), so the save handler's three fetches resolve once TLS is handled.
- **AC1-AC4, AC6 (build + unit):** TypeScript compiles; `verify_peer_tls`
  config defaults/override covered in `tests/test_config.py`; the panel logic
  (dblclick -> save+open, context menu, REMOTE_MIME drag) builds clean. Live UI
  confirmation requires the new labextension installed and the page reloaded.

## Notes / limitations

- "Open" a remote file necessarily writes a local copy first (JupyterLab opens
  by workspace path, not by bytes). The copy lands in the current folder.
- Live in-panel confirmation of AC1-AC4 is pending a deploy of this version
  (new frontend assets + server restart); the server-side save path (AC5) is
  verified against the live peer above.
