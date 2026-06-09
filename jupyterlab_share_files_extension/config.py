"""Configuration for the share-files extension.

Users can set options in their jupyter_server_config.py:

    c.ShareFilesConfig.shares_dir = "uploads"

A relative `shares_dir` is resolved against the notebook root (the Jupyter
server's `root_dir`, the folder the file browser starts in). The directory is
created lazily on first use.
"""

from __future__ import annotations

from traitlets import Bool, Int, Unicode
from traitlets.config import Configurable


class ShareFilesConfig(Configurable):
    """Configurable settings for jupyterlab_share_files_extension.

    Set in jupyter_server_config.py:
        c.ShareFilesConfig.shares_dir = "uploads"        # relative to notebook root
        c.ShareFilesConfig.shares_dir = "data/uploads"   # any path INSIDE the root
        c.ShareFilesConfig.use_trash = True

    A relative path is resolved against the notebook root (the Jupyter server's
    `root_dir` - same folder the file browser starts in). An absolute path is
    used as-is, but it MUST land inside the notebook root. The extension
    refuses to start otherwise - shares outside the root are unreachable from
    JupyterLab's file browser and would break the panel's drag-out, copy, and
    "Show in File Browser" actions. The directory is created on demand.
    """

    shares_dir = Unicode(
        "",
        config=True,
        help=(
            "Directory where shares, requests, and connections are stored. "
            "If empty (default), uses `uploads/` under the notebook root "
            "(Jupyter server's `root_dir`). Relative paths are resolved "
            "against the notebook root; absolute paths are used as-is. The "
            "resolved path must live inside the notebook root or the "
            "extension refuses to start."
        ),
    )

    use_trash = Bool(
        True,
        config=True,
        help=(
            "When True (default), files and folders deleted via the panel "
            "(whole shares/requests, removed share items, removed request "
            "uploads) are sent to the OS trash via send2trash. When False, "
            "they are deleted permanently."
        ),
    )

    public_base_url = Unicode(
        "",
        config=True,
        help=(
            "External base URL share/request links are rewritten to, e.g. "
            "'https://share.example.com'. Only the scheme and host are used; "
            "the path is auto-detected from the server's base_url. If empty "
            "(default), the value is read from the CLI config file "
            "(~/.config/jupyterlab-share-files/config.json, written by "
            "`jupyterlab_share_files cloudflare --setup`); if that is also "
            "empty, links use the host the browser is on (old behaviour)."
        ),
    )

    cloudflared_retries = Int(
        3,
        config=True,
        help=(
            "How many times the server extension tries to start the "
            "cloudflared connector at startup when Cloudflare sharing is "
            "configured. All attempts failing is logged as an error "
            "(Cloudflare links will not work until the connector runs)."
        ),
    )

    verify_peer_tls = Bool(
        True,
        config=True,
        help=(
            "When True (default), the server verifies the peer's TLS "
            "certificate when saving from a connected share or uploading to a "
            "connected request. Set to False when peers (e.g. a JupyterHub) "
            "use a self-signed certificate - otherwise those server-side "
            "fetches fail with a certificate verification error."
        ),
    )
