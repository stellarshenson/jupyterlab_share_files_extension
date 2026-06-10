"""Cloudflare tunnel sharing - the library behind the CLI and the HTTP API.

ALL tunnel/Cloudflare behaviour lives here, exactly once. The CLI
(``cli.py``, `cloudflare` subcommands) and the server API (``routes.py``,
``api/tunnel*``) are thin frontends dispatching into this module - they must
never grow their own logic, or the two paths diverge.

Covers: the chmod-600 config file, the Cloudflare REST client, token
verification, end-to-end tunnel provisioning (`setup_and_start`), the
public/private toggle (`tunnel_start`/`tunnel_stop`), autostart, state and
info views, validation, reset, and the cloudflared connector daemon
lifecycle.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import signal
import ssl
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"


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


TUNNEL_NAME_PREFIX = "share-files"


def tunnel_name(private_base_url: str) -> str:
    """Tunnel name: extension prefix + sluggified private base URL.

    Deterministic and reproducible - the same ``--private-base-url`` always
    maps to the same tunnel, so repeated setups reuse it (idempotency), while
    different users/servers on the same Cloudflare account never collide on
    a shared name. E.g. ``https://hub.example.com/user/alice/`` ->
    ``share-files-hub-example-com-user-alice``.
    """
    parsed = urllib.parse.urlsplit(private_base_url)
    raw = (parsed.netloc + parsed.path).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return f"{TUNNEL_NAME_PREFIX}-{slug}"


def _origin_from_base_url(base_url: str) -> str:
    """Derive the tunnel origin (scheme://host) from the PUBLIC base URL.

    The origin is where the `cloudflared` connector forwards traffic. The
    user provides it explicitly via the mandatory ``--private-base-url`` option
    - it is never inferred from the environment or defaulted, so an internal
    address can't sneak in by accident. `localhost` is acceptable when the
    server genuinely runs there (and the connector with it); HTTPS towards
    recipients is enforced at the Cloudflare edge either way.
    """
    parsed = urllib.parse.urlsplit(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(
            f"cloudflare setup: '--private-base-url {base_url}' is not an "
            "absolute URL (expected e.g. https://hub.example.com/user/<name>/)"
        )
    if parsed.scheme != "https":
        raise RuntimeError(
            f"cloudflare setup: '--private-base-url {base_url}' must use "
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
    "private_base_url",
    "public_base_url",
    "tunnel_active",
    "tunnel_autostart",
)


def cloudflare_setup(
    token: str, account_id: str, hostname: str, private_base_url: str
) -> dict:
    """Provision a Cloudflare tunnel + DNS route and record the public base.

    Creates (or reuses) a tunnel named per ``tunnel_name`` (extension prefix
    + sluggified private base URL - unique per user/server, reproducible
    across runs), points its ingress at
    the origin derived from ``private_base_url`` (the server's own URL - see
    ``_origin_from_base_url``) restricted to the extension's ``/public/``
    endpoints, routes ``hostname`` to it with a proxied CNAME, enforces HTTPS
    on the zone, and saves ``public_base_url = https://<hostname>`` so the
    server rewrites generated share/request links to the Cloudflare host.
    Returns the connector token and the ``cloudflared`` command to run.
    """
    origin = _origin_from_base_url(private_base_url)
    verify = cloudflare_verify(token, account_id)
    if not verify.get("can_create_tunnel"):
        raise RuntimeError(
            "cloudflare setup: the token cannot create tunnels ("
            + (verify.get("create_error") or verify.get("error") or "unknown")
            + "); fix the token permissions and re-run --verify"
        )
    account_id = verify["account_id"]

    # 1. Reuse this server's tunnel if it exists, else create it. The name
    # is derived from the private base URL, so it is stable across setups
    # and unique per user/server on a shared account.
    name = tunnel_name(private_base_url)
    tunnel = next(
        (t for t in verify["existing_tunnels"] if t.get("name") == name),
        None,
    )
    if tunnel is None:
        created = _cf_request(
            "POST",
            f"accounts/{account_id}/cfd_tunnel",
            token,
            {"name": name, "config_src": "cloudflare"},
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
        cloudflare_tunnel_name=name,
        cloudflare_tunnel_token=tunnel_token,
        private_base_url=private_base_url,
        public_base_url=public_base_url,
        tunnel_active=True,
    )
    _save_config(cfg)

    return {
        "tunnel_id": tunnel_id,
        "tunnel_name": name,
        "hostname": hostname,
        "origin": origin,
        "private_base_url": private_base_url,
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


def stop_connector() -> bool:
    """Stop any running cloudflared connector. True when one was stopped."""
    stopped = False
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,args"], capture_output=True, text=True
        ).stdout
    except OSError:
        return False
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and "cloudflared tunnel run" in parts[1]:
            try:
                os.kill(int(parts[0]), signal.SIGTERM)
                stopped = True
            except (OSError, ValueError):
                pass
    # Wait for the processes to actually exit - callers (e.g. setup with a
    # changed tunnel token) start a replacement right after, and a dying
    # daemon still visible in ps would make ensure_connector a no-op.
    if stopped:
        for _ in range(20):
            if not _connector_running():
                break
            time.sleep(0.25)
    return stopped


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
        log.error("cloudflared connector: no tunnel configured (run cloudflare setup first)")
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



def reset_config() -> dict:
    """Reset the saved token to none - `cloudflare reset`.

    Removes the token and every piece of setup state it authorised
    (account id, tunnel state, private/public base URLs, toggle flags);
    unrelated config keys are preserved. Local-only: Cloudflare-side
    resources (tunnel, DNS) are kept. Shared by the CLI and the panel
    (`api/tunnel/reset`).
    """
    cfg = _load_config()
    removed = [k for k in _RESET_KEYS if cfg.pop(k, None) is not None]
    _save_config(cfg)
    return {"reset": removed}


def setup_and_start(
    token: str, account_id: str, hostname: str, private_base_url: str
) -> dict:
    """Save credentials, provision the tunnel end to end, guarantee the daemon.

    The full `cloudflare setup` sequence, shared by the CLI and the panel's
    configuration popup (`api/tunnel/setup`). Restarts the connector when the
    tunnel token changed - a daemon still serving the OLD tunnel would leave
    the new hostname dead.
    """
    out: dict[str, Any] = {}
    if token or account_id:
        cfg = _load_config()
        if token:
            cfg["cloudflare_token"] = token
        if account_id:
            cfg["cloudflare_account_id"] = account_id
        _save_config(cfg)
        out["saved"] = str(config_path())
    cfg = _load_config()
    token = token or cfg.get("cloudflare_token") or ""
    if not token:
        raise RuntimeError(
            "cloudflare setup: no token given or saved; pass --token"
        )
    account_id = account_id or cfg.get("cloudflare_account_id") or ""
    prev_tunnel_token = cfg.get("cloudflare_tunnel_token", "")
    out["setup"] = cloudflare_setup(token, account_id, hostname, private_base_url)
    if (
        _load_config().get("cloudflare_tunnel_token", "") != prev_tunnel_token
        and _connector_running()
    ):
        stop_connector()
    out["setup"]["daemon_running"] = ensure_connector()
    out["setup"]["connector_log"] = CONNECTOR_LOG
    return out



def tunnel_start(retries: int = 3) -> dict:
    """Switch to public links: mark the tunnel active and start the daemon."""
    cfg = _load_config()
    if not cfg.get("cloudflare_tunnel_token"):
        raise RuntimeError(
            "cloudflare start: no tunnel configured; run cloudflare setup first"
        )
    cfg["tunnel_active"] = True
    _save_config(cfg)
    return {
        "tunnel_active": True,
        "daemon_running": ensure_connector(retries),
        "connector_log": CONNECTOR_LOG,
    }


def tunnel_stop() -> dict:
    """Switch to private links: stop the daemon; setup and credentials kept."""
    cfg = _load_config()
    cfg["tunnel_active"] = False
    _save_config(cfg)
    return {"tunnel_active": False, "daemon_stopped": stop_connector()}


def set_tunnel_autostart(value: bool) -> dict:
    """Persist whether the server brings the tunnel up at startup."""
    cfg = _load_config()
    cfg["tunnel_autostart"] = bool(value)
    _save_config(cfg)
    return {"tunnel_autostart": bool(value)}


def tunnel_state() -> dict:
    """Config-file view of the tunnel: configured / active / autostart / running."""
    cfg = _load_config()
    return {
        "tunnel_configured": bool(cfg.get("public_base_url")),
        "tunnel_active": bool(cfg.get("tunnel_active", True)),
        "tunnel_autostart": bool(cfg.get("tunnel_autostart", True)),
        "tunnel_running": _connector_running(),
    }


def tunnel_info() -> dict:
    """Full configuration view - `cloudflare info`. Secrets masked to their
    last 4 characters; includes daemon state and the Cloudflare-side
    tunnel status."""
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
        "cloudflare_tunnel_name": cfg.get("cloudflare_tunnel_name", ""),
        "cloudflare_hostname": cfg.get("cloudflare_hostname", ""),
        "cloudflare_tunnel_token": _mask_secret(
            cfg.get("cloudflare_tunnel_token", "")
        ),
        "private_base_url": cfg.get("private_base_url", ""),
        "public_base_url": cfg.get("public_base_url", ""),
        "tunnel_active": cfg.get("tunnel_active", True),
        "tunnel_autostart": cfg.get("tunnel_autostart", True),
        "daemon_running": _connector_running(),
        "tunnel_status": tunnel_status,
    }


def validate_config() -> dict:
    """End-to-end check of the SAVED config - `cloudflare validate`."""
    cfg = _load_config()
    token = cfg.get("cloudflare_token")
    if not token:
        raise RuntimeError(
            "cloudflare validate: no token saved; run cloudflare setup "
            "--token first"
        )
    result = cloudflare_verify(token, cfg.get("cloudflare_account_id") or "")
    # The extension launches the connector itself - if the system can't
    # reach the cloudflared binary, the tunnel can never come up.
    cloudflared = shutil.which("cloudflared")
    result["cloudflared_available"] = bool(cloudflared)
    result["cloudflared_path"] = cloudflared or (
        "NOT FOUND - install cloudflared and make sure it is on the "
        "PATH of the Jupyter server process"
    )
    return {"validate": result}


def apply_autostart(
    retries: int = 3, logger: Optional[logging.Logger] = None
) -> None:
    """Honour the autostart preference at server startup.

    Configured + autostart on -> tunnel active, daemon ensured. Autostart
    off -> tunnel forced inactive so generated links stay private instead
    of carrying a public host the tunnel can't serve. No tunnel configured
    -> no-op.
    """
    log = logger or logging.getLogger("jupyterlab_share_files_extension")
    cfg = _load_config()
    if not cfg.get("cloudflare_tunnel_token"):
        return
    if cfg.get("tunnel_autostart", True):
        if not cfg.get("tunnel_active", True):
            cfg["tunnel_active"] = True
            _save_config(cfg)
        ensure_connector(retries, log)
    else:
        if cfg.get("tunnel_active", True):
            cfg["tunnel_active"] = False
            _save_config(cfg)
        log.info(
            "cloudflared connector autostart disabled - links stay private "
            "(toggle the cloud icon or api/tunnel to go public)"
        )
