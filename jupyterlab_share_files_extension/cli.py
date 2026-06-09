"""Command-line interface for the Share Files JupyterLab extension.

A thin client over the extension's authenticated HTTP API
(``{base}/jupyterlab-share-files-extension/api/*``) - the same endpoints the
JupyterLab panel uses - so link generation, connecting to peers, server-side
save, upload and delete all behave identically. It acts as a single user,
authenticating with that user's Jupyter / JupyterHub token. Configuration
comes from the environment:

- ``SHARE_FILES_BASE_URL`` (preferred) or ``JUPYTER_SERVER_URL`` - the base URL
  of the Jupyter server running the extension. On JupyterHub this MUST be the
  public user URL (e.g. ``https://hub.example.com/user/<name>/``) so that share
  links the server generates carry the public host and ``/user/<name>/`` prefix.
- ``SHARE_FILES_TOKEN`` (preferred), ``JUPYTERHUB_API_TOKEN`` or
  ``JUPYTER_TOKEN`` - the API token used for ``Authorization: token <token>``.
- ``SHARE_FILES_INSECURE`` - set to ``1`` to skip TLS verification (self-signed
  certificates). Off by default.

Additionally provides a ``cloudflare`` command with four orthogonal
subcommands:

- ``setup --token T --account-id A --hostname H --local-base-url U`` - save
  the credentials and provision the tunnel end to end (tunnel + DNS + HTTPS
  enforcement + ``public_base_url`` link rewriting + connector daemon)
- ``validate`` - end-to-end check of the saved config: token validity, bind
  to existing tunnels, create rights (proven by creating a test tunnel and
  removing it)
- ``info`` - current configuration with tokens masked to their last 4
  characters, plus daemon and tunnel status
- ``reset`` - reset the saved token to none (clears account id, tunnel state
  and ``public_base_url``; links revert to the local/hub address)

Run it with the console script ``jupyterlab_share_files``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
NAMESPACE = "jupyterlab-share-files-extension"


# --------------------------------------------------------------------------- #
# Extension API client (environment-configured, stdlib urllib)
# --------------------------------------------------------------------------- #


def _base_url() -> str:
    """Return the configured Jupyter base URL (no trailing slash)."""
    base = os.environ.get("SHARE_FILES_BASE_URL") or os.environ.get("JUPYTER_SERVER_URL")
    if not base:
        raise RuntimeError(
            "No server URL configured. Set SHARE_FILES_BASE_URL to your public "
            "Jupyter URL (e.g. https://hub.example.com/user/<name>/)."
        )
    return base.rstrip("/")


def _token() -> str:
    token = (
        os.environ.get("SHARE_FILES_TOKEN")
        or os.environ.get("JUPYTERHUB_API_TOKEN")
        or os.environ.get("JUPYTER_TOKEN")
    )
    if not token:
        raise RuntimeError(
            "No API token configured. Set SHARE_FILES_TOKEN (or rely on "
            "JUPYTERHUB_API_TOKEN / JUPYTER_TOKEN in the server environment)."
        )
    return token


def _ssl_context() -> Optional[ssl.SSLContext]:
    """Return an unverified SSL context when SHARE_FILES_INSECURE is set.

    Defaults to None (normal certificate verification). Many self-hosted
    JupyterHub deployments use a self-signed certificate; set
    ``SHARE_FILES_INSECURE=1`` to skip verification for those.
    """
    if os.environ.get("SHARE_FILES_INSECURE", "").lower() in ("1", "true", "yes"):
        return ssl._create_unverified_context()
    return None


def _request(method: str, endpoint: str, body: Optional[dict] = None) -> Any:
    """Call the extension API and return parsed JSON.

    Raises RuntimeError with the server's error message on a non-2xx response,
    so the caller gets an actionable explanation rather than a raw traceback.
    """
    url = _base_url() + "/" + NAMESPACE + "/" + endpoint.lstrip("/")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "token " + _token())
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=_ssl_context()) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        message = detail
        try:
            parsed = json.loads(detail)
            message = parsed.get("error") or parsed.get("message") or detail
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(f"Server returned {exc.code}: {message}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach the Jupyter server at {_base_url()}: {exc.reason}"
        ) from None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _fetch_public_json(link: str) -> Optional[dict]:
    """Best-effort, unauthenticated GET of a peer's public manifest.

    Mirrors the panel's `credentials: 'omit'` fetch - never sends the token, so
    it cannot trigger JupyterHub's spawn-as-owner flow. Returns None on failure.
    """
    url = link.rstrip("/") + "/manifest"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, context=_ssl_context()) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Share operations
# --------------------------------------------------------------------------- #


def list_items() -> dict:
    """List your shares, your requests (with upload counts), and your
    connections to other people's shares/requests - with ids, keys and links."""
    shares = _request("GET", "api/shares").get("shares", [])
    requests = _request("GET", "api/requests").get("requests", [])
    connections = _request("GET", "api/connections").get("connections", [])
    return {
        "shares": [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "link": s.get("link"),
                "entries": [e.get("name") for e in s.get("entries", [])],
            }
            for s in shares
        ],
        "requests": [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "link": r.get("link"),
                "upload_count": r.get("upload_count", 0),
            }
            for r in requests
        ],
        "connections": [
            {
                "key": c.get("key"),
                "kind": c.get("kind"),
                "name": c.get("name") or c.get("id"),
                "link": c.get("link"),
            }
            for c in connections
        ],
    }


def create_share(name: str, paths: list[str]) -> dict:
    """Create a named share (a read-only drop) from workspace-relative paths
    and return its shareable link."""
    s = _request("POST", "api/shares", {"name": name, "paths": paths})
    return {"id": s.get("id"), "name": s.get("name"), "link": s.get("link")}


def create_request(name: str) -> dict:
    """Create a named file request (an inbox) and return its link."""
    r = _request("POST", "api/requests", {"name": name})
    return {"id": r.get("id"), "name": r.get("name"), "link": r.get("link")}


def connect(link: str) -> dict:
    """Connect to someone else's share or request link. Returns the connection
    `key` used by other subcommands, plus - for a share - the entry names."""
    c = _request("POST", "api/connections", {"link": link})
    result = {
        "key": c.get("key"),
        "kind": c.get("kind"),
        "name": c.get("name") or c.get("id"),
        "link": c.get("link"),
    }
    if c.get("kind") == "share" and c.get("link"):
        manifest = _fetch_public_json(c["link"])
        if manifest:
            result["entries"] = [e.get("name") for e in manifest.get("entries", [])]
    return result


def disconnect(key: str) -> dict:
    """Remove a connection by its `key`."""
    return _request("DELETE", f"api/connections/{key}")


def close_share(share_id: str) -> dict:
    """Delete one of your own shares by id."""
    return _request("DELETE", f"api/shares/{share_id}")


def close_request(request_id: str) -> dict:
    """Delete one of your own requests by id, with its uploaded files."""
    return _request("DELETE", f"api/requests/{request_id}")


def pick_up(
    key: str, names: Optional[list[str]] = None, target_dir: str = ""
) -> dict:
    """Pick up (download) files from a connected SHARE into the workspace.
    `names` selects specific top-level entries (omit for all); `target_dir` is
    a workspace-relative destination (default: root)."""
    body: dict = {"target_dir": target_dir}
    if names is not None:
        body["names"] = names
    return _request("POST", f"api/connections/{key}/save", body)


def send_to_request(key: str, paths: list[str], uploader: str = "") -> dict:
    """Send (upload) workspace files to a connected REQUEST. `uploader` is an
    optional label shown to the request's owner."""
    return _request(
        "POST",
        f"api/connections/{key}/upload",
        {"paths": paths, "uploader": uploader},
    )


def list_request_uploads(request_id: str) -> dict:
    """List the files uploaded to one of your requests, grouped by uploader,
    with each file's workspace-relative path."""
    r = _request("GET", f"api/requests/{request_id}")
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "upload_count": r.get("upload_count", 0),
        "path": r.get("path"),
        "uploaders": [
            {
                "name": u.get("name"),
                "files": [
                    {"name": e.get("name"), "path": e.get("path"), "type": e.get("type")}
                    for e in u.get("entries", [])
                ],
            }
            for u in r.get("uploaders", [])
        ],
    }


# --------------------------------------------------------------------------- #
# Config file (stores the Cloudflare token)
# --------------------------------------------------------------------------- #


def config_path() -> Path:
    """Path of the CLI config file (created on first save, chmod 600)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / "jupyterlab-share-files" / "config.json"


def _load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_config(cfg: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


# --------------------------------------------------------------------------- #
# Cloudflare API helpers
# --------------------------------------------------------------------------- #


def _cf_request(
    method: str, endpoint: str, token: str, body: Optional[dict] = None
) -> dict:
    """Call the Cloudflare v4 API and return the parsed JSON envelope.

    Cloudflare errors come back as JSON with ``success: false`` and an
    ``errors`` list; HTTP errors carrying that envelope are returned rather
    than raised so callers can report the API's own message.
    """
    url = CLOUDFLARE_API + "/" + endpoint.lstrip("/")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(detail)
        except ValueError:
            return {
                "success": False,
                "errors": [{"code": exc.code, "message": detail or str(exc)}],
            }
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the Cloudflare API: {exc.reason}") from None


def _cf_errors(envelope: dict) -> str:
    return "; ".join(
        f"{e.get('code')}: {e.get('message')}" for e in envelope.get("errors", [])
    )


def cloudflare_verify(token: str, account_id: str = "") -> dict:
    """Report what the token can do with Cloudflare tunnels.

    - ``token_valid`` - the token is active. User-owned tokens verify at
      ``/user/tokens/verify``; account-owned tokens (``cfat_...``) only verify
      at ``/accounts/{id}/tokens/verify``, so both are tried.
    - ``can_bind_existing`` - can list cfd_tunnel in the account (read access,
      enough to bind ``cloudflared`` to an existing tunnel by id/token)
    - ``can_create_tunnel`` - proven by creating a throwaway tunnel and
      deleting it immediately (the only definitive check the API offers)
    """
    result: dict[str, Any] = {
        "token_valid": False,
        "accounts": [],
        "can_bind_existing": False,
        "existing_tunnels": [],
        "can_create_tunnel": False,
    }

    accounts = _cf_request("GET", "accounts", token)
    if accounts.get("success"):
        result["accounts"] = [
            {"id": a.get("id"), "name": a.get("name")}
            for a in accounts.get("result", [])
        ]
    if not account_id:
        if not result["accounts"]:
            result["error"] = (
                "no account id: pass --account_id or use a token that can "
                "list accounts (" + (_cf_errors(accounts) or "empty list") + ")"
            )
            return result
        account_id = result["accounts"][0]["id"]
    result["account_id"] = account_id

    verify = _cf_request("GET", "user/tokens/verify", token)
    if not verify.get("success"):
        verify = _cf_request("GET", f"accounts/{account_id}/tokens/verify", token)
    if not verify.get("success"):
        result["error"] = _cf_errors(verify) or "token verification failed"
        return result
    result["token_valid"] = verify.get("result", {}).get("status") == "active"

    tunnels = _cf_request(
        "GET", f"accounts/{account_id}/cfd_tunnel?is_deleted=false", token
    )
    if tunnels.get("success"):
        result["can_bind_existing"] = True
        result["existing_tunnels"] = [
            {"id": t.get("id"), "name": t.get("name"), "status": t.get("status")}
            for t in tunnels.get("result") or []
        ]
    else:
        result["bind_error"] = _cf_errors(tunnels)

    probe_name = "share-files-verify-" + secrets.token_hex(4)
    created = _cf_request(
        "POST",
        f"accounts/{account_id}/cfd_tunnel",
        token,
        {"name": probe_name, "config_src": "cloudflare"},
    )
    if created.get("success"):
        result["can_create_tunnel"] = True
        tunnel_id = created.get("result", {}).get("id")
        if tunnel_id:
            deleted = _cf_request(
                "DELETE", f"accounts/{account_id}/cfd_tunnel/{tunnel_id}", token
            )
            if not deleted.get("success"):
                result["probe_cleanup_warning"] = (
                    f"verify tunnel '{probe_name}' ({tunnel_id}) could not be "
                    "deleted: " + _cf_errors(deleted)
                )
    else:
        result["create_error"] = _cf_errors(created)

    return result


DEFAULT_TUNNEL_NAME = "share-files"


def _origin_from_base_url(base_url: str) -> str:
    """Derive the tunnel origin (scheme://host) from the PUBLIC base URL.

    The origin is where the `cloudflared` connector forwards traffic. The
    user provides it explicitly via the mandatory ``--local-base-url`` option
    - it is never inferred from the environment or defaulted, so an internal
    address can't sneak in by accident. `localhost` is acceptable when the
    server genuinely runs there (and the connector with it); HTTPS towards
    recipients is enforced at the Cloudflare edge either way.
    """
    parsed = urllib.parse.urlsplit(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(
            f"cloudflare setup: '--local-base-url {base_url}' is not an "
            "absolute URL (expected e.g. https://hub.example.com/user/<name>/)"
        )
    if parsed.scheme != "https":
        raise RuntimeError(
            f"cloudflare setup: '--local-base-url {base_url}' must use "
            "https - only secure connections are permitted (localhost is "
            "fine, but it must be served over https)"
        )
    return parsed.scheme + "://" + parsed.netloc

# Keys --reset removes: the token and every piece of setup state it authorised.
_RESET_KEYS = (
    "cloudflare_token",
    "cloudflare_account_id",
    "cloudflare_tunnel_id",
    "cloudflare_hostname",
    "cloudflare_tunnel_token",
    "public_base_url",
)


def cloudflare_setup(
    token: str, account_id: str, hostname: str, local_base_url: str
) -> dict:
    """Provision a Cloudflare tunnel + DNS route and record the public base.

    Creates (or reuses) a tunnel named ``share-files``, points its ingress at
    the origin derived from ``local_base_url`` (the PUBLIC server URL - see
    ``_origin_from_base_url``) restricted to the extension's ``/public/``
    endpoints, routes ``hostname`` to it with a proxied CNAME, enforces HTTPS
    on the zone, and saves ``public_base_url = https://<hostname>`` so the
    server rewrites generated share/request links to the Cloudflare host.
    Returns the connector token and the ``cloudflared`` command to run.
    """
    origin = _origin_from_base_url(local_base_url)
    verify = cloudflare_verify(token, account_id)
    if not verify.get("can_create_tunnel"):
        raise RuntimeError(
            "cloudflare setup: the token cannot create tunnels ("
            + (verify.get("create_error") or verify.get("error") or "unknown")
            + "); fix the token permissions and re-run --verify"
        )
    account_id = verify["account_id"]

    # 1. Reuse the share-files tunnel if it exists, else create it.
    tunnel = next(
        (t for t in verify["existing_tunnels"] if t.get("name") == DEFAULT_TUNNEL_NAME),
        None,
    )
    if tunnel is None:
        created = _cf_request(
            "POST",
            f"accounts/{account_id}/cfd_tunnel",
            token,
            {"name": DEFAULT_TUNNEL_NAME, "config_src": "cloudflare"},
        )
        if not created.get("success"):
            raise RuntimeError("cloudflare setup: tunnel create failed: " + _cf_errors(created))
        tunnel = created["result"]
    tunnel_id = tunnel["id"]

    # 2. Ingress: ONLY the extension's unauthenticated /public/ capability
    # endpoints are routed to the origin - the rest of the hub and anything
    # else on the private network stays unreachable (404 at the edge).
    public_path = r"^(/user/[^/]+)?/jupyterlab-share-files-extension/public/.*"
    origin_host = urllib.parse.urlsplit(origin).hostname or ""
    # cloudflared forwards the original Host (the tunnel hostname) by default;
    # a reverse proxy in front of the origin routes by Host and would 404, so
    # send the origin's own name for both the Host header and the TLS SNI.
    origin_request = {
        "httpHostHeader": origin_host,
        "originServerName": origin_host,
    }
    if origin.startswith("https://"):
        origin_request["noTLSVerify"] = True
    ingress = [
        {
            "hostname": hostname,
            "path": public_path,
            "service": origin,
            "originRequest": origin_request,
        },
        {"service": "http_status:404"},
    ]
    configured = _cf_request(
        "PUT",
        f"accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        token,
        {"config": {"ingress": ingress}},
    )
    if not configured.get("success"):
        raise RuntimeError("cloudflare setup: ingress config failed: " + _cf_errors(configured))

    # 3. DNS: proxied CNAME <sub> -> <tunnel>.cfargotunnel.com on the apex zone.
    apex = ".".join(hostname.split(".")[-2:])
    zones = _cf_request("GET", f"zones?name={apex}", token)
    zone_list = zones.get("result") or []
    if not zones.get("success") or not zone_list:
        raise RuntimeError(
            f"cloudflare setup: zone '{apex}' not found: " + (_cf_errors(zones) or "empty list")
        )
    zone_id = zone_list[0]["id"]
    record = {
        "type": "CNAME",
        "name": hostname,
        "content": f"{tunnel_id}.cfargotunnel.com",
        "proxied": True,
    }
    existing = _cf_request(
        "GET", f"zones/{zone_id}/dns_records?type=CNAME&name={hostname}", token
    )
    matches = existing.get("result") or []
    if matches:
        dns = _cf_request(
            "PUT", f"zones/{zone_id}/dns_records/{matches[0]['id']}", token, record
        )
    else:
        dns = _cf_request("POST", f"zones/{zone_id}/dns_records", token, record)
    if not dns.get("success"):
        raise RuntimeError("cloudflare setup: DNS record failed: " + _cf_errors(dns))

    # 4. HTTPS only: plain http is redirected (301) at the Cloudflare edge.
    https_setting = _cf_request(
        "GET", f"zones/{zone_id}/settings/always_use_https", token
    )
    if https_setting.get("success") and https_setting["result"].get("value") != "on":
        forced = _cf_request(
            "PATCH",
            f"zones/{zone_id}/settings/always_use_https",
            token,
            {"value": "on"},
        )
        if not forced.get("success"):
            raise RuntimeError(
                "cloudflare setup: could not enforce HTTPS (always_use_https): "
                + _cf_errors(forced)
            )

    # 5. Connector token for `cloudflared tunnel run`.
    tok = _cf_request("GET", f"accounts/{account_id}/cfd_tunnel/{tunnel_id}/token", token)
    if not tok.get("success"):
        raise RuntimeError("cloudflare setup: connector token fetch failed: " + _cf_errors(tok))
    tunnel_token = tok["result"]

    public_base_url = "https://" + hostname
    cfg = _load_config()
    cfg.update(
        cloudflare_account_id=account_id,
        cloudflare_tunnel_id=tunnel_id,
        cloudflare_hostname=hostname,
        cloudflare_tunnel_token=tunnel_token,
        public_base_url=public_base_url,
    )
    _save_config(cfg)

    return {
        "tunnel_id": tunnel_id,
        "tunnel_name": DEFAULT_TUNNEL_NAME,
        "hostname": hostname,
        "origin": origin,
        "public_base_url": public_base_url,
        "run_command": f"cloudflared tunnel run --token {tunnel_token}",
        "saved": str(config_path()),
    }


CONNECTOR_LOG = "/tmp/cloudflared-share-files.log"


def _connector_running() -> bool:
    try:
        out = subprocess.run(
            ["ps", "-eo", "args"], capture_output=True, text=True
        ).stdout
    except OSError:
        return False
    return any("cloudflared tunnel run" in line for line in out.splitlines())


def ensure_connector(retries: int = 3, logger: Optional[logging.Logger] = None) -> bool:
    """Make sure the cloudflared connector for the configured tunnel runs.

    Called by the server extension at startup (and after tunnel setup) when
    Cloudflare sharing is configured. Tries up to ``retries`` times
    (configurable via ``c.ShareFilesConfig.cloudflared_retries``); each
    attempt spawns ``cloudflared tunnel run`` and checks it stays up. On
    failure the error is logged and False returned; True on success.
    """
    log = logger or logging.getLogger("jupyterlab_share_files_extension")
    token = _load_config().get("cloudflare_tunnel_token", "")
    if not token:
        log.error("cloudflared connector: no tunnel configured (run cloudflare --local-base-url first)")
        return False
    if _connector_running():
        log.info("cloudflared connector already running")
        return True
    for attempt in range(1, retries + 1):
        try:
            with open(CONNECTOR_LOG, "ab") as logfile:
                proc = subprocess.Popen(
                    ["cloudflared", "tunnel", "run", "--token", token],
                    stdout=logfile,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            log.error(
                f"cloudflared launch failed (attempt {attempt}/{retries}): {exc}"
            )
            continue
        time.sleep(2)
        if proc.poll() is None:
            log.info(
                f"cloudflared connector started (pid {proc.pid}, log {CONNECTOR_LOG})"
            )
            return True
        log.error(
            f"cloudflared exited with code {proc.returncode} "
            f"(attempt {attempt}/{retries}); see {CONNECTOR_LOG}"
        )
    log.error(
        f"cloudflared connector failed after {retries} attempts - "
        f"Cloudflare links will not work; see {CONNECTOR_LOG}"
    )
    return False


# --------------------------------------------------------------------------- #
# Subcommand handlers
# --------------------------------------------------------------------------- #


def _mask_secret(value: str) -> str:
    """Show only the last 4 characters of a secret."""
    if not value:
        return ""
    return "..." + value[-4:]


def _cmd_cloudflare(args: argparse.Namespace) -> Any:
    cmd = args.cf_command
    if cmd == "reset":
        cfg = _load_config()
        removed = [k for k in _RESET_KEYS if cfg.pop(k, None) is not None]
        _save_config(cfg)
        return {"reset": removed}
    if cmd == "info":
        cfg = _load_config()
        # tunnel status as Cloudflare sees it (healthy/degraded/inactive/down)
        tunnel_status = ""
        token = cfg.get("cloudflare_token", "")
        account_id = cfg.get("cloudflare_account_id", "")
        tunnel_id = cfg.get("cloudflare_tunnel_id", "")
        if token and account_id and tunnel_id:
            try:
                env = _cf_request(
                    "GET", f"accounts/{account_id}/cfd_tunnel/{tunnel_id}", token
                )
                if env.get("success"):
                    tunnel_status = env.get("result", {}).get("status", "")
                else:
                    tunnel_status = "unknown (" + _cf_errors(env) + ")"
            except RuntimeError as exc:
                tunnel_status = f"unknown ({exc})"
        return {
            "config_path": str(config_path()),
            "cloudflare_token": _mask_secret(cfg.get("cloudflare_token", "")),
            "cloudflare_account_id": account_id,
            "cloudflare_tunnel_id": tunnel_id,
            "cloudflare_hostname": cfg.get("cloudflare_hostname", ""),
            "cloudflare_tunnel_token": _mask_secret(
                cfg.get("cloudflare_tunnel_token", "")
            ),
            "public_base_url": cfg.get("public_base_url", ""),
            "daemon_running": _connector_running(),
            "tunnel_status": tunnel_status,
        }
    if cmd == "validate":
        cfg = _load_config()
        token = cfg.get("cloudflare_token")
        if not token:
            raise RuntimeError(
                "cloudflare validate: no token saved; run cloudflare setup "
                "--token first"
            )
        return {
            "validate": cloudflare_verify(
                token, cfg.get("cloudflare_account_id") or ""
            )
        }
    # setup
    out: dict[str, Any] = {}
    if args.token or args.account_id:
        cfg = _load_config()
        if args.token:
            cfg["cloudflare_token"] = args.token
        if args.account_id:
            cfg["cloudflare_account_id"] = args.account_id
        _save_config(cfg)
        out["saved"] = str(config_path())
    cfg = _load_config()
    token = args.token or cfg.get("cloudflare_token")
    if not token:
        raise RuntimeError(
            "cloudflare setup: no token given or saved; pass --token"
        )
    account_id = args.account_id or cfg.get("cloudflare_account_id") or ""
    out["setup"] = cloudflare_setup(
        token, account_id, args.hostname, args.local_base_url
    )
    out["setup"]["daemon_running"] = ensure_connector()
    out["setup"]["connector_log"] = CONNECTOR_LOG
    return out


_HANDLERS = {
    "list-items": lambda a: list_items(),
    "create-share": lambda a: create_share(a.name, a.paths),
    "create-request": lambda a: create_request(a.name),
    "connect": lambda a: connect(a.link),
    "disconnect": lambda a: disconnect(a.key),
    "close-share": lambda a: close_share(a.id),
    "close-request": lambda a: close_request(a.id),
    "pick-up": lambda a: pick_up(a.key, a.names or None, a.target_dir),
    "send-to-request": lambda a: send_to_request(a.key, a.paths, a.uploader),
    "list-request-uploads": lambda a: list_request_uploads(a.id),
    "cloudflare": _cmd_cloudflare,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jupyterlab_share_files",
        description="CLI for the Share Files JupyterLab extension.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine-readable JSON output instead of the human-readable form",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-items", help="list your shares, requests and connections")

    p = sub.add_parser("create-share", help="create a share from workspace paths")
    p.add_argument("name")
    p.add_argument("paths", nargs="*", default=[])

    p = sub.add_parser("create-request", help="create a file request (inbox)")
    p.add_argument("name")

    p = sub.add_parser("connect", help="connect to a share or request link")
    p.add_argument("link")

    p = sub.add_parser("disconnect", help="remove a connection by key")
    p.add_argument("key")

    p = sub.add_parser("close-share", help="delete one of your shares by id")
    p.add_argument("id")

    p = sub.add_parser("close-request", help="delete one of your requests by id")
    p.add_argument("id")

    p = sub.add_parser("pick-up", help="save files from a connected share")
    p.add_argument("key")
    p.add_argument("names", nargs="*", default=[])
    p.add_argument("--target-dir", default="")

    p = sub.add_parser("send-to-request", help="upload files to a connected request")
    p.add_argument("key")
    p.add_argument("paths", nargs="+")
    p.add_argument("--uploader", default="")

    p = sub.add_parser(
        "list-request-uploads", help="list files uploaded to one of your requests"
    )
    p.add_argument("id")

    p = sub.add_parser(
        "cloudflare",
        help="Cloudflare tunnel sharing: setup, validate, info, reset",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Expose share/request links beyond the hub or local network "
            "through a Cloudflare tunnel.\n"
            "Four orthogonal subcommands cover the whole lifecycle:\n\n"
            "  setup     save credentials and provision everything: tunnel, "
            "proxied DNS record,\n"
            "            HTTPS enforcement (http 301s to https at the edge), "
            "public_base_url link\n"
            "            rewriting, and the cloudflared connector daemon. "
            "Only the extension's\n"
            "            unauthenticated /public/ endpoints pass through the "
            "tunnel - the hub login,\n"
            "            authenticated API and the private network answer 404 "
            "at the Cloudflare edge.\n"
            "  validate  end-to-end check of the saved config: token "
            "validity, bind to existing\n"
            "            tunnels, create rights (proven by creating a test "
            "tunnel and removing it).\n"
            "  info      current configuration - tokens masked to their last "
            "4 characters, account id\n"
            "            in full, daemon_running (cloudflared process) and "
            "tunnel_status (Cloudflare).\n"
            "  reset     reset the saved token to none: clears account id, "
            "tunnel state and\n"
            "            public_base_url - links revert to the local/hub "
            "address on the next request.\n"
            "            Cloudflare-side resources (tunnel, DNS) are kept."
        ),
        epilog=(
            "examples:\n"
            "  # provision end to end (token policies: Account|Cloudflare "
            "Tunnel|Edit + Zone|DNS|Edit)\n"
            "  jupyterlab_share_files cloudflare setup \\\n"
            "      --token <api-token> --account-id <account-id> \\\n"
            "      --hostname share.example.com \\\n"
            "      --local-base-url https://hub.example.com/user/<name>/\n\n"
            "  # check the saved config end to end\n"
            "  jupyterlab_share_files cloudflare validate\n\n"
            "  # show the configuration and daemon/tunnel status "
            "(machine-readable: --json before cloudflare)\n"
            "  jupyterlab_share_files cloudflare info\n"
            "  jupyterlab_share_files --json cloudflare info\n\n"
            "  # back to the unconfigured state\n"
            "  jupyterlab_share_files cloudflare reset\n\n"
            "full guide: docs/cloudflare_setup.md"
        ),
    )
    cf = p.add_subparsers(dest="cf_command", required=True)

    ps = cf.add_parser(
        "setup",
        help="save credentials and provision the tunnel end to end "
        "(tunnel + DNS + HTTPS enforcement + link rewriting + connector)",
    )
    ps.add_argument("--token", default="", help="Cloudflare API token to save")
    ps.add_argument(
        "--account-id", dest="account_id", default="", help="Cloudflare account id to save"
    )
    ps.add_argument("--hostname", default="share.duoptimum.com")
    ps.add_argument(
        "--local-base-url",
        required=True,
        help="REQUIRED: this server's URL as the cloudflared connector "
        "reaches it (e.g. https://hub.example.com/user/<name>/ - given "
        "explicitly, never inferred, https only)",
    )

    cf.add_parser(
        "validate",
        help="end-to-end check of the saved config: token validity, bind to "
        "existing tunnels, create rights (proven by creating a test tunnel "
        "and removing it)",
    )

    cf.add_parser(
        "info",
        help="show the current Cloudflare configuration (tokens masked to "
        "their last 4 characters) and whether the connector is running",
    )

    cf.add_parser(
        "reset",
        help="reset the saved Cloudflare token to none (also clears account "
        "id, tunnel state and public_base_url; Cloudflare-side resources are "
        "kept)",
    )

    return parser


def _render_human(value: Any, indent: int = 0) -> list[str]:
    """Render a result as indented `key: value` lines for terminal reading."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, val in value.items():
            if isinstance(val, (dict, list)) and val:
                lines.append(f"{pad}{key}:")
                lines.extend(_render_human(val, indent + 1))
            else:
                lines.append(f"{pad}{key}: {val if val not in ({}, []) else '-'}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.extend(_render_human(item, indent + 1))
            else:
                lines.append(f"{pad}- {item}")
    else:
        lines.append(f"{pad}{value}")
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _HANDLERS[args.command](args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("\n".join(_render_human(result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
