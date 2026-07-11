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
import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
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


def mint_uploader_hash() -> str:
    """Return a 6-char base32 identity hash for a request uploader.

    Short on purpose - it is shown next to the uploader's name in the owner
    panel and only needs to be unique within a single request.
    """
    return generate_token()[:6]


def generate_password(numwords: int = 4) -> str:
    """Return an xkcd-style passphrase (e.g. ``correct-horse-battery-staple``).

    Uses the ``xkcdpass`` library so owners get a memorable, copy-pasteable
    password without inventing one. Raises RuntimeError if xkcdpass is not
    installed so the caller can surface a clear message.
    """
    try:
        from xkcdpass import xkcd_password as xp
    except ImportError as exc:  # pragma: no cover - xkcdpass is a hard dep
        raise RuntimeError(
            "password generation needs the 'xkcdpass' package"
        ) from exc
    wordfile = xp.locate_wordfile()
    words = xp.generate_wordlist(wordfile=wordfile, min_length=4, max_length=8)
    return xp.generate_xkcdpassword(words, numwords=numwords, delimiter="-")


def verify_password(stored: str, attempt: str) -> bool:
    """Constant-time comparison of an unlock attempt against the stored value.

    Empty ``stored`` means no password is set - callers gate on that before
    reaching here, so an empty stored value never grants access via an empty
    attempt by accident.
    """
    if not stored:
        return False
    return hmac.compare_digest(stored, attempt or "")


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
    """Return [{name, type, size, mtime, path?}] for the top-level items in directory.

    When `workspace_root` is provided and the entry lies inside it, the
    workspace-relative `path` is added so the frontend can pass it to
    JupyterLab's Contents API (e.g. for copy-to-file-browser). `mtime` is the
    filesystem modification time (unix seconds) shown in the panel's hover
    tooltip.
    """
    if not directory.exists():
        return []
    entries = []
    for child in sorted(directory.iterdir()):
        entry: dict[str, Any] = {"name": child.name}
        st = child.stat()
        entry["mtime"] = int(st.st_mtime)
        if child.is_dir():
            entry["type"] = "directory"
            entry["size"] = _dir_size(child)
        else:
            entry["type"] = "file"
            entry["size"] = st.st_size
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
    """Return a non-colliding target path - appends -2, -3 if needed.

    Note - this is racy by design; the caller picks a name, then we open
    it. Two concurrent callers can land on the same path. Use
    `_open_exclusive_for_write` for atomic reserve+open instead - it is
    the only race-free way to reserve a filename on POSIX.
    """
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


def _open_exclusive_for_write(target_dir: Path, name: str) -> tuple[Path, int]:
    """Atomically reserve a non-colliding target path and return (path, fd).

    Uses `O_CREAT | O_EXCL` so two concurrent uploads picking the same
    filename cannot end up writing to the same path - one wins, the
    other gets `FileExistsError` and tries the next `-2`, `-3` suffix.
    The caller owns the returned fd and must close it (or wrap it in
    `os.fdopen()` which closes on context exit).
    """
    stem, dot, ext = name.rpartition(".")
    if dot and stem:
        base, suffix = stem, f".{ext}"
    else:
        base, suffix = name, ""
    counter = 0
    while True:
        candidate_name = name if counter == 0 else f"{base}-{counter + 1}{suffix}"
        candidate = target_dir / candidate_name
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            return candidate, fd
        except FileExistsError:
            counter += 1


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON via write-temp + atomic rename so readers never see a
    half-written manifest, and a crash mid-write does not corrupt it.

    `os.replace` is atomic on POSIX and Windows (Python 3.3+).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


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

    `workspace_root` is the notebook root (the Jupyter server's `root_dir`).
    Empty `configured` defaults to `<notebook_root>/uploads/`. A relative
    `configured` path is resolved against the notebook root; an absolute path
    is used as-is.

    The resolved path MUST land inside the notebook root - `..`-escapes in
    a relative path or an absolute path outside the root raise
    `StorageError`. Files outside the notebook root are not reachable via
    JupyterLab's file browser or Contents API, so they would break the
    panel's drag-out / "Show in File Browser" / "Copy to Current Folder"
    flows; fail loudly at startup instead of silently producing an
    unusable share directory.
    """
    ws = Path(os.path.expanduser(workspace_root)).resolve()
    if not configured:
        return ws / SHARES_DIR_NAME
    configured = os.path.expanduser(configured)
    if os.path.isabs(configured):
        result = Path(configured).resolve()
    else:
        result = (ws / configured).resolve()
    if not result.is_relative_to(ws):
        raise StorageError(
            f"c.ShareFilesConfig.shares_dir resolves to {result}, which is "
            f"outside the notebook root ({ws}). The shares directory must "
            f"live inside the notebook root."
        )
    return result


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
        builds one from the manifest's slug + id. Atomic via temp-file +
        rename so concurrent reads never see a torn write and a crash
        mid-write cannot corrupt the manifest.
        """
        existing = self._manifest_path_for(id_)
        if existing.exists():
            path = existing
        else:
            slug = data.get("slug") or _safe_name(data.get("name", ""))
            path = self.root / f"{slug}-{id_}.json"
        _atomic_write_json(path, data)

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
                    manifest = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            # never leak the plaintext password through the raw summary
            manifest["has_password"] = bool(manifest.get("password"))
            manifest.pop("password", None)
            result.append(manifest)
        return result

    def get_password(self, id_: str) -> str:
        """Return the stored plaintext password, or '' if none. Owner-only -
        callers must be authenticated; never expose this on public routes."""
        try:
            manifest = self._read_manifest(id_)
        except NotFoundError:
            return ""
        return manifest.get("password") or ""

    def set_password(self, id_: str, password: str) -> dict[str, Any]:
        """Set, change, or clear (empty string) the resource password."""
        manifest = self._read_manifest(id_)
        if password:
            manifest["password"] = password
        else:
            manifest.pop("password", None)
        self._write_manifest(id_, manifest)
        return self.get(id_)

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

    def _enrich(self, manifest: dict[str, Any], content_dir: Path) -> dict[str, Any]:
        """Decorate a stored manifest with fields derived from disk.

        Stored manifests carry only the irreducible state (id, name, and -
        for requests - last_seen_upload_at). Everything else - `kind`,
        `slug`, `created_at`, the workspace-relative `path` - is computed
        here at read time so the on-disk manifest stays portable: rename
        the slug folder, move the upload root, copy it elsewhere, and the
        store still picks up the right values.
        """
        id_ = manifest.get("id")
        manifest.setdefault("kind", "share" if self.subdir == "shares" else "request")
        # slug from the folder name (`<slug>-<id>`); fall back to the id
        # itself if for some reason the folder is missing or the user has
        # hand-rolled a manifest without writing a content dir yet.
        if content_dir.is_dir() and id_ and content_dir.name.endswith(f"-{id_}"):
            manifest.setdefault("slug", content_dir.name[: -(len(id_) + 1)])
        else:
            manifest.setdefault("slug", _safe_name(manifest.get("name", "")))
        # created_at from the manifest file mtime - same point in time as
        # `_now()` would have stamped at create
        if "created_at" not in manifest:
            manifest_file = self._manifest_path_for(id_) if id_ else None
            try:
                manifest["created_at"] = int(manifest_file.stat().st_mtime) if manifest_file and manifest_file.exists() else 0
            except OSError:
                manifest["created_at"] = 0
        try:
            manifest["path"] = str(content_dir.resolve().relative_to(self.workspace_root)).replace(os.sep, "/")
        except ValueError:
            pass
        # The plaintext password is owner-only - expose only its presence,
        # never the value, in any enriched (client-facing) manifest. The
        # owner retrieves the value through the authenticated password
        # endpoint; public handlers use get()/list() which run through here.
        manifest["has_password"] = bool(manifest.get("password"))
        manifest.pop("password", None)
        return manifest


class ShareStore(BaseStore):
    subdir = "shares"

    def list(self) -> list[dict[str, Any]]:
        """List shares as API-ready dicts.

        Reads each `<slug>-<id>.json` sidecar, enriches with derived
        fields (`kind`, `slug`, `created_at`, `path`), and freshly scans
        the content directory for `entries`. The folder is the source of
        truth - files may have been added or removed since the manifest
        was written.
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
            manifest = self._enrich(manifest, content_dir)
            manifest["entries"] = _list_entries(content_dir, self.workspace_root)
            result.append(manifest)
        return result

    def create(
        self, name: str, source_paths: list[str], password: str = ""
    ) -> dict[str, Any]:
        """Create a share, copying source_paths (relative to workspace) into it.

        Persists the minimal `{id, name}` manifest (plus an optional
        `password`); everything else is derived on read.
        """
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

        manifest: dict[str, Any] = {"id": id_, "name": name}
        if password:
            manifest["password"] = password
        self._write_manifest(id_, manifest)
        return self.get(id_)

    def get(self, id_: str) -> dict[str, Any]:
        manifest = self._read_manifest(id_)
        content_dir = self._path_for(id_)
        manifest = self._enrich(manifest, content_dir)
        manifest["entries"] = _list_entries(content_dir, self.workspace_root)
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


UPLOADER_SIDECAR = ".uploader.json"


class RequestStore(BaseStore):
    subdir = "requests"

    def create(self, name: str, password: str = "") -> dict[str, Any]:
        id_ = generate_token()
        request_dir = self._new_path(name, id_)
        request_dir.mkdir(parents=True, exist_ok=True)
        # Stored manifest is minimal - only the irreducible state. Everything
        # else (slug, kind, created_at, upload counts, last_upload_at) is
        # derived from disk in `get()`.
        manifest: dict[str, Any] = {"id": id_, "name": name, "last_seen_upload_at": 0}
        if password:
            manifest["password"] = password
        self._write_manifest(id_, manifest)
        return self.get(id_)

    def get(self, id_: str) -> dict[str, Any]:
        manifest = self._read_manifest(id_)
        content_dir = self._path_for(id_)
        manifest = self._enrich(manifest, content_dir)
        manifest.setdefault("last_seen_upload_at", 0)
        uploaders = []
        upload_count = 0
        last_upload_at = 0
        if content_dir.exists():
            for child in sorted(content_dir.iterdir()):
                if not child.is_dir():
                    continue
                # the sidecar carries the display label; entries must not list it
                entries = [
                    e for e in _list_entries(child, self.workspace_root)
                    # also hides crashed sidecar temp files (.uploader.json.*.tmp)
                    if not e["name"].startswith(UPLOADER_SIDECAR)
                ]
                upload_count += len(entries)
                # derive last_upload_at from the most recent file mtime so
                # the manifest does not have to be touched on every upload
                for entry in entries:
                    try:
                        mtime = int((child / entry["name"]).stat().st_mtime)
                        if mtime > last_upload_at:
                            last_upload_at = mtime
                    except OSError:
                        pass
                # pre-identity dirs have no sidecar - fall back to the dir
                # name for both hash and label so legacy uploads stay visible
                display_name = child.name
                try:
                    with open(child / UPLOADER_SIDECAR, encoding="utf-8") as f:
                        display_name = json.load(f).get("name") or child.name
                except (OSError, ValueError):
                    pass
                uploaders.append({
                    "hash": child.name,
                    "name": display_name,
                    "entries": entries,
                })
        manifest["uploaders"] = uploaders
        manifest["upload_count"] = upload_count
        manifest["last_upload_at"] = last_upload_at
        return manifest

    def add_upload(
        self,
        id_: str,
        uploader_hash: str,
        uploader_name: str,
        filename: str,
        data: bytes,
    ) -> dict[str, Any]:
        if not self.exists(id_):
            raise NotFoundError(f"Request not found: {id_}")
        # the hash is the identity - the directory key; the name is only a
        # display label persisted in the sidecar (last write wins, so an
        # uploader renaming themselves relabels their existing pool)
        uploader_slug = _safe_name(uploader_hash)
        display_name = uploader_name.strip() or "anonymous"
        # filename may include path components (folder uploads) - keep them but sanitise each
        filename = filename.replace("\\", "/")
        parts = [p for p in filename.split("/") if p and p not in (".", "..")]
        if not parts:
            raise StorageError("Invalid filename")
        safe_parts = [_safe_name(p) for p in parts]
        target_dir = self._path_for(id_) / uploader_slug
        # mkdir(parents=True, exist_ok=True) is safe under concurrent calls
        target_dir.mkdir(parents=True, exist_ok=True)
        # atomic sidecar write with a UNIQUE temp name - concurrent uploads to
        # the same pool each get their own temp file, so the os.replace of one
        # cannot yank the other's away (a fixed `.tmp` name would race)
        fd, tmp = tempfile.mkstemp(
            prefix=UPLOADER_SIDECAR + ".", suffix=".tmp", dir=target_dir
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"name": display_name}, f)
        os.replace(tmp, target_dir / UPLOADER_SIDECAR)
        # nested folders for path-bearing filenames
        nested_dir = target_dir.joinpath(*safe_parts[:-1]) if len(safe_parts) > 1 else target_dir
        nested_dir.mkdir(parents=True, exist_ok=True)
        # Atomic reserve+open via O_EXCL - two concurrent uploads with the
        # same filename will not stomp on each other, the loser gets the
        # next `-2`, `-3` suffix.
        target, fd = _open_exclusive_for_write(nested_dir, safe_parts[-1])
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        # last_upload_at is derived from file mtimes - no manifest touch needed
        return self.get(id_)

    def remove_upload(self, id_: str, uploader: str, item_name: str) -> dict[str, Any]:
        if not uploader or "/" in uploader or "\\" in uploader or uploader in (".", ".."):
            raise StorageError(f"Invalid uploader: {uploader}")
        if not item_name or "/" in item_name or "\\" in item_name or item_name in (".", ".."):
            raise StorageError(f"Invalid item: {item_name}")
        if item_name == UPLOADER_SIDECAR:
            raise StorageError(f"Invalid item: {item_name}")
        target = self._path_for(id_) / uploader / item_name
        if not target.exists():
            raise NotFoundError(f"Upload not found")
        _remove(target, self.use_trash)
        # remove the uploader dir once only the sidecar (or nothing) is left
        uploader_dir = target.parent
        try:
            if uploader_dir.exists():
                leftovers = [p.name for p in uploader_dir.iterdir()]
                if leftovers == [UPLOADER_SIDECAR]:
                    (uploader_dir / UPLOADER_SIDECAR).unlink()
                    leftovers = []
                if not leftovers:
                    uploader_dir.rmdir()
        except OSError:
            pass
        return self.get(id_)

    def mark_seen(self, id_: str) -> None:
        """Mark all current uploads as seen.

        Reads the live last_upload_at (derived from file mtimes in get())
        and stores it in the manifest. A new upload arriving between read
        and write is benign: the next get() will report a fresh
        last_upload_at > last_seen_upload_at and the panel will flag a
        new arrival.
        """
        current = self.get(id_).get("last_upload_at", 0)
        manifest = self._read_manifest(id_)
        manifest["last_seen_upload_at"] = current
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

    def add(
        self,
        kind: str,
        id_: str,
        host: str,
        name: str = "",
        owner: str = "",
        link: str = "",
        password: str = "",
    ) -> dict[str, Any]:
        if kind not in ("share", "request"):
            raise StorageError(f"Invalid kind: {kind}")
        key = self._make_key(kind, id_, host)
        items = self._load()
        for existing in items:
            if existing.get("key") == key:
                # Backfill the full link on re-add - older entries persisted
                # before links were stored reconstructed a base_path-less URL
                # that JupyterHub bounces to /hub/ (404). Reconnecting repairs.
                changed = False
                if link and not existing.get("link"):
                    existing["link"] = link
                    changed = True
                # Re-connecting with a (new) password updates the stored one -
                # the owner may have changed it since the first connect.
                if password and existing.get("password") != password:
                    existing["password"] = password
                    changed = True
                if changed:
                    self._save(items)
                return existing
        entry = {
            "key": key,
            "kind": kind,
            "id": id_,
            "host": host,
            "name": name,
            "owner": owner,
            # Persist the exact pasted link so refresh/download keep the
            # owner's full `/user/<name>/` prefix on JupyterHub. Reconstructing
            # it on the client is impossible - the client cannot know the
            # owner's base_url.
            "link": link,
            "added_at": _now(),
        }
        if password:
            # Peer password for a protected remote share/request - used by the
            # server (and panel) to unlock before manifest/download/upload.
            entry["password"] = password
        items.append(entry)
        self._save(items)
        return entry

    def remove(self, key: str) -> None:
        items = [e for e in self._load() if e.get("key") != key]
        self._save(items)

    def set_uploader_hash(self, key: str, uploader_hash: str) -> None:
        """Persist the server-issued uploader identity for a request connection.

        Server-to-server uploads have no browser cookie jar - the hash minted
        by the peer on the first upload is stored here and replayed as a
        Cookie header so every later upload lands in the same pool.
        """
        items = self._load()
        for entry in items:
            if entry.get("key") == key:
                entry["uploader_hash"] = uploader_hash
                self._save(items)
                return
        raise NotFoundError(f"Connection not found: {key}")

    def get(self, key: str) -> dict[str, Any]:
        for entry in self._load():
            if entry.get("key") == key:
                return entry
        raise NotFoundError(f"Connection not found: {key}")
