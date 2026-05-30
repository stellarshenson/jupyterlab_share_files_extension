"""MCP server for the Share Files JupyterLab extension.

Exposes the extension's capability to an AI agent over the Model Context
Protocol (stdio). The server is a thin client over the extension's existing
authenticated HTTP API (``{base}/jupyterlab-share-files-extension/api/*``) - the
same endpoints the JupyterLab panel uses - so link generation, connecting to
peers, server-side save, upload and delete all behave identically.

It acts as a single user, authenticating with that user's Jupyter / JupyterHub
token. Configuration comes from the environment:

- ``SHARE_FILES_BASE_URL`` (preferred) or ``JUPYTER_SERVER_URL`` - the base URL
  of the Jupyter server running the extension. On JupyterHub this MUST be the
  public user URL (e.g. ``https://hub.example.com/user/<name>/``) so that share
  links the server generates carry the public host and ``/user/<name>/`` prefix
  (the extension derives links from the incoming request's forwarded headers).
- ``SHARE_FILES_TOKEN`` (preferred), ``JUPYTERHUB_API_TOKEN`` or
  ``JUPYTER_TOKEN`` - the API token used for ``Authorization: token <token>``.
- ``SHARE_FILES_INSECURE`` - set to ``1`` to skip TLS verification (self-signed
  certificates). Off by default.

Run it with the console script ``jupyterlab-share-files-mcp``.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

NAMESPACE = "jupyterlab-share-files-extension"
SERVER_NAME = "jupyterlab-share-files"

# --------------------------------------------------------------------------- #
# Configuration (read from the environment at call time)
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


# --------------------------------------------------------------------------- #
# HTTP helper (stdlib urllib - no third-party HTTP dependency)
# --------------------------------------------------------------------------- #


def _request(method: str, endpoint: str, body: Optional[dict] = None) -> Any:
    """Call the extension API and return parsed JSON.

    Raises RuntimeError with the server's error message on a non-2xx response,
    so the agent gets an actionable explanation rather than a raw traceback.
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
# Tool implementations (plain functions - registered with FastMCP in main())
# --------------------------------------------------------------------------- #


def list_items() -> dict:
    """List everything in your Share Files panel: your shares, your requests
    (with how many files have been uploaded to each), and your connections to
    other people's shares/requests. Use this to discover ids, keys and links."""
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
    """Create a named share (a read-only drop) from one or more files/folders in
    your workspace and return a shareable link. `paths` are workspace-relative.
    Recipients open the link in a browser or paste it into their panel."""
    s = _request("POST", "api/shares", {"name": name, "paths": paths})
    return {"id": s.get("id"), "name": s.get("name"), "link": s.get("link")}


def create_request(name: str) -> dict:
    """Create a named file request (an inbox) and return a link. Anyone with the
    link can upload files to you; the files land in your workspace."""
    r = _request("POST", "api/requests", {"name": name})
    return {"id": r.get("id"), "name": r.get("name"), "link": r.get("link")}


def connect(link: str) -> dict:
    """Connect to someone else's share or request link so you can pick up files
    from it (share) or send files to it (request). Returns the connection `key`
    used by other tools, plus - for a share - the list of available entry names."""
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
    """Remove a connection by its `key` (from list_items or connect)."""
    return _request("DELETE", f"api/connections/{key}")


def close_share(share_id: str) -> dict:
    """Delete one of your own shares by id. The shared files are removed."""
    return _request("DELETE", f"api/shares/{share_id}")


def close_request(request_id: str) -> dict:
    """Delete one of your own requests by id, along with its uploaded files."""
    return _request("DELETE", f"api/requests/{request_id}")


def pick_up(
    key: str, names: Optional[list[str]] = None, target_dir: str = ""
) -> dict:
    """Pick up (download) files from a connected SHARE into your workspace. Pass
    the connection `key`; `names` selects specific top-level entries (omit for
    all). `target_dir` is a workspace-relative destination (default: root).
    Folders are downloaded and extracted. Returns the saved workspace paths."""
    body: dict = {"target_dir": target_dir}
    if names is not None:
        body["names"] = names
    return _request("POST", f"api/connections/{key}/save", body)


def send_to_request(key: str, paths: list[str], uploader: str = "") -> dict:
    """Send (upload) files from your workspace to a connected REQUEST. Pass the
    connection `key` and workspace-relative `paths`. `uploader` is an optional
    label shown to the request's owner."""
    return _request(
        "POST",
        f"api/connections/{key}/upload",
        {"paths": paths, "uploader": uploader},
    )


def list_request_uploads(request_id: str) -> dict:
    """List the files other people have uploaded to one of your requests, grouped
    by uploader, with each file's workspace-relative path so you can read them."""
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


_TOOLS = [
    list_items,
    create_share,
    create_request,
    connect,
    disconnect,
    close_share,
    close_request,
    pick_up,
    send_to_request,
    list_request_uploads,
]


def build_server():
    """Construct the FastMCP server with all tools registered.

    The `mcp` import is deferred to here so the module (and its unit tests) load
    without the optional runtime dependency installed.
    """
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(SERVER_NAME)
    for fn in _TOOLS:
        server.add_tool(fn)
    return server


def main() -> None:
    """Console entry point: run the MCP server over stdio."""
    # Surface a clear hint (to stderr - stdout is the protocol channel) when the
    # base URL looks internal, since generated links would not be shareable.
    base = os.environ.get("SHARE_FILES_BASE_URL") or os.environ.get("JUPYTER_SERVER_URL", "")
    if base and not os.environ.get("SHARE_FILES_BASE_URL"):
        print(
            "share-files-mcp: SHARE_FILES_BASE_URL not set; using JUPYTER_SERVER_URL "
            f"({base}). On JupyterHub set SHARE_FILES_BASE_URL to your public user "
            "URL so created links are shareable.",
            file=sys.stderr,
        )
    build_server().run()


if __name__ == "__main__":
    main()
