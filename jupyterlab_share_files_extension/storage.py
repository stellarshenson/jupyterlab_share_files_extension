"""Storage layer for shares, requests, and connections.

Layout under workspace root (.jupyterlab_shares/):
    shares/<id>/manifest.json
    shares/<id>/data/<files and folders>
    requests/<id>/manifest.json
    requests/<id>/uploads/<uploader>/<files and folders>
    connections.json
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

try:
    from send2trash import send2trash as _send2trash
except ImportError:  # pragma: no cover - send2trash is a hard dep, this is a safety net
    _send2trash = None


def _remove(target: Path, use_trash: bool) -> None:
    """Delete `target` permanently, or move it to the OS trash when use_trash.

    Falls back to permanent delete if send2trash is unavailable or fails (e.g.
    target lives on a filesystem with no trash mount, common in containers).
    """
    if use_trash and _send2trash is not None:
        try:
            _send2trash(str(target))
            return
        except Exception:
            pass  # fall through to permanent delete
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()


SHARES_DIR_NAME = "uploads"
TOKEN_BYTES = 5  # 5 bytes => 8 chars base32 (e.g. "A3KM7X2P")


class StorageError(Exception):
    """Generic storage failure."""


class NotFoundError(StorageError):
    """Requested share/request/connection not found."""


def generate_token() -> str:
    """Return an 8-char base32 random token."""
    raw = secrets.token_bytes(TOKEN_BYTES)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _safe_name(name: str) -> str:
    """Sanitise a user-supplied name to a filesystem-safe slug.

    Keeps alphanumerics, hyphens, underscores, dots. Replaces runs of other
    characters with a single hyphen. Empty results fall back to 'unnamed'.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-")
    return cleaned or "unnamed"


def _now() -> int:
    return int(time.time())


def _is_safe_relative(path: str) -> bool:
    """Reject paths that escape the workspace via .. or absolute paths."""
    if not path:
        return False
    normalised = os.path.normpath(path)
    if normalised.startswith(".."):
        return False
    if os.path.isabs(normalised):
        return False
    parts = normalised.split(os.sep)
    return ".." not in parts


def _list_entries(directory: Path, workspace_root: Path | None = None) -> list[dict[str, Any]]:
    """Return [{name, type, size, path?}] for the top-level items in directory.

    When `workspace_root` is provided and the entry lies inside it, the
    workspace-relative `path` is added so the frontend can pass it to
    JupyterLab's Contents API (e.g. for copy-to-file-browser).
    """
    if not directory.exists():
        return []
    entries = []
    for child in sorted(directory.iterdir()):
        entry: dict[str, Any] = {"name": child.name}
        if child.is_dir():
            entry["type"] = "directory"
            entry["size"] = _dir_size(child)
        else:
            entry["type"] = "file"
            entry["size"] = child.stat().st_size
        if workspace_root is not None:
            try:
                rel = child.resolve().relative_to(workspace_root)
                entry["path"] = str(rel).replace(os.sep, "/")
            except ValueError:
                pass
        entries.append(entry)
    return entries


def _dir_size(directory: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(directory):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _resolve_unique_target(target_dir: Path, name: str) -> Path:
    """Return a non-colliding target path - appends -2, -3 if needed."""
    candidate = target_dir / name
    if not candidate.exists():
        return candidate
    stem, dot, ext = name.rpartition(".")
    if dot and stem:
        base, suffix = stem, f".{ext}"
    else:
        base, suffix = name, ""
    counter = 2
    while True:
        candidate = target_dir / f"{base}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _copy_into(source: Path, target_dir: Path) -> Path:
    """Copy source (file or directory) into target_dir, return the destination."""
    if not source.exists():
        raise NotFoundError(f"Source not found: {source}")
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = _resolve_unique_target(target_dir, source.name)
    if source.is_dir():
        shutil.copytree(source, dest)
    else:
        shutil.copy2(source, dest)
    return dest


def resolve_shares_dir(workspace_root: str, configured: str = "") -> Path:
    """Resolve the directory where shares/requests/connections are stored.

    Empty `configured` defaults to `<workspace_root>/.jupyterlab_shares/`.
    Relative paths are resolved against `workspace_root`. Absolute paths are
    used as-is.
    """
    ws = Path(os.path.expanduser(workspace_root)).resolve()
    if not configured:
        return ws / SHARES_DIR_NAME
    configured = os.path.expanduser(configured)
    if os.path.isabs(configured):
        return Path(configured).resolve()
    return (ws / configured).resolve()


class BaseStore:
    """Common helpers for ShareStore and RequestStore."""

    subdir: str = ""  # 'shares' or 'requests'

    def __init__(self, workspace_root: str, shares_dir: str = "", use_trash: bool = False):
        self.workspace_root = Path(os.path.expanduser(workspace_root)).resolve()
        self.shares_base = resolve_shares_dir(workspace_root, shares_dir)
        self.root = self.shares_base / self.subdir
        self.root.mkdir(parents=True, exist_ok=True)
        self.use_trash = use_trash

    def _path_for(self, id_: str) -> Path:
        """Resolve the on-disk content directory for a share/request id.

        Folders are named `<slug>-<id>` and sit next to a sibling
        `<slug>-<id>.json` manifest. We resolve by scanning for a directory
        ending in `-<id>`.
        """
        if not re.fullmatch(r"[A-Z2-7]{6,16}", id_):
            raise NotFoundError(f"Invalid id: {id_}")
        if self.root.exists():
            for child in self.root.iterdir():
                if child.is_dir() and child.name.endswith("-" + id_):
                    return child
        # fall back to a synthetic path - used when creating a new entry
        return self.root / id_

    def _manifest_path_for(self, id_: str) -> Path:
        """Resolve the on-disk manifest path (`<slug>-<id>.json`)."""
        if not re.fullmatch(r"[A-Z2-7]{6,16}", id_):
            raise NotFoundError(f"Invalid id: {id_}")
        if self.root.exists():
            for child in self.root.iterdir():
                if child.is_file() and child.name.endswith(f"-{id_}.json"):
                    return child
        return self.root / f"{id_}.json"

    def _new_path(self, name: str, id_: str) -> Path:
        """Build the directory name for a freshly-created entry."""
        slug = _safe_name(name)
        candidate = self.root / f"{slug}-{id_}"
        return candidate

    def _new_manifest_path(self, name: str, id_: str) -> Path:
        slug = _safe_name(name)
        return self.root / f"{slug}-{id_}.json"

    def _read_manifest(self, id_: str) -> dict[str, Any]:
        path = self._manifest_path_for(id_)
        if not path.exists():
            raise NotFoundError(f"No manifest at {path}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_manifest(self, id_: str, data: dict[str, Any]) -> None:
        """Write the manifest. Uses the existing path if found, otherwise
        builds one from the manifest's slug + id."""
        existing = self._manifest_path_for(id_)
        if existing.exists():
            path = existing
        else:
            slug = data.get("slug") or _safe_name(data.get("name", ""))
            path = self.root / f"{slug}-{id_}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def exists(self, id_: str) -> bool:
        try:
            return self._manifest_path_for(id_).exists()
        except NotFoundError:
            return False

    def list(self) -> list[dict[str, Any]]:
        """List all entries as summary dicts."""
        result = []
        if not self.root.exists():
            return result
        for child in sorted(self.root.iterdir()):
            if not child.is_file() or not child.name.endswith(".json"):
                continue
            try:
                with open(child, "r", encoding="utf-8") as f:
                    result.append(json.load(f))
            except (OSError, json.JSONDecodeError):
                continue
        return result

    def delete(self, id_: str) -> None:
        """Remove both the content directory and the sidecar manifest."""
        manifest_path = self._manifest_path_for(id_)
        content_path = self._path_for(id_)
        if not manifest_path.exists() and not content_path.exists():
            raise NotFoundError(f"Not found: {id_}")
        if content_path.exists():
            _remove(content_path, self.use_trash)
        if manifest_path.exists():
            _remove(manifest_path, self.use_trash)


class ShareStore(BaseStore):
    subdir = "shares"

    def list(self) -> list[dict[str, Any]]:
        """Override base list to refresh `entries` from disk each time.

        The sidecar manifest stores a snapshot of entries at create time,
        but the share folder is the source of truth - files may have been
        added or removed since. Re-scan on every list so the panel stays
        in sync.
        """
        result = []
        if not self.root.exists():
            return result
        for child in sorted(self.root.iterdir()):
            if not child.is_file() or not child.name.endswith(".json"):
                continue
            try:
                with open(child, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            id_ = manifest.get("id")
            if not id_:
                continue
            content_dir = self._path_for(id_)
            manifest["entries"] = _list_entries(content_dir, self.workspace_root)
            try:
                manifest["path"] = str(content_dir.resolve().relative_to(self.workspace_root)).replace(os.sep, "/")
            except ValueError:
                pass
            result.append(manifest)
        return result

    def create(self, name: str, source_paths: list[str]) -> dict[str, Any]:
        """Create a share, copying source_paths (relative to workspace) into it."""
        id_ = generate_token()
        share_dir = self._new_path(name, id_)
        share_dir.mkdir(parents=True, exist_ok=True)

        for rel in source_paths:
            if not _is_safe_relative(rel):
                raise StorageError(f"Unsafe path: {rel}")
            source = self.workspace_root / rel
            if not source.exists():
                raise NotFoundError(f"Source not found: {rel}")
            _copy_into(source, share_dir)

        manifest = {
            "id": id_,
            "name": name,
            "slug": _safe_name(name),
            "kind": "share",
            "created_at": _now(),
        }
        manifest["entries"] = _list_entries(share_dir, self.workspace_root)
        self._write_manifest(id_, manifest)
        return manifest

    def get(self, id_: str) -> dict[str, Any]:
        manifest = self._read_manifest(id_)
        content_dir = self._path_for(id_)
        manifest["entries"] = _list_entries(content_dir, self.workspace_root)
        try:
            manifest["path"] = str(content_dir.resolve().relative_to(self.workspace_root)).replace(os.sep, "/")
        except ValueError:
            pass
        return manifest

    def add_items(self, id_: str, source_paths: list[str]) -> dict[str, Any]:
        content_dir = self._path_for(id_)
        if not content_dir.exists():
            raise NotFoundError(f"Share content missing: {id_}")
        for rel in source_paths:
            if not _is_safe_relative(rel):
                raise StorageError(f"Unsafe path: {rel}")
            source = self.workspace_root / rel
            if not source.exists():
                raise NotFoundError(f"Source not found: {rel}")
            _copy_into(source, content_dir)
        return self.get(id_)

    def remove_items(self, id_: str, item_names: list[str]) -> dict[str, Any]:
        content_dir = self._path_for(id_)
        for name in item_names:
            if not name or "/" in name or "\\" in name or name in (".", ".."):
                raise StorageError(f"Invalid item name: {name}")
            target = content_dir / name
            if not target.exists():
                continue
            _remove(target, self.use_trash)
        return self.get(id_)

    def resolve_data_path(self, id_: str, sub_path: str = "") -> Path:
        """Resolve a sub-path inside a share's content directory safely."""
        content_dir = self._path_for(id_).resolve()
        if not sub_path:
            return content_dir
        if not _is_safe_relative(sub_path):
            raise StorageError(f"Unsafe sub-path: {sub_path}")
        target = (content_dir / sub_path).resolve()
        if not target.is_relative_to(content_dir):
            raise StorageError(f"Path escapes share: {sub_path}")
        if not target.exists():
            raise NotFoundError(f"Not found: {sub_path}")
        return target


class RequestStore(BaseStore):
    subdir = "requests"

    def create(self, name: str) -> dict[str, Any]:
        id_ = generate_token()
        request_dir = self._new_path(name, id_)
        request_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "id": id_,
            "name": name,
            "slug": _safe_name(name),
            "kind": "request",
            "created_at": _now(),
            "upload_count": 0,
            "last_upload_at": 0,
            "last_seen_upload_at": 0,
        }
        self._write_manifest(id_, manifest)
        return manifest

    def get(self, id_: str) -> dict[str, Any]:
        manifest = self._read_manifest(id_)
        content_dir = self._path_for(id_)
        uploaders = []
        upload_count = 0
        if content_dir.exists():
            for child in sorted(content_dir.iterdir()):
                if not child.is_dir():
                    continue
                entries = _list_entries(child, self.workspace_root)
                upload_count += len(entries)
                uploaders.append({
                    "name": child.name,
                    "entries": entries,
                })
        manifest["uploaders"] = uploaders
        manifest["upload_count"] = upload_count
        try:
            manifest["path"] = str(content_dir.resolve().relative_to(self.workspace_root)).replace(os.sep, "/")
        except ValueError:
            pass
        return manifest

    def add_upload(self, id_: str, uploader: str, filename: str, data: bytes) -> dict[str, Any]:
        if not self.exists(id_):
            raise NotFoundError(f"Request not found: {id_}")
        # default to "anonymous" when uploader is empty rather than going
        # through _safe_name (which returns "unnamed" for empty input)
        uploader_slug = _safe_name(uploader) if uploader.strip() else "anonymous"
        # filename may include path components (folder uploads) - keep them but sanitise each
        filename = filename.replace("\\", "/")
        parts = [p for p in filename.split("/") if p and p not in (".", "..")]
        if not parts:
            raise StorageError("Invalid filename")
        safe_parts = [_safe_name(p) for p in parts]
        target_dir = self._path_for(id_) / uploader_slug
        target_dir.mkdir(parents=True, exist_ok=True)
        # nested folders for path-bearing filenames
        nested_dir = target_dir.joinpath(*safe_parts[:-1]) if len(safe_parts) > 1 else target_dir
        nested_dir.mkdir(parents=True, exist_ok=True)
        target = nested_dir / safe_parts[-1]
        if target.exists():
            target = _resolve_unique_target(nested_dir, safe_parts[-1])
        with open(target, "wb") as f:
            f.write(data)
        manifest = self._read_manifest(id_)
        manifest["last_upload_at"] = _now()
        self._write_manifest(id_, manifest)
        return self.get(id_)

    def remove_upload(self, id_: str, uploader: str, item_name: str) -> dict[str, Any]:
        if not uploader or "/" in uploader or "\\" in uploader or uploader in (".", ".."):
            raise StorageError(f"Invalid uploader: {uploader}")
        if not item_name or "/" in item_name or "\\" in item_name or item_name in (".", ".."):
            raise StorageError(f"Invalid item: {item_name}")
        target = self._path_for(id_) / uploader / item_name
        if not target.exists():
            raise NotFoundError(f"Upload not found")
        _remove(target, self.use_trash)
        # remove empty uploader dirs
        uploader_dir = target.parent
        try:
            if uploader_dir.exists() and not any(uploader_dir.iterdir()):
                uploader_dir.rmdir()
        except OSError:
            pass
        return self.get(id_)

    def mark_seen(self, id_: str) -> None:
        manifest = self._read_manifest(id_)
        manifest["last_seen_upload_at"] = manifest.get("last_upload_at", 0)
        self._write_manifest(id_, manifest)

    def resolve_upload_path(self, id_: str, sub_path: str = "") -> Path:
        request_dir = self._path_for(id_).resolve()
        if not sub_path:
            return request_dir
        if not _is_safe_relative(sub_path):
            raise StorageError(f"Unsafe sub-path: {sub_path}")
        target = (request_dir / sub_path).resolve()
        if not target.is_relative_to(request_dir):
            raise StorageError(f"Path escapes request: {sub_path}")
        if not target.exists():
            raise NotFoundError(f"Not found: {sub_path}")
        return target


class ConnectionStore:
    """Tracks links the user has connected to.

    A connection is a remote share or request the user wants to reach from
    their panel. Stored as a flat JSON list at <shares_dir>/connections.json.
    """

    def __init__(self, workspace_root: str, shares_dir: str = ""):
        self.workspace_root = Path(os.path.expanduser(workspace_root)).resolve()
        self.root = resolve_shares_dir(workspace_root, shares_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "connections.json"

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _save(self, items: list[dict[str, Any]]) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)

    @staticmethod
    def _make_key(kind: str, id_: str, host: str) -> str:
        return f"{kind}:{host}:{id_}"

    def list(self) -> list[dict[str, Any]]:
        return self._load()

    def add(self, kind: str, id_: str, host: str, name: str = "", owner: str = "") -> dict[str, Any]:
        if kind not in ("share", "request"):
            raise StorageError(f"Invalid kind: {kind}")
        key = self._make_key(kind, id_, host)
        items = self._load()
        for existing in items:
            if existing.get("key") == key:
                return existing
        entry = {
            "key": key,
            "kind": kind,
            "id": id_,
            "host": host,
            "name": name,
            "owner": owner,
            "added_at": _now(),
        }
        items.append(entry)
        self._save(items)
        return entry

    def remove(self, key: str) -> None:
        items = [e for e in self._load() if e.get("key") != key]
        self._save(items)

    def get(self, key: str) -> dict[str, Any]:
        for entry in self._load():
            if entry.get("key") == key:
                return entry
        raise NotFoundError(f"Connection not found: {key}")
