try:
    from ._version import __version__
except ImportError:
    # Fallback when using the package in dev mode without installing
    # in editable mode with pip. It is highly recommended to install
    # the package from a stable release or in editable mode: https://pip.pypa.io/en/stable/topics/local-project-installs/#editable-installs
    import warnings
    warnings.warn("Importing 'jupyterlab_share_files_extension' outside a proper installation.")
    __version__ = "dev"
from .config import ShareFilesConfig
from .hub import hub_api_base, hub_mode
from .routes import setup_route_handlers
from .storage import resolve_shares_dir


def _jupyter_labextension_paths():
    return [{
        "src": "labextension",
        "dest": "jupyterlab_share_files_extension"
    }]


def _jupyter_server_extension_points():
    return [{
        "module": "jupyterlab_share_files_extension"
    }]


def _load_jupyter_server_extension(server_app):
    """Registers the API handlers and instantiates user-overridable config.

    Configurable via jupyter_server_config.py:
        c.ShareFilesConfig.shares_dir = "/path/to/store"

    Parameters
    ----------
    server_app: jupyterlab.labapp.LabApp
        JupyterLab application instance
    """
    config = ShareFilesConfig(parent=server_app)
    setup_route_handlers(server_app.web_app, config=config)
    name = "jupyterlab_share_files_extension"
    if hub_mode():
        # No local store and no per-user tunnel: the hub holds the bytes and
        # the tunnel. The store directory is never created on a hub lab.
        server_app.log.info(
            f"Registered {name} server extension (hub mode, api={hub_api_base() or 'contract incomplete'})"
        )
        return
    workspace_root = server_app.web_app.settings.get("server_root_dir", "")
    resolved = resolve_shares_dir(workspace_root, config.shares_dir)
    server_app.log.info(f"Registered {name} server extension (shares_dir={resolved})")
    # Cloudflare sharing: honour the autostart preference (all behaviour
    # lives in the tunnel library module, shared with the CLI and api/tunnel)
    from .tunnel import apply_autostart
    apply_autostart(config.cloudflared_retries, server_app.log)
