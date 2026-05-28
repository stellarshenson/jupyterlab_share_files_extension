"""Configuration for the share-files extension.

Users can set options in their jupyter_server_config.py:

    c.ShareFilesConfig.shares_dir = "/some/path"

If `shares_dir` is unset (default), shares live in `.jupyterlab_shares/` under
the workspace root (server_root_dir). The directory is created lazily on first
use.
"""

from __future__ import annotations

from traitlets import Bool, Unicode
from traitlets.config import Configurable


class ShareFilesConfig(Configurable):
    """Configurable settings for jupyterlab_share_files_extension.

    Set in jupyter_server_config.py:
        c.ShareFilesConfig.shares_dir = "/absolute/or/relative/path"
        c.ShareFilesConfig.use_trash = True

    A relative path is resolved against the server_root_dir. An absolute path
    is used as-is. The directory is created on demand and does not need to
    exist before the extension starts.
    """

    shares_dir = Unicode(
        "",
        config=True,
        help=(
            "Directory where shares, requests, and connections are stored. "
            "If empty (default), uses `uploads/` under server_root_dir. "
            "Relative paths are resolved against server_root_dir."
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
