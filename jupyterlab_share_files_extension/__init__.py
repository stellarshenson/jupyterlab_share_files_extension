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
    workspace_root = server_app.web_app.settings.get("server_root_dir", "")
    resolved = resolve_shares_dir(workspace_root, config.shares_dir)
    server_app.log.info(f"Registered {name} server extension (shares_dir={resolved})")
    # Cloudflare sharing configured -> the extension guarantees the connector
    # runs (cloudflare setup wrote the tunnel token), unless autostart was
    # switched off (Settings -> Share Files, or api/tunnel). With autostart
    # off the tunnel starts inactive so generated links stay private instead
    # of carrying a public host the tunnel can't serve. Failure after the
    # configured retries is logged as an error; success as info.
    from .cli import _load_config, _save_config, ensure_connector
    cfg = _load_config()
    if cfg.get("cloudflare_tunnel_token"):
        if cfg.get("tunnel_autostart", True):
            if not cfg.get("tunnel_active", True):
                cfg["tunnel_active"] = True
                _save_config(cfg)
            ensure_connector(config.cloudflared_retries, server_app.log)
        else:
            if cfg.get("tunnel_active", True):
                cfg["tunnel_active"] = False
                _save_config(cfg)
            server_app.log.info(
                "cloudflared connector autostart disabled - links stay private "
                "(toggle the cloud icon or api/tunnel to go public)"
            )
