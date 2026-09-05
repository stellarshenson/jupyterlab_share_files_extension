"""Tornado handlers for the share-files extension.

Two handler groups:
  * api/*       - authenticated, used by the side panel
  * public/*    - unauthenticated, used by standalone HTML pages and by
                  remote JupyterLab instances that have connected to a link

The public endpoints rely on the share/request ID being secret (8-char base32).
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import ssl
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tornado
import tornado.httpclient
import tornado.ioloop
import tornado.web
from jupyter_server.base.handlers import APIHandler
from jupyter_server.utils import url_path_join
from tornado.web import StaticFileHandler

from .config import ShareFilesConfig
from .hub import hub_mode
from .storage import (
    SHARES_DIR_NAME,
    ConnectionStore,
    NotFoundError,
    RequestStore,
    ShareStore,
    StorageError,
    _is_safe_relative,
    _resolve_unique_target,
    _safe_name,
    generate_password,
    mint_uploader_hash,
    resolve_shares_dir,
    verify_password,
)


EXTENSION_NAMESPACE = "jupyterlab-share-files-extension"


def _shares_dir_setting(handler) -> str:
    """Return the configured shares_dir override (or empty string)."""
    cfg: ShareFilesConfig = handler.settings.get("share_files_config")
    if cfg is None:
        return ""
    return cfg.shares_dir or ""


def _use_trash_setting(handler) -> bool:
    """Return whether deletes should go to the OS trash."""
    cfg: ShareFilesConfig = handler.settings.get("share_files_config")
    if cfg is None:
        return True  # match default
    return bool(cfg.use_trash)


def _verify_peer_tls_setting(handler) -> bool:
    """Return whether peer (cross-server) TLS certs should be verified."""
    cfg: ShareFilesConfig = handler.settings.get("share_files_config")
    if cfg is None:
        return True  # match default
    return bool(cfg.verify_peer_tls)


class PeerUnavailable(Exception):
    """A server-side fetch to a connected peer could not be completed."""


# --------------------------------------------------------------------------- #
# Base classes
# --------------------------------------------------------------------------- #


class _Base(APIHandler):
    """Common helpers: store factories, JSON body parsing, error reporting."""

    @property
    def workspace_root(self) -> str:
        return os.path.expanduser(self.settings["server_root_dir"])

    @property
    def shares_dir(self) -> str:
        return _shares_dir_setting(self)

    @property
    def use_trash(self) -> bool:
        return _use_trash_setting(self)

    @property
    def verify_peer_tls(self) -> bool:
        return _verify_peer_tls_setting(self)

    async def _peer_fetch(self, url: str, **kwargs):
        """Fetch a connected peer's public endpoint server-side.

        Honours the `verify_peer_tls` config (self-signed peers need it off)
        and converts TLS / connection errors - which `raise_error=False` does
        NOT suppress - into a `PeerUnavailable` the handler maps to a 502.
        """
        client = tornado.httpclient.AsyncHTTPClient()
        try:
            return await client.fetch(
                url,
                raise_error=False,
                validate_cert=self.verify_peer_tls,
                **kwargs,
            )
        except ssl.SSLError as exc:
            raise PeerUnavailable(
                "TLS verification failed for the peer "
                f"({exc}). If the peer uses a self-signed certificate, set "
                "c.ShareFilesConfig.verify_peer_tls = False in "
                "jupyter_server_config.py."
            ) from None
        except (OSError, ConnectionError) as exc:
            raise PeerUnavailable(f"Could not reach the peer: {exc}") from None

    async def _peer_auth_headers(self, conn: dict) -> dict:
        """Unlock a password-protected peer resource before fetching from it.

        Connections to protected shares/requests persist the password; this
        trades it for a short-lived unlock token via the peer's public unlock
        endpoint and returns the ``X-Share-Token`` header to send on every
        subsequent peer fetch. Unprotected connections return no headers.
        """
        password = conn.get("password") or ""
        if not password:
            return {}
        link = (conn.get("link") or "").rstrip("/")
        if not link:
            return {}
        resp = await self._peer_fetch(
            link + "/unlock",
            method="POST",
            body=json.dumps({"password": password}),
            headers={"Content-Type": "application/json"},
        )
        if resp.code != 200:
            raise PeerUnavailable(
                "The peer rejected the stored password (the owner may have "
                "changed it) - reconnect the link with the new password."
            )
        try:
            token = json.loads(resp.body).get("token") or ""
        except ValueError:
            token = ""
        return {"X-Share-Token": token} if token else {}

    @property
    def share_store(self) -> ShareStore:
        return ShareStore(self.workspace_root, self.shares_dir, self.use_trash)

    @property
    def request_store(self) -> RequestStore:
        return RequestStore(self.workspace_root, self.shares_dir, self.use_trash)

    @property
    def connection_store(self) -> ConnectionStore:
        return ConnectionStore(self.workspace_root, self.shares_dir)

    def write_error_json(self, status: int, message: str) -> None:
        self.set_status(status)
        self.finish(json.dumps({"error": message}))

    def write_json(self, payload: Any) -> None:
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(payload))


class _PublicBase(tornado.web.RequestHandler):
    """Unauthenticated base for standalone pages and cross-peer endpoints.

    Extends plain tornado RequestHandler (not JupyterHandler) so we don't
    inherit jupyter_server's identity / origin / CSRF prepare() machinery -
    those reject unauthenticated requests in JupyterHub setups.
    """

    def check_xsrf_cookie(self):  # noqa: D401
        return None

    def set_default_headers(self):
        # Cooperative call so this class never terminates the chain. Note the
        # invariant it does NOT provide: `_UncachedPublicMixin` must be listed
        # FIRST in the bases (`class X(_UncachedPublicMixin, _PublicBase)`) -
        # listed second it lands after RequestHandler in the MRO, which does
        # not call super(), so its Cache-Control would silently never be set
        # while CORS still worked. Pinned by test_mixin_precedes_public_base.
        super().set_default_headers()
        # Allow cross-origin GETs of manifests and downloads so other peers'
        # JupyterLab panels can fetch directly from this server.
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.set_header(
            "Access-Control-Allow-Headers", "Content-Type, X-Share-Token"
        )

    def options(self, *args, **kwargs):
        self.set_status(204)
        self.finish()


    @property
    def workspace_root(self) -> str:
        return os.path.expanduser(self.settings["server_root_dir"])

    @property
    def shares_dir(self) -> str:
        return _shares_dir_setting(self)

    @property
    def share_store(self) -> ShareStore:
        return ShareStore(self.workspace_root, self.shares_dir)

    @property
    def request_store(self) -> RequestStore:
        return RequestStore(self.workspace_root, self.shares_dir)


# Cached content of the CLI config file, keyed by mtime so `cloudflare setup`
# / `reset` / the api/tunnel toggle take effect on the next request without a
# server restart and without re-reading the file on every link built.
_CONFIG_FILE_CACHE: dict[str, Any] = {"path": None, "mtime": None, "value": {}}


def _config_file_state() -> dict:
    """Read the CLI config file (mtime-cached)."""
    from .tunnel import config_path

    path = config_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _CONFIG_FILE_CACHE["path"] == path and _CONFIG_FILE_CACHE["mtime"] == mtime:
        return _CONFIG_FILE_CACHE["value"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    _CONFIG_FILE_CACHE.update(path=path, mtime=mtime, value=value)
    return value


def _parse_origin(raw: str) -> str:
    """Reduce a base URL to scheme://host, or ''."""
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    if not parsed.netloc:
        return ""
    return (parsed.scheme or "https") + "://" + parsed.netloc


def _raw_public_origin(handler: tornado.web.RequestHandler) -> str:
    """Configured external origin regardless of the tunnel toggle.

    Used where "is this OUR address?" must hold even while the tunnel is
    switched off (self-connect detection) and for the configured/active
    distinction in api/info.
    """
    cfg: ShareFilesConfig = handler.settings.get("share_files_config")
    raw = (cfg.public_base_url if cfg is not None else "") or ""
    if not raw:
        raw = _config_file_state().get("public_base_url") or ""
    return _parse_origin(raw)


def _tunnel_active() -> bool:
    """The api/tunnel toggle: public links when on, private when off."""
    return bool(_config_file_state().get("tunnel_active", True))


def _configured_public_origin(handler: tornado.web.RequestHandler) -> str:
    """External origin (scheme://host) generated links should carry, or ''.

    Precedence: the ``public_base_url`` trait (always wins - explicit admin
    override), else the value the CLI wrote to
    ``~/.config/jupyterlab-share-files/config.json`` - the latter only while
    the tunnel toggle (``tunnel_active``) is on; switched off, links revert
    to the private/request address. Only scheme + host are kept - the base
    path stays auto-detected from the server's own ``base_url``.
    """
    cfg: ShareFilesConfig = handler.settings.get("share_files_config")
    raw = (cfg.public_base_url if cfg is not None else "") or ""
    if raw:
        return _parse_origin(raw)
    if not _tunnel_active():
        return ""
    return _parse_origin(_config_file_state().get("public_base_url") or "")


def _public_origin(handler: tornado.web.RequestHandler) -> str:
    """Pick scheme + host the browser actually sees.

    A configured external origin (Cloudflare tunnel hostname set up via
    ``cloudflare --setup``, or the ``public_base_url`` trait) wins. Otherwise,
    behind a TLS-terminating proxy (JupyterHub, Traefik, nginx) Tornado's
    ``request.protocol`` reports ``http`` unless ``trust_xheaders`` is enabled
    on the server. Honour ``X-Forwarded-Proto`` / ``X-Forwarded-Host``
    explicitly so HTTPS-facing sessions emit HTTPS links, while plain p2p
    sessions stay on HTTP.
    """
    configured = _configured_public_origin(handler)
    if configured:
        return configured
    return _request_origin(handler)


def _request_origin(handler: tornado.web.RequestHandler) -> str:
    """The scheme + host the browser reached this server on, from the
    forwarded headers a TLS-terminating proxy sets, else the request itself."""
    proto = handler.request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    if not proto:
        proto = handler.request.protocol
    host = handler.request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    if not host:
        host = handler.request.host
    return proto + "://" + host


def _own_link_prefixes(handler: tornado.web.RequestHandler) -> set[str]:
    """All `scheme://host/base_url/` prefixes that mean "this server".

    Used by self-connect detection. The configured external origin (Cloudflare
    tunnel) is also "us" - without it, pasting one's own Cloudflare link would
    create a loop connection.
    """
    own_base_url = handler.settings.get("base_url", "/")
    if not own_base_url.endswith("/"):
        own_base_url += "/"
    prefixes = {
        handler.request.protocol + "://" + handler.request.host + own_base_url
    }
    # Raw (toggle-independent): one's own Cloudflare link stays "us" even
    # while the tunnel is switched off.
    configured = _raw_public_origin(handler)
    if configured:
        prefixes.add(configured + own_base_url)
    return prefixes


def _public_share_url(handler: tornado.web.RequestHandler, id_: str) -> str:
    """Build an absolute URL the share's public page is reachable at."""
    path = url_path_join(handler.settings.get("base_url", "/"), EXTENSION_NAMESPACE, "public", "share", id_)
    return _public_origin(handler) + path


def _public_request_url(handler: tornado.web.RequestHandler, id_: str) -> str:
    path = url_path_join(handler.settings.get("base_url", "/"), EXTENSION_NAMESPACE, "public", "request", id_)
    return _public_origin(handler) + path


def _strip_owner_fields(manifest: dict) -> dict:
    """Remove owner-only filesystem metadata before a manifest crosses the
    public boundary. The stores stamp `path` (the owner's workspace-relative
    path) and `mtime` on entries for the authenticated owner panel; a public
    recipient must never see the owner's on-disk layout or file timestamps.
    Mutates and returns the freshly built, per-request manifest in place."""
    manifest.pop("path", None)
    for entry in manifest.get("entries", []):
        entry.pop("path", None)
        entry.pop("mtime", None)
    for uploader in manifest.get("uploaders", []):
        for entry in uploader.get("entries", []):
            entry.pop("path", None)
            entry.pop("mtime", None)
    return manifest


# --------------------------------------------------------------------------- #
# Password protection: rate-limited unlock + capability token
# --------------------------------------------------------------------------- #

# In-memory rate limiter (the `limits` library) guarding the public unlock
# endpoint against brute force. Per-resource keying (share/request id) - one
# protected resource being hammered cannot lock out a different one. Storage is
# process-local; on a multi-process server each worker keeps its own counters,
# which only makes the limit more lenient, never less safe.
from limits import RateLimitItemPerMinute, RateLimitItemPerSecond  # noqa: E402
from limits import storage as _limits_storage  # noqa: E402
from limits import strategies as _limits_strategies  # noqa: E402

_RATE_STORAGE = _limits_storage.MemoryStorage()
_RATE_LIMITER = _limits_strategies.MovingWindowRateLimiter(_RATE_STORAGE)
# Token-of-the-day style secret: random per process. Unlock tokens are also
# bound to the password value, so a restart simply asks recipients to re-enter
# the password (tokens expire); no persistence needed.
_TOKEN_TTL_SECONDS = 6 * 3600


def _rate_limit_ok(handler, kind: str, id_: str) -> bool:
    """Charge one password attempt against the per-resource limits.

    Returns False (and the caller should 429) when either the per-attempt
    cooldown or the per-minute cap is exceeded. Defaults are generous; both
    are tunable via ShareFilesConfig.
    """
    cfg: ShareFilesConfig = handler.settings.get("share_files_config")
    per_min = getattr(cfg, "password_max_attempts_per_minute", 30) or 30
    cooldown = getattr(cfg, "password_attempt_cooldown_seconds", 1) or 0
    key = f"pw:{kind}:{id_}"
    burst = RateLimitItemPerMinute(per_min)
    if not _RATE_LIMITER.test(burst, key):
        return False
    if cooldown > 0:
        gap = RateLimitItemPerSecond(1, cooldown)
        if not _RATE_LIMITER.test(gap, key):
            return False
        _RATE_LIMITER.hit(gap, key)
    _RATE_LIMITER.hit(burst, key)
    return True


def _make_unlock_token(id_: str, password: str) -> str:
    """Signed capability token for a successfully-unlocked resource.

    HMAC-keyed on the password itself, so changing the password invalidates
    every outstanding token automatically. Format: ``<expiry>.<hex-sig>``.
    """
    exp = int(time.time()) + _TOKEN_TTL_SECONDS
    msg = f"{id_}.{exp}".encode("utf-8")
    sig = hmac.new(password.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _check_unlock_token(id_: str, password: str, token: str) -> bool:
    """Validate an unlock token against the current password (constant-time)."""
    if not token or not password:
        return False
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < int(time.time()):
        return False
    msg = f"{id_}.{exp}".encode("utf-8")
    expected = hmac.new(password.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _password_gate(handler, store, id_: str) -> bool:
    """Allow a public request through, or write 401 and return False.

    No password set on the resource -> always allowed. Otherwise a valid
    unlock token (header ``X-Share-Token`` or ``?t=``) is required.
    """
    password = store.get_password(id_)
    if not password:
        return True
    token = handler.request.headers.get("X-Share-Token", "")
    if not token:
        token = handler.get_query_argument("t", default="")
    if _check_unlock_token(id_, password, token):
        return True
    handler.set_status(401)
    handler.set_header("Content-Type", "application/json")
    handler.finish(json.dumps({"error": "password required", "password_required": True}))
    return False


# --------------------------------------------------------------------------- #
# Authenticated API handlers
# --------------------------------------------------------------------------- #


class InfoHandler(_Base):
    """Return basic info about the extension so the UI can display where
    shares and requests live on disk."""

    @tornado.web.authenticated
    def get(self):
        workspace = Path(self.workspace_root).resolve()
        storage = resolve_shares_dir(self.workspace_root, self.shares_dir)
        # Express storage path relative to the workspace root when possible,
        # so the frontend can show something like "./uploads"
        try:
            rel = storage.relative_to(workspace)
            display_path = "./" + str(rel).replace(os.sep, "/")
        except ValueError:
            display_path = str(storage)
        self.write_json({
            "storage_path": display_path,
            "shares_subdir": "shares",
            "requests_subdir": "requests",
            # '' while the tunnel toggle is off - links are private then
            "public_base_url": _configured_public_origin(self),
            **_tunnel_state(self),
        })


def _tunnel_state(handler: tornado.web.RequestHandler) -> dict:
    """Tunnel state for the frontend: configured / active / running."""
    from .tunnel import tunnel_state as _lib_tunnel_state

    state = _lib_tunnel_state()
    # handler-aware: the public_base_url trait also counts as configured
    state["tunnel_configured"] = bool(_raw_public_origin(handler))
    return state


class TunnelHandler(_Base):
    """Cloudflare tunnel toggle (api/tunnel): GET state, POST switch.

    POST ``{"active": true}`` switches to public links (daemon started,
    ``public_base_url`` applied to generated links); ``{"active": false}``
    stops the daemon and reverts links to the private/request address.
    POST ``{"autostart": bool}`` persists whether the server brings the
    tunnel up at startup.
    """

    @tornado.web.authenticated
    def get(self):
        self.write_json(_tunnel_state(self))

    @tornado.web.authenticated
    def post(self):
        from .tunnel import (
            _load_config,
            set_tunnel_autostart,
            tunnel_start,
            tunnel_stop,
        )

        body = self.get_json_body() or {}
        if not _load_config().get("cloudflare_tunnel_token"):
            return self.write_error_json(
                400, "Cloudflare sharing is not configured (run cloudflare setup)"
            )
        if "autostart" in body:
            set_tunnel_autostart(bool(body["autostart"]))
        if body.get("active") is True:
            share_config: ShareFilesConfig = self.settings.get("share_files_config")
            retries = share_config.cloudflared_retries if share_config else 3
            try:
                tunnel_start(retries)
            except RuntimeError as exc:
                return self.write_error_json(400, str(exc))
        elif body.get("active") is False:
            tunnel_stop()
        self.write_json(_tunnel_state(self))


class LinkCheckHandler(_Base):
    """Probe a generated public link server-side (api/link-check).

    The link dialog asks "is this link actually reachable?" - a frontend
    fetch would be blocked by CORS, so the server fetches its own public
    link (through the Cloudflare edge when configured) and reports the
    outcome. Only kind+id are accepted; the URL is rebuilt server-side,
    never taken from the client.
    """

    @tornado.web.authenticated
    async def get(self):
        kind = self.get_query_argument("kind", "")
        id_ = self.get_query_argument("id", "")
        if not re.fullmatch(r"[A-Z2-7]{6,16}", id_):
            return self.write_error_json(400, "invalid id")
        if kind == "share":
            link = _public_share_url(self, id_)
        elif kind == "request":
            link = _public_request_url(self, id_)
        else:
            return self.write_error_json(400, "kind must be 'share' or 'request'")
        # The URL is OUR OWN (rebuilt from kind+id above) - the question is
        # "does it answer", not "is the certificate trusted", so a
        # self-signed hub certificate must not fail the probe.
        client = tornado.httpclient.AsyncHTTPClient()
        try:
            resp = await client.fetch(
                link, raise_error=False, validate_cert=False, request_timeout=10
            )
            self.write_json({
                "link": link,
                "reachable": resp.code == 200,
                "status": resp.code,
            })
        except (ssl.SSLError, OSError, ConnectionError) as exc:
            self.write_json({"link": link, "reachable": False, "error": str(exc)})


class TunnelSetupHandler(_Base):
    """Provision Cloudflare sharing from the panel (api/tunnel/setup).

    Same inputs and sequence as ``cloudflare setup``: token, account id,
    hostname (the public share host) and the mandatory https
    private_base_url. The blocking Cloudflare API calls run in an executor
    so the event loop stays responsive.
    """

    @tornado.web.authenticated
    async def post(self):
        from .tunnel import setup_and_start

        body = self.get_json_body() or {}
        token = str(body.get("token") or "")
        account_id = str(body.get("account_id") or "")
        hostname = str(body.get("hostname") or "")
        private_base_url = str(body.get("private_base_url") or "")
        if not hostname or not private_base_url:
            return self.write_error_json(
                400, "hostname and private_base_url are required"
            )
        try:
            result = await tornado.ioloop.IOLoop.current().run_in_executor(
                None,
                setup_and_start,
                token,
                account_id,
                hostname,
                private_base_url,
            )
        except RuntimeError as exc:
            return self.write_error_json(400, str(exc))
        self.write_json({**result, **_tunnel_state(self)})


class TunnelResetHandler(_Base):
    """Reset Cloudflare sharing from the panel (api/tunnel/reset).

    Same as ``cloudflare reset``: credentials, tunnel state and the
    private/public base URLs are cleared; links revert to the private
    address on the next request; Cloudflare-side resources are kept.
    """

    @tornado.web.authenticated
    def post(self):
        from .tunnel import reset_config

        result = reset_config()
        self.write_json({**result, **_tunnel_state(self)})


class PasswordHandler(_Base):
    """Owner-side password management (api/<shares|requests>/<id>/password).

    GET returns the stored plaintext (owner-only - the link dialog shows it
    with a copy button); POST sets/changes it, an empty value clears it.
    """

    def _store(self, kind: str):
        return self.share_store if kind == "shares" else self.request_store

    @tornado.web.authenticated
    def get(self, kind, id_):
        store = self._store(kind)
        if not store.exists(id_):
            return self.write_error_json(404, "not found")
        self.write_json({"id": id_, "password": store.get_password(id_)})

    @tornado.web.authenticated
    def post(self, kind, id_):
        body = self.get_json_body() or {}
        password = str(body.get("password") or "")
        store = self._store(kind)
        try:
            manifest = store.set_password(id_, password)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        manifest["password"] = password
        self.write_json(manifest)


class GeneratePasswordHandler(_Base):
    """xkcd-style passphrase generation (api/generate-password)."""

    @tornado.web.authenticated
    def get(self):
        try:
            self.write_json({"password": generate_password()})
        except RuntimeError as exc:
            self.write_error_json(500, str(exc))


class PublicUnlockHandler(_PublicBase):
    """Password attempt for a protected resource (public/<kind>/<id>/unlock).

    Rate limited per resource id - a generous per-minute cap plus a
    per-attempt cooldown (both tunable via ShareFilesConfig). A correct
    password returns a signed unlock token the standalone page and peers
    send back via the ``X-Share-Token`` header (or ``?t=`` for plain
    download links). Wrong password and rate-limit both burn one attempt.
    """

    def post(self, kind, id_):
        store = self.share_store if kind == "share" else self.request_store
        password = store.get_password(id_)
        if not store.exists(id_):
            self.set_status(404)
            self.finish(json.dumps({"error": "not found"}))
            return
        if not password:
            self.finish(json.dumps({"unlocked": True, "token": ""}))
            return
        if not _rate_limit_ok(self, kind, id_):
            self.set_status(429)
            self.finish(json.dumps({
                "error": "too many attempts - wait before retrying",
            }))
            return
        try:
            body = json.loads(self.request.body or b"{}")
        except ValueError:
            body = {}
        attempt = str(body.get("password") or "")
        if verify_password(password, attempt):
            self.finish(json.dumps({
                "unlocked": True,
                "token": _make_unlock_token(id_, password),
            }))
        else:
            self.set_status(401)
            self.finish(json.dumps({"error": "wrong password"}))


class SharesListHandler(_Base):
    @tornado.web.authenticated
    def get(self):
        items = self.share_store.list()
        for item in items:
            item["link"] = _public_share_url(self, item["id"])
        self.write_json({"shares": items})

    @tornado.web.authenticated
    def post(self):
        body = self.get_json_body() or {}
        name = (body.get("name") or "").strip()
        paths = body.get("paths") or []
        password = str(body.get("password") or "")
        if not name:
            return self.write_error_json(400, "Missing 'name'")
        if not isinstance(paths, list):
            return self.write_error_json(400, "'paths' must be a list")
        try:
            manifest = self.share_store.create(name, paths, password=password)
        except (StorageError, NotFoundError) as exc:
            return self.write_error_json(400, str(exc))
        manifest["link"] = _public_share_url(self, manifest["id"])
        self.write_json(manifest)


class ShareItemHandler(_Base):
    @tornado.web.authenticated
    def get(self, id_):
        try:
            manifest = self.share_store.get(id_)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        manifest["link"] = _public_share_url(self, id_)
        self.write_json(manifest)

    @tornado.web.authenticated
    def delete(self, id_):
        try:
            self.share_store.delete(id_)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        self.write_json({"ok": True})


class ShareItemsHandler(_Base):
    @tornado.web.authenticated
    def post(self, id_):
        body = self.get_json_body() or {}
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths:
            return self.write_error_json(400, "'paths' must be a non-empty list")
        try:
            manifest = self.share_store.add_items(id_, paths)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        except StorageError as exc:
            return self.write_error_json(400, str(exc))
        manifest["link"] = _public_share_url(self, id_)
        self.write_json(manifest)

    @tornado.web.authenticated
    def delete(self, id_):
        """Remove items from a share.

        Names come via query parameters (`?name=foo&name=bar`) rather than
        request body - browsers' fetch() does send DELETE bodies, but the
        downstream Jupyter ServerConnection layer can drop them, and many
        proxies strip them too. Query params are universally reliable.
        """
        names = [n for n in self.get_arguments("name") if n]
        if not names:
            return self.write_error_json(400, "Pass at least one ?name=...")
        try:
            manifest = self.share_store.remove_items(id_, names)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        except StorageError as exc:
            return self.write_error_json(400, str(exc))
        manifest["link"] = _public_share_url(self, id_)
        self.write_json(manifest)


class RequestsListHandler(_Base):
    @tornado.web.authenticated
    def get(self):
        items = []
        for m in self.request_store.list():
            full = self.request_store.get(m["id"])
            full["link"] = _public_request_url(self, m["id"])
            items.append(full)
        self.write_json({"requests": items})

    @tornado.web.authenticated
    def post(self):
        body = self.get_json_body() or {}
        name = (body.get("name") or "").strip()
        password = str(body.get("password") or "")
        if not name:
            return self.write_error_json(400, "Missing 'name'")
        manifest = self.request_store.create(name, password=password)
        manifest["link"] = _public_request_url(self, manifest["id"])
        manifest["uploaders"] = []
        self.write_json(manifest)


class RequestItemHandler(_Base):
    @tornado.web.authenticated
    def get(self, id_):
        try:
            manifest = self.request_store.get(id_)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        manifest["link"] = _public_request_url(self, id_)
        self.write_json(manifest)

    @tornado.web.authenticated
    def delete(self, id_):
        try:
            self.request_store.delete(id_)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        self.write_json({"ok": True})


class RequestUploadsHandler(_Base):
    @tornado.web.authenticated
    def delete(self, id_):
        """Remove an upload. Pass uploader and name as query params, not body."""
        uploader = self.get_argument("uploader", default="")
        name = self.get_argument("name", default="")
        if not uploader or not name:
            return self.write_error_json(400, "Missing ?uploader=... or ?name=...")
        try:
            manifest = self.request_store.remove_upload(id_, uploader, name)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        except StorageError as exc:
            return self.write_error_json(400, str(exc))
        manifest["link"] = _public_request_url(self, id_)
        self.write_json(manifest)


class RequestSeenHandler(_Base):
    @tornado.web.authenticated
    def post(self, id_):
        try:
            self.request_store.mark_seen(id_)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        self.write_json({"ok": True})


class ConnectionsHandler(_Base):
    @tornado.web.authenticated
    def get(self):
        self.write_json({"connections": self.connection_store.list()})

    @tornado.web.authenticated
    async def post(self):
        """Add a connection.

        Body: { "link": "https://host/.../public/share/<id>",
                "password": "..." (optional) }
        We parse the link, probe whether the resource is password protected,
        verify a provided password against the peer's unlock endpoint, and
        persist a connection entry. A protected resource without (or with a
        wrong) password answers 401 `password_required` so the panel can
        prompt and retry.
        """
        body = self.get_json_body() or {}
        link = (body.get("link") or "").strip()
        password = str(body.get("password") or "")
        if not link:
            return self.write_error_json(400, "Missing 'link'")
        try:
            parsed = _parse_share_link(link)
        except ValueError as exc:
            return self.write_error_json(400, str(exc))
        # Refuse to connect to ourselves - that would create a loop where
        # any save/upload routes back through the same server. On
        # JupyterHub two users share a host but live at different
        # `/user/<name>/` prefixes, so compare the full prefix (host +
        # base_url) not just the host.
        link_prefix = parsed["host"] + parsed.get("base_path", "/")
        if link_prefix in _own_link_prefixes(self):
            return self.write_error_json(
                400,
                "That link points to your own server - it's already in your panel.",
            )
        # Probe the peer: protected resources answer 401 on the bare manifest.
        # With a password given, verify it via the peer's unlock endpoint so a
        # wrong password is caught at connect time, not at first download.
        try:
            probe = await self._peer_fetch(link.rstrip("/") + "/manifest")
            if probe.code == 401:
                if not password:
                    self.set_status(401)
                    return self.write_json({
                        "error": "password required",
                        "password_required": True,
                    })
                unlock = await self._peer_fetch(
                    link.rstrip("/") + "/unlock",
                    method="POST",
                    body=json.dumps({"password": password}),
                    headers={"Content-Type": "application/json"},
                )
                if unlock.code == 429:
                    return self.write_error_json(
                        429, "too many password attempts - wait before retrying"
                    )
                if unlock.code != 200:
                    self.set_status(401)
                    return self.write_json({
                        "error": "wrong password",
                        "password_required": True,
                    })
        except PeerUnavailable as exc:
            return self.write_error_json(502, str(exc))
        entry = self.connection_store.add(
            kind=parsed["kind"],
            id_=parsed["id"],
            host=parsed["host"],
            name=parsed.get("name", ""),
            owner=parsed.get("owner", ""),
            link=link,
            password=password,
        )
        # Ensure the response carries the link even if the entry already existed
        # without one (older persisted connections); the store backfills on disk.
        if not entry.get("link"):
            entry["link"] = link
        self.write_json(entry)


class ConnectionItemHandler(_Base):
    @tornado.web.authenticated
    def delete(self, key):
        self.connection_store.remove(key)
        self.write_json({"ok": True})


def _resolve_workspace_target_dir(workspace_root: str, target_dir: str, shares_dir_setting: str = "") -> Path:
    """Resolve target_dir relative to workspace, reject paths inside the shares dir.

    Accepts symlinked subdirectories - we only validate the user-supplied path
    syntactically (rejecting `..` and absolute paths) and check that the final
    location isn't the shares directory itself.
    """
    root = Path(workspace_root)
    if target_dir in ("", "."):
        resolved = root
    else:
        if not _is_safe_relative(target_dir):
            raise StorageError(f"Unsafe target_dir: {target_dir}")
        resolved = root / target_dir
    forbidden = resolve_shares_dir(workspace_root, shares_dir_setting)
    # only block if resolved actually equals the shares dir literally;
    # symlinked subtrees are fine
    try:
        if resolved.resolve() == forbidden.resolve():
            raise StorageError("Cannot save into the shares directory")
    except (OSError, ValueError):
        pass
    return resolved


def _extract_zip_into(zip_bytes: bytes, dest_dir: Path) -> int:
    """Extract a zip archive into dest_dir, return file count.

    Strips path components that escape the destination.
    """
    count = 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p and p not in (".", "..")]
            if not parts:
                continue
            target = dest_dir.joinpath(*parts).resolve()
            if not target.is_relative_to(dest_dir.resolve()):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
            count += 1
    return count


class ConnectionSaveHandler(_Base):
    """Download items from a connected share into the user's workspace."""

    @tornado.web.authenticated
    async def post(self, key):
        try:
            conn = self.connection_store.get(key)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        if conn.get("kind") != "share":
            return self.write_error_json(400, "Connection is not a share")
        body = self.get_json_body() or {}
        target_dir = body.get("target_dir") or ""
        names = body.get("names")  # None or list[str] - None means "all"
        try:
            dest_root = _resolve_workspace_target_dir(self.workspace_root, target_dir, self.shares_dir)
        except StorageError as exc:
            return self.write_error_json(400, str(exc))

        host = conn["host"]
        share_id = conn["id"]
        # Prefer the persisted full link - it carries the owner's
        # `/user/<name>/` prefix on JupyterHub. Reconstructing from our own
        # base_url points at OUR server (404). Fall back to reconstruction only
        # for legacy link-less connections.
        link = (conn.get("link") or "").rstrip("/")
        if link:
            api_base = link
        else:
            base_url = self.settings.get("base_url", "/")
            api_base = host + url_path_join(base_url, EXTENSION_NAMESPACE, "public", "share", share_id)

        saved: list[str] = []
        # Peer fetches can fail on TLS (self-signed) or connection - map those
        # to a clean 502 rather than an unhandled 500.
        try:
            # Protected peer? trade the stored password for an unlock token
            auth_headers = await self._peer_auth_headers(conn)
            # Resolve share name for the wrapping folder when saving all
            manifest_resp = await self._peer_fetch(api_base + "/manifest", headers=auth_headers)
            if manifest_resp.code != 200:
                return self.write_error_json(502, f"Remote unavailable ({manifest_resp.code})")
            manifest = json.loads(manifest_resp.body)
            share_slug = manifest.get("slug") or share_id

            if names is None:
                # Save All - download zip, extract into <dest_root>/<share-slug>/
                zip_resp = await self._peer_fetch(api_base + "/download-all", headers=auth_headers)
                if zip_resp.code != 200:
                    return self.write_error_json(502, f"Could not download share ({zip_resp.code})")
                wrap_dir = _resolve_unique_target(dest_root, share_slug)
                _extract_zip_into(zip_resp.body, wrap_dir)
                saved.append(str(wrap_dir.relative_to(Path(self.workspace_root).resolve())))
            else:
                if not isinstance(names, list) or not names:
                    return self.write_error_json(400, "'names' must be a non-empty list")
                # determine which entries are directories vs files
                entry_map = {e["name"]: e for e in manifest.get("entries", [])}
                for name in names:
                    if not name or "/" in name or "\\" in name or name in (".", ".."):
                        return self.write_error_json(400, f"Invalid name: {name}")
                    entry = entry_map.get(name)
                    if entry is None:
                        return self.write_error_json(404, f"Not in share: {name}")
                    url = api_base + "/download/" + tornado.escape.url_escape(name)
                    resp = await self._peer_fetch(url, headers=auth_headers)
                    if resp.code != 200:
                        return self.write_error_json(502, f"Could not download {name} ({resp.code})")
                    if entry.get("type") == "directory":
                        wrap_dir = _resolve_unique_target(dest_root, name)
                        _extract_zip_into(resp.body, wrap_dir)
                        saved.append(str(wrap_dir.relative_to(Path(self.workspace_root).resolve())))
                    else:
                        target = _resolve_unique_target(dest_root, name)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with open(target, "wb") as f:
                            f.write(resp.body)
                        saved.append(str(target.relative_to(Path(self.workspace_root).resolve())))
        except PeerUnavailable as exc:
            return self.write_error_json(502, str(exc))

        self.write_json({"ok": True, "saved": saved})


class ConnectionUploadHandler(_Base):
    """Upload items from the user's workspace to a connected request."""

    @tornado.web.authenticated
    async def post(self, key):
        try:
            conn = self.connection_store.get(key)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        if conn.get("kind") != "request":
            return self.write_error_json(400, "Connection is not a request")
        body = self.get_json_body() or {}
        paths = body.get("paths") or []
        uploader = (body.get("uploader") or "anonymous").strip() or "anonymous"
        if not isinstance(paths, list) or not paths:
            return self.write_error_json(400, "'paths' must be a non-empty list")

        host = conn["host"]
        request_id = conn["id"]
        # Prefer the persisted full link (carries the owner's /user/<name>/
        # prefix on JupyterHub); reconstruction points at our own server.
        link = (conn.get("link") or "").rstrip("/")
        if link:
            upload_url = link + "/upload"
        else:
            base_url = self.settings.get("base_url", "/")
            upload_url = host + url_path_join(
                base_url, EXTENSION_NAMESPACE, "public", "request", request_id, "upload"
            )
        upload_url += "?uploader=" + tornado.escape.url_escape(uploader)

        client = tornado.httpclient.AsyncHTTPClient()
        ws_root = Path(self.workspace_root).resolve()
        sent: list[str] = []

        try:
            # Protected peer? trade the stored password for an unlock token
            auth_headers = await self._peer_auth_headers(conn)
            # Server-to-server uploads have no browser cookie jar. Replay the
            # connection's persisted uploader identity as a Cookie header so
            # every upload (and every file of a folder batch) lands in ONE
            # pool on the peer; the hash the peer mints on the first upload
            # is captured from the response and persisted for next time.
            uploader_hash = conn.get("uploader_hash") or ""

            async def post_one(filename: str, data: bytes) -> None:
                nonlocal uploader_hash
                headers = dict(auth_headers or {})
                if uploader_hash:
                    headers["Cookie"] = (
                        f"sf_uploader_{request_id}={uploader_hash}"
                    )
                resp_body = await _post_file(
                    client,
                    upload_url,
                    filename,
                    data,
                    validate_cert=self.verify_peer_tls,
                    headers=headers,
                )
                if not uploader_hash:
                    try:
                        minted = (json.loads(resp_body) or {}).get("me", {}).get("hash", "")
                    except (ValueError, AttributeError):
                        minted = ""
                    if minted:
                        uploader_hash = minted
                        self.connection_store.set_uploader_hash(key, minted)

            for rel in paths:
                if not _is_safe_relative(rel):
                    return self.write_error_json(400, f"Unsafe path: {rel}")
                src = ws_root / rel
                if not src.exists():
                    return self.write_error_json(404, f"Not found: {rel}")
                if src.is_dir():
                    # walk and upload each file with the folder structure preserved in filename
                    for root, _dirs, files in os.walk(src):
                        for fname in files:
                            abs_path = os.path.join(root, fname)
                            rel_name = os.path.join(src.name, os.path.relpath(abs_path, src))
                            with open(abs_path, "rb") as f:
                                data = f.read()
                            await post_one(rel_name.replace(os.sep, "/"), data)
                            sent.append(rel_name)
                else:
                    with open(src, "rb") as f:
                        data = f.read()
                    await post_one(src.name, data)
                    sent.append(src.name)
        except PeerUnavailable as exc:
            return self.write_error_json(502, str(exc))

        self.write_json({"ok": True, "uploaded": sent})


async def _post_file(
    client,
    url: str,
    filename: str,
    data: bytes,
    validate_cert: bool = True,
    headers: dict | None = None,
) -> bytes:
    """Send a single file as multipart/form-data POST. Returns the response body."""
    boundary = "----shareFilesBoundary" + os.urandom(8).hex()
    body_parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(body_parts)
    try:
        resp = await client.fetch(
            url,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                **(headers or {}),
            },
            body=body,
            raise_error=False,
            validate_cert=validate_cert,
        )
    except ssl.SSLError as exc:
        raise PeerUnavailable(
            f"TLS verification failed for the peer ({exc}). If the peer uses a "
            "self-signed certificate, set c.ShareFilesConfig.verify_peer_tls = "
            "False in jupyter_server_config.py."
        ) from None
    except (OSError, ConnectionError) as exc:
        raise PeerUnavailable(f"Could not reach the peer: {exc}") from None
    if resp.code >= 400:
        raise PeerUnavailable(f"Upload failed: {resp.code}")
    return resp.body or b""


def _parse_share_link(link: str) -> dict[str, str]:
    """Extract kind/id/host/base_path from a public link URL.

    `base_path` is everything between the host and the extension namespace -
    on JupyterHub that is `/user/<name>/`, on a standalone Jupyter server
    it is `/`. Without it, two users on the same hub look identical when
    we compare hosts for self-connect detection.
    """
    parsed = urlparse(link)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Link must be an absolute URL")
    parts = [p for p in parsed.path.split("/") if p]
    # find marker 'public'
    try:
        idx = parts.index("public")
    except ValueError as exc:
        raise ValueError("Link is not a share/request URL") from exc
    if idx + 2 >= len(parts):
        raise ValueError("Link is malformed")
    kind = parts[idx + 1]
    id_ = parts[idx + 2]
    if kind not in ("share", "request"):
        raise ValueError(f"Unknown kind: {kind}")
    # The path part before EXTENSION_NAMESPACE is the JupyterLab base_url.
    try:
        ns_idx = parts.index(EXTENSION_NAMESPACE)
    except ValueError:
        ns_idx = idx  # legacy/malformed - fall back to before 'public'
    base_path = "/" + "/".join(parts[:ns_idx])
    if not base_path.endswith("/"):
        base_path += "/"
    return {
        "kind": kind,
        "id": id_,
        "host": parsed.scheme + "://" + parsed.netloc,
        "base_path": base_path,
    }


# --------------------------------------------------------------------------- #
# Public (unauthenticated) endpoints
# --------------------------------------------------------------------------- #


class _UncachedPublicMixin:
    """Serve the public pages and manifests uncached.

    Applied to the two manifest endpoints and the two recipient-page
    endpoints. Three concrete reasons, all about a *stored* response being
    reused:

    1. Privacy. `PublicRequestManifestHandler` varies its body by cookie - it
       filters `uploaders` down to the caller's own pool. It carried an ETag,
       no `Cache-Control` and no `Vary: Cookie`, so any shared cache keyed on
       URL alone (a Cloudflare cache rule, a corporate proxy) could hand one
       uploader's file list to another. `no-store` closes that.
    2. Staleness. A manifest changes whenever a file is added or removed, and
       with no `Cache-Control` a browser is free to reuse a stored copy under
       heuristic freshness - showing a file list that silently omits the file
       the recipient was told to download.
    3. A stale gate. The recipient page bakes `password_required` into its
       HTML at render time; a cached page from before the owner set a password
       skips the prompt, so the manifest fetch 401s. (The page also recovers
       from that on its own now - see `loadShare` in `static/standalone.html`.)

    Not a reason: a `304` reaching JavaScript. A browser consumes the 304 its
    own cache solicited and resolves the fetch with the stored 200; only a
    request that sets `If-None-Match` itself sees a 304, and no client here
    does. Suppressing the ETag simply keeps this endpoint out of caches
    entirely, which is what both reasons above require.

    `no-store` forbids storing; `no-cache` and `max-age=0` are belt-and-braces
    for intermediaries that predate or ignore it (a Cloudflare tunnel sits in
    this path for public links). Do not reduce this to `must-revalidate`,
    which alone still permits serving a heuristically-fresh response without
    revalidating - reason 2 above.
    """

    def compute_etag(self):
        return None

    def set_default_headers(self):
        # Runs via RequestHandler.clear() on success AND error paths, so a 401
        # from the password gate or a 404 is uncacheable too - a cached 401
        # must not be replayed after the recipient unlocks.
        super().set_default_headers()
        self.set_header("Cache-Control", "no-store, no-cache, max-age=0")


class PublicSharePageHandler(_UncachedPublicMixin, _PublicBase):
    def get(self, id_):
        if not self.share_store.exists(id_):
            self.set_status(404)
            self.set_header("Content-Type", "text/html")
            self.finish("<h1>Share not found</h1>")
            return
        template_path = os.path.join(os.path.dirname(__file__), "static", "standalone.html")
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read()
        # the standalone JS reads kind/id from window.__SHARE_FILES_CONTEXT__
        base_url = self.settings.get("base_url", "/")
        ctx = json.dumps({
            "kind": "share",
            "id": id_,
            "api_base": url_path_join(base_url, EXTENSION_NAMESPACE, "public"),
            "password_required": bool(self.share_store.get_password(id_)),
        })
        html = html.replace("__CONTEXT__", ctx)
        self.set_header("Content-Type", "text/html")
        self.finish(html)


class PublicRequestPageHandler(_UncachedPublicMixin, _PublicBase):
    def get(self, id_):
        if not self.request_store.exists(id_):
            self.set_status(404)
            self.set_header("Content-Type", "text/html")
            self.finish("<h1>Request not found</h1>")
            return
        template_path = os.path.join(os.path.dirname(__file__), "static", "standalone.html")
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read()
        base_url = self.settings.get("base_url", "/")
        ctx = json.dumps({
            "kind": "request",
            "id": id_,
            "api_base": url_path_join(base_url, EXTENSION_NAMESPACE, "public"),
            "password_required": bool(self.request_store.get_password(id_)),
        })
        html = html.replace("__CONTEXT__", ctx)
        self.set_header("Content-Type", "text/html")
        self.finish(html)


class PublicShareManifestHandler(_UncachedPublicMixin, _PublicBase):
    def get(self, id_):
        if not _password_gate(self, self.share_store, id_):
            return
        try:
            manifest = self.share_store.get(id_)
        except NotFoundError:
            self.set_status(404)
            self.finish(json.dumps({"error": "not found"}))
            return
        _strip_owner_fields(manifest)
        manifest["link"] = _public_share_url(self, id_)
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(manifest))


class PublicRequestManifestHandler(_UncachedPublicMixin, _PublicBase):
    def get(self, id_):
        if not _password_gate(self, self.request_store, id_):
            return
        try:
            manifest = self.request_store.get(id_)
        except NotFoundError:
            self.set_status(404)
            self.finish(json.dumps({"error": "not found"}))
            return
        manifest["link"] = _public_request_url(self, id_)
        # never expose other people's uploads publicly - only the caller's
        # own pool, identified by the uploader cookie, is returned
        uploader_hash = _uploader_hash_from_cookie(self, id_)
        mine = [
            u for u in manifest.get("uploaders", [])
            if uploader_hash and u.get("hash") == uploader_hash
        ]
        manifest["uploaders"] = mine
        if mine:
            manifest["me"] = {"hash": uploader_hash, "name": mine[0].get("name")}
        _strip_owner_fields(manifest)
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(manifest))


class PublicShareDownloadHandler(_PublicBase):
    def get(self, id_, sub_path):
        if not _password_gate(self, self.share_store, id_):
            return
        try:
            target = self.share_store.resolve_data_path(id_, sub_path)
        except (NotFoundError, StorageError):
            self.set_status(404)
            self.finish("Not found")
            return
        if target.is_dir():
            self._serve_zip(target, target.name)
        else:
            self._serve_file(target)

    def _serve_file(self, path: Path):
        self.set_header("Content-Type", "application/octet-stream")
        self.set_header("Content-Disposition", f'attachment; filename="{path.name}"')
        with open(path, "rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                self.write(chunk)
        self.finish()

    def _serve_zip(self, directory: Path, name: str):
        self.set_header("Content-Type", "application/zip")
        self.set_header("Content-Disposition", f'attachment; filename="{name}.zip"')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(directory):
                for fname in files:
                    abs_path = os.path.join(root, fname)
                    arcname = os.path.join(name, os.path.relpath(abs_path, directory))
                    zf.write(abs_path, arcname)
        self.finish(buf.getvalue())


class PublicShareDownloadAllHandler(_PublicBase):
    def get(self, id_):
        if not _password_gate(self, self.share_store, id_):
            return
        try:
            data_dir = self.share_store.resolve_data_path(id_)
            manifest = self.share_store.get(id_)
        except NotFoundError:
            self.set_status(404)
            self.finish("Not found")
            return
        name = manifest.get("slug") or id_
        self.set_header("Content-Type", "application/zip")
        self.set_header("Content-Disposition", f'attachment; filename="{name}.zip"')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(data_dir):
                for fname in files:
                    abs_path = os.path.join(root, fname)
                    arcname = os.path.relpath(abs_path, data_dir)
                    zf.write(abs_path, arcname)
        self.finish(buf.getvalue())


def _uploader_cookie_name(id_: str) -> str:
    return f"sf_uploader_{id_}"


def _uploader_hash_from_cookie(handler, id_: str) -> str:
    """Read and sanity-check the uploader identity cookie. '' when absent."""
    raw = handler.get_cookie(_uploader_cookie_name(id_), "") or ""
    # hashes are short base32 - reject anything else (forged / corrupted)
    if not re.fullmatch(r"[A-Z2-7]{4,16}", raw):
        return ""
    return raw


class PublicRequestUploadHandler(_PublicBase):
    def post(self, id_):
        if not _password_gate(self, self.request_store, id_):
            return
        if not self.request_store.exists(id_):
            self.set_status(404)
            self.finish(json.dumps({"error": "not found"}))
            return
        uploader_name = self.get_argument("uploader", default="anonymous")
        # identity comes from the cookie, never from the client's parameters;
        # first upload mints a hash and sets the cookie on the response
        uploader_hash = _uploader_hash_from_cookie(self, id_)
        if not uploader_hash:
            uploader_hash = mint_uploader_hash()
            self.set_cookie(
                _uploader_cookie_name(id_),
                uploader_hash,
                httponly=True,
                expires_days=365,
                samesite="Lax",
            )
        files = self.request.files.get("file") or self.request.files.get("files") or []
        if not files:
            self.set_status(400)
            self.finish(json.dumps({"error": "no file"}))
            return
        for f in files:
            filename = f.get("filename") or "upload.bin"
            data = f.get("body") or b""
            try:
                self.request_store.add_upload(
                    id_, uploader_hash, uploader_name, filename, data
                )
            except StorageError as exc:
                self.set_status(400)
                self.finish(json.dumps({"error": str(exc)}))
                return
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps({
            "ok": True,
            "count": len(files),
            "me": {"hash": uploader_hash, "name": uploader_name},
        }))

    def delete(self, id_):
        """Remove one of the caller's own uploads - identity from the cookie only."""
        if not _password_gate(self, self.request_store, id_):
            return
        uploader_hash = _uploader_hash_from_cookie(self, id_)
        if not uploader_hash:
            self.set_status(403)
            self.finish(json.dumps({"error": "no uploader identity"}))
            return
        name = self.get_argument("name", default="")
        if not name:
            self.set_status(400)
            self.finish(json.dumps({"error": "Missing ?name=..."}))
            return
        try:
            self.request_store.remove_upload(id_, uploader_hash, name)
        except NotFoundError:
            self.set_status(404)
            self.finish(json.dumps({"error": "not found"}))
            return
        except StorageError as exc:
            self.set_status(400)
            self.finish(json.dumps({"error": str(exc)}))
            return
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps({"ok": True}))


# --------------------------------------------------------------------------- #
# Route registration
# --------------------------------------------------------------------------- #


def setup_route_handlers(web_app, config: ShareFilesConfig | None = None):
    host_pattern = ".*$"
    base_url = web_app.settings["base_url"]
    ns = EXTENSION_NAMESPACE
    # store config in settings so handlers can read it
    web_app.settings["share_files_config"] = config or ShareFilesConfig()

    if hub_mode():
        # Hub-managed lab: the panel works through the hub fileshare API and
        # nothing unauthenticated is mounted - no public/*, no static/*.
        from .hub_routes import hub_handlers

        web_app.add_handlers(host_pattern, hub_handlers(base_url, ns))
        return

    static_path = os.path.join(os.path.dirname(__file__), "static")

    handlers = [
        # api/info
        (url_path_join(base_url, ns, "api", "info"), InfoHandler),
        # api/link-check
        (url_path_join(base_url, ns, "api", "link-check"), LinkCheckHandler),
        # api/tunnel
        (url_path_join(base_url, ns, "api", "tunnel"), TunnelHandler),
        (url_path_join(base_url, ns, "api", "tunnel", "setup"), TunnelSetupHandler),
        (url_path_join(base_url, ns, "api", "tunnel", "reset"), TunnelResetHandler),
        # api/generate-password
        (url_path_join(base_url, ns, "api", "generate-password"), GeneratePasswordHandler),
        # api/<shares|requests>/<id>/password
        (url_path_join(base_url, ns, "api", r"(shares|requests)", r"([A-Z2-7]{6,16})", "password"), PasswordHandler),
        # api/shares
        (url_path_join(base_url, ns, "api", "shares"), SharesListHandler),
        (url_path_join(base_url, ns, "api", "shares", r"([A-Z2-7]{6,16})"), ShareItemHandler),
        (url_path_join(base_url, ns, "api", "shares", r"([A-Z2-7]{6,16})", "items"), ShareItemsHandler),
        # api/requests
        (url_path_join(base_url, ns, "api", "requests"), RequestsListHandler),
        (url_path_join(base_url, ns, "api", "requests", r"([A-Z2-7]{6,16})"), RequestItemHandler),
        (url_path_join(base_url, ns, "api", "requests", r"([A-Z2-7]{6,16})", "uploads"), RequestUploadsHandler),
        (url_path_join(base_url, ns, "api", "requests", r"([A-Z2-7]{6,16})", "seen"), RequestSeenHandler),
        # api/connections
        (url_path_join(base_url, ns, "api", "connections"), ConnectionsHandler),
        (url_path_join(base_url, ns, "api", "connections", r"([^/]+)", "save"), ConnectionSaveHandler),
        (url_path_join(base_url, ns, "api", "connections", r"([^/]+)", "upload"), ConnectionUploadHandler),
        (url_path_join(base_url, ns, "api", "connections", r"([^/]+)"), ConnectionItemHandler),
        # public unlock (password attempt; rate limited)
        (url_path_join(base_url, ns, "public", r"(share|request)", r"([A-Z2-7]{6,16})", "unlock"), PublicUnlockHandler),
        # public/share
        (url_path_join(base_url, ns, "public", "share", r"([A-Z2-7]{6,16})"), PublicSharePageHandler),
        (url_path_join(base_url, ns, "public", "share", r"([A-Z2-7]{6,16})", "manifest"), PublicShareManifestHandler),
        (url_path_join(base_url, ns, "public", "share", r"([A-Z2-7]{6,16})", "download-all"), PublicShareDownloadAllHandler),
        (url_path_join(base_url, ns, "public", "share", r"([A-Z2-7]{6,16})", "download", r"(.+)"), PublicShareDownloadHandler),
        # public/request
        (url_path_join(base_url, ns, "public", "request", r"([A-Z2-7]{6,16})"), PublicRequestPageHandler),
        (url_path_join(base_url, ns, "public", "request", r"([A-Z2-7]{6,16})", "manifest"), PublicRequestManifestHandler),
        (url_path_join(base_url, ns, "public", "request", r"([A-Z2-7]{6,16})", "upload"), PublicRequestUploadHandler),
        # static assets used by the standalone page
        (url_path_join(base_url, ns, "static", "(.*)"), StaticFileHandler, {"path": static_path}),
    ]

    web_app.add_handlers(host_pattern, handlers)
