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

Additionally provides a ``cloudflare`` command with six orthogonal
subcommands:

- ``setup --token T --account-id A --hostname H --private-base-url U`` - save
  the credentials and provision the tunnel end to end (tunnel + DNS + HTTPS
  enforcement + ``public_base_url`` link rewriting + connector daemon)
- ``validate`` - end-to-end check of the saved config: token validity, bind
  to existing tunnels, create rights (proven by creating a test tunnel and
  removing it)
- ``info`` - current configuration with tokens masked to their last 4
  characters, plus daemon and tunnel status
- ``start`` / ``stop`` - switch between public links (tunnel active, daemon
  running) and private links (daemon stopped); setup and credentials kept
- ``reset`` - reset the saved token to none (clears account id, tunnel state
  and ``public_base_url``; links revert to the local/hub address)

The CLI is a thin frontend: every cloudflare subcommand dispatches into
the ``tunnel`` library module, which also backs the server's ``api/tunnel*``
endpoints - one implementation, two frontends.

Run it with the console script ``jupyterlab_share_files``.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from . import tunnel

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


def create_share(name: str, paths: list[str], password: str = "") -> dict:
    """Create a named share (a read-only drop) from workspace-relative paths
    and return its shareable link. An optional password protects all public
    access to the share."""
    body: dict = {"name": name, "paths": paths}
    if password:
        body["password"] = password
    s = _request("POST", "api/shares", body)
    result = {"id": s.get("id"), "name": s.get("name"), "link": s.get("link")}
    if password:
        result["password"] = password
    return result


def create_request(name: str, password: str = "") -> dict:
    """Create a named file request (an inbox) and return its link. An optional
    password protects all public access to the request."""
    body: dict = {"name": name}
    if password:
        body["password"] = password
    r = _request("POST", "api/requests", body)
    result = {"id": r.get("id"), "name": r.get("name"), "link": r.get("link")}
    if password:
        result["password"] = password
    return result


def set_password(kind: str, id_: str, password: str) -> dict:
    """Set, change, or clear (empty) the password of one of your shares or
    requests. ``kind`` is 'share' or 'request'."""
    plural = "shares" if kind == "share" else "requests"
    r = _request("POST", f"api/{plural}/{id_}/password", {"password": password})
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "has_password": bool(password),
        "password": password,
    }


def generate_password() -> dict:
    """Generate an xkcd-style passphrase (server-side, via xkcdpass)."""
    return _request("GET", "api/generate-password")


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


def _cmd_cloudflare(args: argparse.Namespace) -> Any:
    """Thin dispatcher - ALL behaviour lives in the `tunnel` library module
    (shared with the server's api/tunnel* endpoints, so the CLI and the
    panel can never diverge)."""
    cmd = args.cf_command
    if cmd == "start":
        return tunnel.tunnel_start()
    if cmd == "stop":
        return tunnel.tunnel_stop()
    if cmd == "reset":
        return tunnel.reset_config()
    if cmd == "info":
        return tunnel.tunnel_info()
    if cmd == "validate":
        return tunnel.validate_config()
    # setup
    return tunnel.setup_and_start(
        args.token, args.account_id, args.hostname, args.private_base_url
    )


def _resolve_password(args: argparse.Namespace) -> str:
    """Password for a create command: explicit value, or xkcdpass-generated."""
    if getattr(args, "generate_password", False):
        return generate_password().get("password") or ""
    return getattr(args, "password", "") or ""


def _cmd_set_password(args: argparse.Namespace) -> dict:
    if args.clear:
        return set_password(args.kind, args.id, "")
    password = args.password
    if args.generate or not password:
        password = generate_password().get("password") or ""
    return set_password(args.kind, args.id, password)


_HANDLERS = {
    "list-items": lambda a: list_items(),
    "create-share": lambda a: create_share(a.name, a.paths, _resolve_password(a)),
    "create-request": lambda a: create_request(a.name, _resolve_password(a)),
    "set-password": _cmd_set_password,
    "generate-password": lambda a: generate_password(),
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
    sub = parser.add_subparsers(dest="command", metavar="<command>", title="commands")

    sub.add_parser("list-items", help="list your shares, requests and connections")

    p = sub.add_parser("create-share", help="create a share from workspace paths")
    p.add_argument("name")
    p.add_argument("paths", nargs="*", default=[])
    p.add_argument("--password", default="", help="protect public access with this password")
    p.add_argument(
        "--generate-password",
        action="store_true",
        help="protect public access with a generated xkcd-style passphrase",
    )

    p = sub.add_parser("create-request", help="create a file request (inbox)")
    p.add_argument("name")
    p.add_argument("--password", default="", help="protect public access with this password")
    p.add_argument(
        "--generate-password",
        action="store_true",
        help="protect public access with a generated xkcd-style passphrase",
    )

    p = sub.add_parser(
        "set-password",
        help="set, change, or clear the password of a share or request",
    )
    p.add_argument("kind", choices=("share", "request"))
    p.add_argument("id")
    p.add_argument("password", nargs="?", default="")
    p.add_argument("--generate", action="store_true", help="generate an xkcd-style passphrase")
    p.add_argument("--clear", action="store_true", help="remove the password")

    sub.add_parser(
        "generate-password", help="generate an xkcd-style passphrase (xkcdpass)"
    )

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
        help="Cloudflare tunnel sharing: setup, validate, info, start, stop, reset",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Expose share/request links beyond the hub or local network "
            "through a Cloudflare tunnel.\n"
            "Six orthogonal subcommands cover the whole lifecycle:\n\n"
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
            "  start     switch to PUBLIC links: mark the tunnel active and "
            "start the cloudflared\n"
            "            daemon - generated links carry the public hostname.\n"
            "  stop      switch to PRIVATE links: stop the daemon - links "
            "revert to the\n"
            "            local/hub address on the next request. Setup and "
            "credentials are kept.\n"
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
            "      --private-base-url https://hub.example.com/user/<name>/\n\n"
            "  # check the saved config end to end\n"
            "  jupyterlab_share_files cloudflare validate\n\n"
            "  # show the configuration and daemon/tunnel status "
            "(machine-readable: --json before cloudflare)\n"
            "  jupyterlab_share_files cloudflare info\n"
            "  jupyterlab_share_files --json cloudflare info\n\n"
            "  # toggle between public and private links\n"
            "  jupyterlab_share_files cloudflare start\n"
            "  jupyterlab_share_files cloudflare stop\n\n"
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
        "--private-base-url",
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
        "start",
        help="switch to public links: mark the tunnel active and start the "
        "cloudflared daemon",
    )

    cf.add_parser(
        "stop",
        help="switch to private links: stop the cloudflared daemon; setup "
        "and credentials are kept",
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


# Human-output value colouring: states that read as healthy vs failed.
_GOOD_VALUES = {"true", "healthy", "active", "on", "ok"}
_BAD_VALUES = {"false", "down", "inactive", "degraded", "error"}


def _colorize_human(lines: list[str]) -> list[str]:
    """Conservative colouring of `key: value` output: keys cyan, healthy
    state values green, failed state values red. Plain text when stdout is
    not a terminal or NO_COLOR is set."""
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return lines
    cyan, green, red, reset = "\033[36m", "\033[32m", "\033[31m", "\033[0m"
    out: list[str] = []
    for line in lines:
        key, sep, value = line.partition(": ")
        if sep:
            v = value.strip().lower()
            if v in _GOOD_VALUES:
                value = green + value + reset
            elif v in _BAD_VALUES or v.startswith("unknown"):
                value = red + value + reset
            indent = key[: len(key) - len(key.lstrip())]
            line = indent + cyan + key.lstrip() + reset + sep + value
        elif line.endswith(":"):
            indent = line[: len(line) - len(line.lstrip())]
            line = indent + cyan + line.lstrip() + reset
        out.append(line)
    return out


def _colorize_help(text: str) -> str:
    """Conservative ANSI colouring of argparse help: bold section headers
    and usage prefix, cyan command/option names. Plain text when stdout is
    not a terminal or NO_COLOR is set."""
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    bold, cyan, reset = "\033[1m", "\033[36m", "\033[0m"
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if line.startswith("usage:"):
            out.append(bold + "usage:" + reset + line[len("usage:"):])
        elif line and not line.startswith(" ") and line.endswith(":"):
            out.append(bold + line + reset)
        elif line.startswith("  ") and not line.startswith("       ") and stripped:
            name, sep, rest = line.lstrip().partition("  ")
            indent = line[: len(line) - len(line.lstrip())]
            out.append(indent + cyan + name + reset + sep + rest)
        else:
            out.append(line)
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        print(_colorize_help(parser.format_help()))
        return 0
    try:
        result = _HANDLERS[args.command](args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("\n".join(_colorize_human(_render_human(result))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
