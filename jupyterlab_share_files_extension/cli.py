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


class ServerError(RuntimeError):
    """A non-2xx answer from the extension API; ``payload`` is its JSON body."""

    def __init__(self, message: str, payload: dict):
        super().__init__(message)
        self.payload = payload


def _connection(key: str, *rest: str) -> str:
    """The API path of a connection: the key holds the peer's ``https://``
    on a standalone lab, so it is encoded as one path segment."""
    return "/".join(["api/connections", urllib.parse.quote(key, safe=""), *rest])


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
        payload: dict = {}
        try:
            parsed = json.loads(detail)
            message = parsed.get("error") or parsed.get("message") or detail
            payload = parsed
        except (ValueError, AttributeError):
            if exc.code == 404:
                # the server's own page, not the extension's answer
                message = (
                    "the server has no such route - the command does not exist in this "
                    "server's mode, or the id or key is malformed; run list-items for the ids and keys"
                )
        raise ServerError(f"Server returned {exc.code}: {message}", payload) from None
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


def connect(link: str, trust_certificate: bool = False) -> dict:
    """Connect to someone else's share or request link. Returns the connection
    `key` used by other subcommands, plus - for a share - the entry names.

    A host whose certificate the system does not trust is refused unless
    ``trust_certificate`` is set: then the certificate the lab read is trusted,
    as Trust in the panel's dialog does, and kept with the connection."""
    trusted = None
    try:
        c = _request("POST", "api/connections", {"link": link})
    except ServerError as exc:
        cert = exc.payload.get("certificate")
        if exc.payload.get("password_required"):
            raise RuntimeError(
                f"{exc}. The CLI cannot send a password: ask the user to connect this link "
                "in the panel, then run list-items for its key"
            ) from None
        if not cert:
            raise
        if not trust_certificate:
            raise RuntimeError(
                f"{exc}. Its SHA-256 fingerprint is {cert['fingerprint']}. "
                "If you know this host and this certificate, run connect again with --trust-certificate"
            ) from None
        c = _request("POST", "api/connections", {"link": link, "trust": cert["fingerprint"]})
        trusted = {"host": cert["host"], "fingerprint": cert["fingerprint"]}
    result = {
        "key": c.get("key"),
        "kind": c.get("kind"),
        "name": c.get("name") or c.get("id"),
        "link": c.get("link"),
    }
    if trusted:
        result["trusted_certificate"] = trusted
    if c.get("kind") == "share" and c.get("key"):
        # read through the lab, which holds the connection's password and
        # certificate; the names are a courtesy, so a failed read omits them
        try:
            manifest = _request("GET", _connection(c["key"], "manifest"))
            result["entries"] = [e.get("name") for e in manifest.get("entries", [])]
        except RuntimeError:
            pass
    return result


def disconnect(key: str) -> dict:
    """Remove a connection by its `key`."""
    return _request("DELETE", _connection(key))


def close_share(share_id: str) -> dict:
    """Delete one of your own shares by id."""
    return _request("DELETE", f"api/shares/{share_id}")


def close_request(request_id: str) -> dict:
    """Delete one of your own requests by id, with its uploaded files."""
    return _request("DELETE", f"api/requests/{request_id}")


def add_files(share_id: str, paths: list[str]) -> dict:
    """Add workspace-relative paths to one of your shares. The files are copied
    into the share's isolated pool, so later edits to the source do not change
    what recipients download."""
    return _request("POST", f"api/shares/{share_id}/items", {"paths": paths})


def remove_files(share_id: str, names: list[str]) -> dict:
    """Remove top-level entries from one of your shares by name. Names are the
    entries listed in the share manifest, not workspace paths."""
    query = urllib.parse.urlencode([("name", n) for n in names])
    return _request("DELETE", f"api/shares/{share_id}/items?{query}")


def remove_upload(request_id: str, uploader: str, name: str) -> dict:
    """Remove a single uploaded file from one of your requests, identified by
    its uploader hash and file name (both shown by list-request-uploads)."""
    query = urllib.parse.urlencode({"uploader": uploader, "name": name})
    return _request("DELETE", f"api/requests/{request_id}/uploads?{query}")


def pick_up(
    key: str, names: Optional[list[str]] = None, target_dir: str = ""
) -> dict:
    """Pick up (download) files from a connected SHARE into the workspace.
    `names` selects specific top-level entries (omit for all); `target_dir` is
    a workspace-relative destination (default: root)."""
    body: dict = {"target_dir": target_dir}
    if names is not None:
        body["names"] = names
    return _request("POST", _connection(key, "save"), body)


def send_to_request(key: str, paths: list[str], uploader: str = "") -> dict:
    """Send (upload) workspace files to a connected REQUEST. `uploader` is an
    optional label shown to the request's owner."""
    return _request(
        "POST",
        _connection(key, "upload"),
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
                "hash": u.get("hash"),
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
    "connect": lambda a: connect(a.link, a.trust_certificate),
    "disconnect": lambda a: disconnect(a.key),
    "close-share": lambda a: close_share(a.id),
    "close-request": lambda a: close_request(a.id),
    "add-files": lambda a: add_files(a.id, a.paths),
    "remove-files": lambda a: remove_files(a.id, a.names),
    "remove-upload": lambda a: remove_upload(a.id, a.uploader, a.name),
    "pick-up": lambda a: pick_up(a.key, a.names or None, a.target_dir),
    "send-to-request": lambda a: send_to_request(a.key, a.paths, a.uploader),
    "list-request-uploads": lambda a: list_request_uploads(a.id),
    "cloudflare": _cmd_cloudflare,
}


_DESCRIPTION = """\
jupyterlab_share_files - shares, file requests and connections of the Share Files
JupyterLab extension, from a terminal.

A share is a set of files copied from the workspace and read through its link. A request
is an inbox that others upload files into through its link. A connection is someone
else's share or request link, kept in the panel. Your own shares and requests are
addressed by id, connections by key; `list-items` prints both.

Every command except `cloudflare` calls the extension's authenticated API on the Jupyter
server - the calls the panel makes. The server runs in one of two modes, and several
commands differ between them; each command's --help says how:

  standalone  the lab keeps the records and the files
  hub         the server runs with SHARE_FILES_PUBLIC_ZONE=hub: the JupyterHub keeps the
              records and copies the files after the command returns

Whatever reads stdout receives what a command prints. A link is the credential of its
record: anyone holding it reaches the record, with the password when one is set.
"""

_EPILOG = """\
Every command has its own --help with examples: `jupyterlab_share_files connect --help`.
--json goes before the command: `jupyterlab_share_files --json list-items`.

exit status:
  0  done; the result is on stdout
  1  refused, or the server could not be reached; the reason is on stderr, stdout is empty
  2  an argument the parser rejects
  An unexpected failure, such as a config file that cannot be written, prints a traceback.

environment:
  SHARE_FILES_BASE_URL  the Jupyter server URL; falls back to JUPYTER_SERVER_URL. On
                        JupyterHub give the public user URL
                        (https://hub.example.com/user/<name>/), so links carry the public host
  SHARE_FILES_TOKEN     the API token; falls back to JUPYTERHUB_API_TOKEN, then
                        JUPYTER_TOKEN. Never print it
  SHARE_FILES_INSECURE  1, true or yes: the CLI does not verify the server's certificate
                        (a self-signed one). How the server checks other hosts stays as it is
  NO_COLOR              plain output on a terminal; output into a pipe is always plain
"""

_PASSWORD_NOTE = (
    "The output holds the password. It is for the recipient: give it to the user once and\n"
    "do not write it into a file, a commit or a log."
)

_CLOUDFLARE_DESCRIPTION = """\
serve the share and request links on a public hostname through a Cloudflare tunnel.
Standalone only: in hub mode the hub owns the tunnel, these commands do not change hub
links, and the panel's cloud icon switches the hub's tunnel.

The commands run inside the CLI process and read and write
$XDG_CONFIG_HOME/jupyterlab-share-files/config.json (default ~/.config/..., mode 0600).
Run the CLI as the user and on the machine of the Jupyter server, so both read one file.

  setup     save the credentials and provision everything: the tunnel, a proxied DNS
            record, HTTPS enforcement, links rewritten to the public host, and the
            cloudflared connector. Only the extension's unauthenticated /public/
            endpoints pass through the tunnel
  validate  check the saved config end to end; prints one field per check
            (config_complete, tunnel_exists, dns_record_ok, ...) and exits 0 when a check
            fails: read the fields
  info      the configuration with tokens masked to their last 4 characters, plus
            tunnel_active, daemon_running and tunnel_status
  start     public links: mark the tunnel active and start the connector. A connector
            that does not come up does not fail start: check daemon_running in info and
            read /tmp/cloudflared-share-files.log
  stop      private links: stop the connector; setup and credentials stay. It ends every
            process on the machine whose command line holds `cloudflared tunnel run`
  reset     clear the saved token, account id, tunnel state and public URL. The tunnel
            and the DNS record on Cloudflare stay. Cannot be undone: ask the user first

The API token is a secret. Given literally it lands in the transcript; inside a command
substitution only the command that printed it does. Either way it shows in the process
list while setup runs, so after the first setup saved it, run setup without --token.
"""

_CLOUDFLARE_EPILOG = """\
examples:
  jupyterlab_share_files cloudflare setup \\
      --token "$(<command that prints the token>)" --account-id <account-id> \\
      --hostname share.example.com \\
      --private-base-url https://hub.example.com/user/<name>/

  jupyterlab_share_files cloudflare validate
  jupyterlab_share_files --json cloudflare info
  jupyterlab_share_files cloudflare start
  jupyterlab_share_files cloudflare stop

full guide: docs/cloudflare_setup.md
"""


def _command(sub: Any, name: str, summary: str, description: str, examples: str) -> argparse.ArgumentParser:
    """A subcommand whose --help carries what it does, what it prints and
    worked examples: the help is what an agent runs the command from."""
    return sub.add_parser(
        name,
        help=summary,
        description=description,
        epilog="examples:\n" + examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jupyterlab_share_files",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine-readable JSON output instead of the human-readable form",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>", title="commands")

    _command(
        sub, "list-items", "list your shares, requests and connections",
        "print your shares (id, name, link, entries), your requests (id, name, link,\n"
        "upload_count) and your connections (key, kind, name, link). The listing names every\n"
        "entry of every share: use --json and keep the fields you need. Nothing is printed\n"
        "when one of the three reads fails.",
        "  jupyterlab_share_files list-items\n"
        "  jupyterlab_share_files --json list-items | python3 -c \\\n"
        "    \"import sys, json; print([(s['id'], s['name']) for s in json.load(sys.stdin)['shares']])\"",
    )

    p = _command(
        sub, "create-share", "create a share from workspace paths",
        "copy files and folders of the workspace into a new share and print id, name, link,\n"
        "and password when one is set. Later edits to the sources do not reach the share.\n"
        + _PASSWORD_NOTE + " --password also shows in the\nprocess list; --generate-password does not.\n\n"
        "standalone: no paths makes an empty share; a second source with a name already taken\n"
        "is kept as <name>-2.\n"
        "hub: the command returns when the hub accepts the work, and the hub copies the files\n"
        "afterwards. An entry shows in list-items once it is copied; a refused copy shows only\n"
        "in the panel. A path that leaves the workspace through a link is refused.",
        "  jupyterlab_share_files create-share report data/a.csv notes.md --generate-password\n"
        "  jupyterlab_share_files create-share docs project/docs",
    )
    p.add_argument("name", help="the share's name, shown to its recipients")
    p.add_argument("paths", nargs="*", default=[], help="workspace-relative files and folders, without ..")
    p.add_argument("--password", default="", help="protect public access with this password")
    p.add_argument(
        "--generate-password",
        action="store_true",
        help="protect public access with a generated xkcd-style passphrase",
    )

    p = _command(
        sub, "create-request", "create a file request (inbox)",
        "create a request - an inbox that others upload files into through its link - and\n"
        "print id, name, link, and password when one is set.\n"
        + _PASSWORD_NOTE + " --password also shows in the\nprocess list; --generate-password does not.",
        "  jupyterlab_share_files create-request submissions\n"
        "  jupyterlab_share_files create-request submissions --generate-password",
    )
    p.add_argument("name", help="the request's name, shown to its uploaders")
    p.add_argument("--password", default="", help="protect public access with this password")
    p.add_argument(
        "--generate-password",
        action="store_true",
        help="protect public access with a generated xkcd-style passphrase",
    )

    p = _command(
        sub, "set-password", "set, change, or clear the password of a share or request",
        "set, change or clear the password of one of your shares or requests and print id,\n"
        "name, has_password and password. With neither PASSWORD nor --clear, a passphrase is\n"
        "generated: four words joined by '-', made on the Jupyter server with xkcdpass.\n"
        + _PASSWORD_NOTE + " A PASSWORD argument also\nshows in the process list.\n\n"
        "hub: name reads None.",
        "  jupyterlab_share_files set-password share AB23CD45 --generate\n"
        "  jupyterlab_share_files set-password request RQ77ZZ12 --clear",
    )
    p.add_argument("kind", choices=("share", "request"), help="what the id names")
    p.add_argument("id", help="the id of your share or request, as list-items prints it")
    p.add_argument("password", nargs="?", default="", help="the new password; omit it to generate one")
    p.add_argument("--generate", action="store_true", help="generate an xkcd-style passphrase")
    p.add_argument("--clear", action="store_true", help="remove the password")

    _command(
        sub, "generate-password", "generate an xkcd-style passphrase (xkcdpass)",
        "print a passphrase of four words joined by '-', made on the Jupyter server with\n"
        "xkcdpass. It is not applied to anything: pass it on with --password or set-password.\n"
        "The output is a password: keep it out of files, commits and logs.",
        "  jupyterlab_share_files generate-password",
    )

    p = _command(
        sub, "connect", "connect to a share or request link",
        "connect to someone else's share or request link and print key, kind, name, link, and\n"
        "for a share its entries. The key addresses the connection in pick-up, send-to-request\n"
        "and disconnect.\n\n"
        "The CLI cannot send a password. A protected link is refused with 'password required':\n"
        "the user connects it once in the panel, and its key then shows in list-items.\n\n"
        "A host whose certificate the system does not trust stops the command, which prints\n"
        "the certificate's SHA-256 fingerprint. Show the user the host and the fingerprint and\n"
        "add --trust-certificate only on their word: the connection then keeps that certificate\n"
        "and prints trusted_certificate.\n\n"
        "hub: a link on the host of SHARE_FILES_BASE_URL is this hub's own and is read inside\n"
        "the hub network, with no certificate step; any other link is read as given.",
        "  jupyterlab_share_files connect https://host/jupyterlab-share-files-extension/public/share/AB23CD45\n"
        "  jupyterlab_share_files connect https://hub.example.com/s/<policy>/<id>",
    )
    p.add_argument("link", help="the whole link of the share or request")
    p.add_argument(
        "--trust-certificate",
        action="store_true",
        help="trust the certificate the link's host presents when the system does not",
    )

    p = _command(
        sub, "disconnect", "remove a connection by key",
        "remove a connection and print ok: true - also for a key that does not exist. The\n"
        "record stays with its owner.\n\n"
        "hub: an upload still running into the connection stops at its next file.",
        "  jupyterlab_share_files disconnect 'share:https://host:AB23CD45'",
    )
    p.add_argument("key", help="the connection's key, as list-items prints it; quote it, it holds : and /")

    p = _command(
        sub, "close-share", "delete one of your shares by id",
        "delete one of your shares and print ok: true. Its link stops working and its copied\n"
        "files are removed; the sources in the workspace stay. Cannot be undone: ask the user\n"
        "first.",
        "  jupyterlab_share_files close-share AB23CD45",
    )
    p.add_argument("id", help="the share's id, as list-items prints it")

    p = _command(
        sub, "close-request", "delete one of your requests by id",
        "delete one of your requests, with every file uploaded into it, and print ok: true.\n"
        "Cannot be undone: ask the user first.",
        "  jupyterlab_share_files close-request RQ77ZZ12",
    )
    p.add_argument("id", help="the request's id, as list-items prints it")

    p = _command(
        sub, "add-files", "add workspace paths to one of your shares",
        "copy more files and folders of the workspace into one of your shares.\n\n"
        "standalone: a name already taken is kept as <name>-2, and the output is the share's\n"
        "whole record.\n"
        "hub: the output is ok: true at once, and the hub copies the files afterwards; an\n"
        "entry shows in list-items once it is copied. Two chosen items with one name, or a\n"
        "name the share already holds, are refused.",
        "  jupyterlab_share_files add-files AB23CD45 extra/diagram.png changelog.md",
    )
    p.add_argument("id", help="the share's id, as list-items prints it")
    p.add_argument("paths", nargs="+", help="workspace-relative files and folders, without ..")

    p = _command(
        sub, "remove-files", "remove entries from one of your shares by name",
        "remove entries from one of your shares. The share's copy is deleted and cannot be\n"
        "brought back; the sources in the workspace stay. Ask the user first.\n\n"
        "standalone: a name the share does not hold is skipped without an error.\n"
        "hub: the names are removed one at a time, and a failure leaves the names before it\n"
        "removed.",
        "  jupyterlab_share_files remove-files AB23CD45 diagram.png",
    )
    p.add_argument("id", help="the share's id, as list-items prints it")
    p.add_argument("names", nargs="+", help="entry names as list-items prints them, not workspace paths")

    p = _command(
        sub, "remove-upload", "remove an uploaded file from one of your requests",
        "remove one top-level item one uploader sent into one of your requests. Cannot be\n"
        "undone: ask the user first.\n\n"
        "standalone only: a hub server has no such route and refuses the command.",
        "  jupyterlab_share_files list-request-uploads RQ77ZZ12\n"
        "  jupyterlab_share_files remove-upload RQ77ZZ12 K3J5H2 draft.pdf",
    )
    p.add_argument("id", help="the request's id, as list-items prints it")
    p.add_argument("uploader", help="uploader hash (see list-request-uploads)")
    p.add_argument("name", help="the item's name (see list-request-uploads)")

    p = _command(
        sub, "pick-up", "save files from a connected share",
        "save files of a connected share into the workspace and print saved, the workspace\n"
        "paths written. With no names the whole share lands in one folder named after the\n"
        "share. A name already taken gets -2. The limit is 10 GB unpacked.\n\n"
        "standalone: a --target-dir that does not exist is created.\n"
        "hub: a --target-dir that does not exist is refused with 'Not a folder'.",
        "  jupyterlab_share_files pick-up 'share:https://host:AB23CD45'\n"
        "  jupyterlab_share_files pick-up 'share:hub:q7Xk2m' a.csv --target-dir inbox",
    )
    p.add_argument("key", help="the connection's key, as list-items prints it; quote it, it holds : and /")
    p.add_argument("names", nargs="*", default=[], help="top-level entries to save (default: all)")
    p.add_argument("--target-dir", default="", help="workspace-relative folder to save into (default: the workspace root)")

    p = _command(
        sub, "send-to-request", "upload files to a connected request",
        "upload files of the workspace into a connected request.\n\n"
        "standalone: sends file by file and prints uploaded; a path that fails leaves the\n"
        "files before it sent.\n"
        "hub: prints the given names at once and uploads in the background; exit 0 does not\n"
        "say the files arrived, and no command reports how the upload ended - the panel does.\n"
        "--uploader is ignored, folders are flattened to their files, and a second send into\n"
        "the same request while one runs is refused: wait, then send again.",
        "  jupyterlab_share_files send-to-request 'request:hub:q7Xk2m' report.pdf\n"
        "  jupyterlab_share_files send-to-request 'request:https://host:RQ77ZZ12' out/ --uploader Alice",
    )
    p.add_argument("key", help="the connection's key, as list-items prints it; quote it, it holds : and /")
    p.add_argument("paths", nargs="+", help="workspace-relative files and folders, without ..")
    p.add_argument("--uploader", default="", help="a label the request's owner sees (default: anonymous)")

    p = _command(
        sub, "list-request-uploads", "list files uploaded to one of your requests",
        "print the files uploaded into one of your requests, grouped by uploader: hash, name,\n"
        "and each file's name, path and type. The listing names every file: use --json and\n"
        "keep the fields you need.\n\n"
        "hub: all files are in one group named Uploads, with no path.",
        "  jupyterlab_share_files list-request-uploads RQ77ZZ12",
    )
    p.add_argument("id", help="the request's id, as list-items prints it")

    p = sub.add_parser(
        "cloudflare",
        help="Cloudflare tunnel sharing: setup, validate, info, start, stop, reset",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=_CLOUDFLARE_DESCRIPTION,
        epilog=_CLOUDFLARE_EPILOG,
    )
    cf = p.add_subparsers(
        dest="cf_command", required=True, metavar="<subcommand>", title="subcommands",
        help="each has its own --help",
    )

    ps = _command(
        cf, "setup",
        "save credentials and provision the tunnel end to end "
        "(tunnel + DNS + HTTPS enforcement + link rewriting + connector)",
        "save the token and the account id, then create or reuse the tunnel, route the\n"
        "hostname with a proxied DNS record, enforce HTTPS, rewrite the links to the public\n"
        "host and start the connector. The token needs Account > Cloudflare Tunnel > Edit and\n"
        "Zone > DNS > Edit. It is a secret and shows in the process list while setup runs:\n"
        "pass it from a command substitution, and omit --token once it is saved.",
        "  jupyterlab_share_files cloudflare setup \\\n"
        "      --token \"$(<command that prints the token>)\" --account-id <account-id> \\\n"
        "      --hostname share.example.com \\\n"
        "      --private-base-url https://hub.example.com/user/<name>/",
    )
    ps.add_argument("--token", default="", help="Cloudflare API token to save (default: the saved one)")
    ps.add_argument(
        "--account-id", dest="account_id", default="",
        help="Cloudflare account id to save (default: the saved one, else the first account the token lists)",
    )
    ps.add_argument(
        "--hostname", default="share.duoptimum.com",
        help="the public hostname the links are served on; always pass it",
    )
    ps.add_argument(
        "--private-base-url",
        required=True,
        help="REQUIRED: this server's URL as the cloudflared connector "
        "reaches it (e.g. https://hub.example.com/user/<name>/ - given "
        "explicitly, never inferred, https only)",
    )

    _command(
        cf, "validate",
        "end-to-end check of the saved config: token validity, bind to "
        "existing tunnels, create rights (proven by creating a test tunnel "
        "and removing it)",
        "check the saved config end to end and print one field per check (config_complete,\n"
        "tunnel_exists, dns_record_ok, ...). The command exits 0 when a check fails: read\n"
        "the fields.",
        "  jupyterlab_share_files --json cloudflare validate",
    )

    _command(
        cf, "info",
        "show the current Cloudflare configuration (tokens masked to "
        "their last 4 characters) and whether the connector is running",
        "print the configuration with tokens masked to their last 4 characters, plus\n"
        "tunnel_active, daemon_running and tunnel_status. A Cloudflare API failure shows as\n"
        "tunnel_status: unknown (...). Any process whose command line holds\n"
        "`cloudflared tunnel run` counts as this tunnel's connector.",
        "  jupyterlab_share_files --json cloudflare info",
    )

    _command(
        cf, "start",
        "switch to public links: mark the tunnel active and start the "
        "cloudflared daemon",
        "mark the tunnel active and start the connector: links carry the public hostname.\n"
        "A connector that does not come up does not fail the command: check daemon_running\n"
        "in `cloudflare info` and read /tmp/cloudflared-share-files.log.",
        "  jupyterlab_share_files cloudflare start",
    )

    _command(
        cf, "stop",
        "switch to private links: stop the cloudflared daemon; setup "
        "and credentials are kept",
        "stop the connector: links revert to the local or hub address at the next request.\n"
        "Setup and credentials stay. It ends every process on the machine whose command line\n"
        "holds `cloudflared tunnel run`.",
        "  jupyterlab_share_files cloudflare stop",
    )

    _command(
        cf, "reset",
        "reset the saved Cloudflare token to none (also clears account "
        "id, tunnel state and public_base_url; Cloudflare-side resources are "
        "kept)",
        "clear the saved token, the account id, the tunnel state and the public URL: links\n"
        "revert to the local or hub address. The tunnel and the DNS record on Cloudflare\n"
        "stay. Cannot be undone: ask the user first.",
        "  jupyterlab_share_files cloudflare reset",
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
