"""Tornado handlers for the share-files extension.

Two handler groups:
  * api/*       - authenticated, used by the side panel
  * public/*    - unauthenticated, used by standalone HTML pages and by
                  remote JupyterLab instances that have connected to a link

The public endpoints rely on the share/request ID being secret (8-char base32).
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tornado
import tornado.httpclient
import tornado.web
from jupyter_server.base.handlers import APIHandler
from jupyter_server.utils import url_path_join
from tornado.web import StaticFileHandler

from .config import ShareFilesConfig
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
    resolve_shares_dir,
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
        # Allow cross-origin GETs of manifests and downloads so other peers'
        # JupyterLab panels can fetch directly from this server.
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")

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


def _public_origin(handler: tornado.web.RequestHandler) -> str:
    """Pick scheme + host the browser actually sees.

    Behind a TLS-terminating proxy (JupyterHub, Traefik, nginx) Tornado's
    ``request.protocol`` reports ``http`` unless ``trust_xheaders`` is enabled
    on the server. Honour ``X-Forwarded-Proto`` / ``X-Forwarded-Host``
    explicitly so HTTPS-facing sessions emit HTTPS links, while plain p2p
    sessions stay on HTTP.
    """
    proto = handler.request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    if not proto:
        proto = handler.request.protocol
    host = handler.request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    if not host:
        host = handler.request.host
    return proto + "://" + host


def _public_share_url(handler: tornado.web.RequestHandler, id_: str) -> str:
    """Build an absolute URL the share's public page is reachable at."""
    path = url_path_join(handler.settings.get("base_url", "/"), EXTENSION_NAMESPACE, "public", "share", id_)
    return _public_origin(handler) + path


def _public_request_url(handler: tornado.web.RequestHandler, id_: str) -> str:
    path = url_path_join(handler.settings.get("base_url", "/"), EXTENSION_NAMESPACE, "public", "request", id_)
    return _public_origin(handler) + path


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
        })


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
        if not name:
            return self.write_error_json(400, "Missing 'name'")
        if not isinstance(paths, list):
            return self.write_error_json(400, "'paths' must be a list")
        try:
            manifest = self.share_store.create(name, paths)
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
        if not name:
            return self.write_error_json(400, "Missing 'name'")
        manifest = self.request_store.create(name)
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
    def post(self):
        """Add a connection.

        Body: { "link": "https://host/.../public/share/<id>" }
        We parse the link, fetch its manifest, and persist a connection entry.
        """
        body = self.get_json_body() or {}
        link = (body.get("link") or "").strip()
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
        own_base_url = self.settings.get("base_url", "/")
        if not own_base_url.endswith("/"):
            own_base_url += "/"
        own_prefix = (
            self.request.protocol + "://" + self.request.host + own_base_url
        )
        link_prefix = parsed["host"] + parsed.get("base_path", "/")
        if link_prefix == own_prefix:
            return self.write_error_json(
                400,
                "That link points to your own server - it's already in your panel.",
            )
        entry = self.connection_store.add(
            kind=parsed["kind"],
            id_=parsed["id"],
            host=parsed["host"],
            name=parsed.get("name", ""),
            owner=parsed.get("owner", ""),
        )
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
        base_url = self.settings.get("base_url", "/")
        api_base = host + url_path_join(base_url, EXTENSION_NAMESPACE, "public", "share", share_id)

        client = tornado.httpclient.AsyncHTTPClient()

        # Resolve share name for the wrapping folder when saving all
        manifest_url = api_base + "/manifest"
        manifest_resp = await client.fetch(manifest_url, raise_error=False)
        if manifest_resp.code != 200:
            return self.write_error_json(502, f"Remote unavailable ({manifest_resp.code})")
        manifest = json.loads(manifest_resp.body)
        share_slug = manifest.get("slug") or share_id

        saved: list[str] = []

        if names is None:
            # Save All - download zip, extract into <dest_root>/<share-slug>/
            zip_resp = await client.fetch(api_base + "/download-all", raise_error=False)
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
                resp = await client.fetch(url, raise_error=False)
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
        base_url = self.settings.get("base_url", "/")
        upload_url = host + url_path_join(
            base_url, EXTENSION_NAMESPACE, "public", "request", request_id, "upload"
        ) + "?uploader=" + tornado.escape.url_escape(uploader)

        client = tornado.httpclient.AsyncHTTPClient()
        ws_root = Path(self.workspace_root).resolve()
        sent: list[str] = []

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
                        await _post_file(client, upload_url, rel_name.replace(os.sep, "/"), data)
                        sent.append(rel_name)
            else:
                with open(src, "rb") as f:
                    data = f.read()
                await _post_file(client, upload_url, src.name, data)
                sent.append(src.name)

        self.write_json({"ok": True, "uploaded": sent})


async def _post_file(client, url: str, filename: str, data: bytes) -> None:
    """Send a single file as multipart/form-data POST."""
    boundary = "----shareFilesBoundary" + os.urandom(8).hex()
    body_parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(body_parts)
    resp = await client.fetch(
        url,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        body=body,
        raise_error=False,
    )
    if resp.code >= 400:
        raise StorageError(f"Upload failed: {resp.code}")


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


class PublicSharePageHandler(_PublicBase):
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
        })
        html = html.replace("__CONTEXT__", ctx)
        self.set_header("Content-Type", "text/html")
        self.finish(html)


class PublicRequestPageHandler(_PublicBase):
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
        })
        html = html.replace("__CONTEXT__", ctx)
        self.set_header("Content-Type", "text/html")
        self.finish(html)


class PublicShareManifestHandler(_PublicBase):
    def get(self, id_):
        try:
            manifest = self.share_store.get(id_)
        except NotFoundError:
            self.set_status(404)
            self.finish(json.dumps({"error": "not found"}))
            return
        manifest["link"] = _public_share_url(self, id_)
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(manifest))


class PublicRequestManifestHandler(_PublicBase):
    def get(self, id_):
        try:
            manifest = self.request_store.get(id_)
        except NotFoundError:
            self.set_status(404)
            self.finish(json.dumps({"error": "not found"}))
            return
        manifest["link"] = _public_request_url(self, id_)
        # don't expose upload contents publicly; keep counts but strip names
        manifest["uploaders"] = []
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(manifest))


class PublicShareDownloadHandler(_PublicBase):
    def get(self, id_, sub_path):
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


class PublicRequestUploadHandler(_PublicBase):
    def post(self, id_):
        if not self.request_store.exists(id_):
            self.set_status(404)
            self.finish(json.dumps({"error": "not found"}))
            return
        uploader = self.get_argument("uploader", default="anonymous")
        files = self.request.files.get("file") or self.request.files.get("files") or []
        if not files:
            self.set_status(400)
            self.finish(json.dumps({"error": "no file"}))
            return
        for f in files:
            filename = f.get("filename") or "upload.bin"
            data = f.get("body") or b""
            try:
                self.request_store.add_upload(id_, uploader, filename, data)
            except StorageError as exc:
                self.set_status(400)
                self.finish(json.dumps({"error": str(exc)}))
                return
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps({"ok": True, "count": len(files)}))


# --------------------------------------------------------------------------- #
# Route registration
# --------------------------------------------------------------------------- #


def setup_route_handlers(web_app, config: ShareFilesConfig | None = None):
    host_pattern = ".*$"
    base_url = web_app.settings["base_url"]
    ns = EXTENSION_NAMESPACE
    # store config in settings so handlers can read it
    web_app.settings["share_files_config"] = config or ShareFilesConfig()

    static_path = os.path.join(os.path.dirname(__file__), "static")

    handlers = [
        # api/info
        (url_path_join(base_url, ns, "api", "info"), InfoHandler),
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
