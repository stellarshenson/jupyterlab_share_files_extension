"""Unit tests for the storage layer.

These tests exercise the ShareStore, RequestStore, and ConnectionStore
classes directly - no Tornado, no Jupyter test fixtures, no HTTP. The
storage layer is the heart of the extension; everything in routes.py is
just a thin handler around it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jupyterlab_share_files_extension import storage as storage_mod
from jupyterlab_share_files_extension.storage import (
    SHARES_DIR_NAME,
    ConnectionStore,
    NotFoundError,
    RequestStore,
    ShareStore,
    StorageError,
    _is_safe_relative,
    _remove,
    _safe_name,
    generate_token,
    resolve_shares_dir,
)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


class TestHelpers:
    def test_generate_token_format(self):
        for _ in range(20):
            token = generate_token()
            assert len(token) == 8
            assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in token)

    def test_generate_token_uniqueness(self):
        tokens = {generate_token() for _ in range(100)}
        assert len(tokens) == 100  # collision among 100 8-char base32 is astronomically unlikely

    def test_safe_name_passthrough(self):
        assert _safe_name("hello") == "hello"
        assert _safe_name("training_data") == "training_data"
        assert _safe_name("file.txt") == "file.txt"

    def test_safe_name_sanitises_spaces(self):
        assert _safe_name("training data") == "training-data"
        assert _safe_name("my cool share!") == "my-cool-share"

    def test_safe_name_strips_leading_trailing(self):
        assert _safe_name("   foo   ") == "foo"
        assert _safe_name("---hello---") == "hello"

    def test_safe_name_empty_fallback(self):
        assert _safe_name("") == "unnamed"
        assert _safe_name("   ") == "unnamed"
        assert _safe_name("@@@") == "unnamed"

    def test_is_safe_relative_accepts_normal_paths(self):
        assert _is_safe_relative("foo.txt")
        assert _is_safe_relative("dir/file.txt")
        assert _is_safe_relative("a/b/c/d")

    def test_is_safe_relative_rejects_traversal(self):
        # Paths that escape the root (start with ../ after normalisation) are rejected
        assert not _is_safe_relative("../escape.txt")
        assert not _is_safe_relative("..")
        # `foo/../bar` normalises to `bar`, which is safe - accepted
        assert _is_safe_relative("foo/../bar")

    def test_is_safe_relative_rejects_absolute(self):
        assert not _is_safe_relative("/etc/passwd")
        assert not _is_safe_relative("/")

    def test_is_safe_relative_rejects_empty(self):
        assert not _is_safe_relative("")


class TestResolveSharesDir:
    def test_default_uses_uploads_under_root(self, tmp_path):
        resolved = resolve_shares_dir(str(tmp_path), "")
        assert resolved == tmp_path / SHARES_DIR_NAME

    def test_relative_resolves_against_root(self, tmp_path):
        resolved = resolve_shares_dir(str(tmp_path), "custom_shares")
        assert resolved == (tmp_path / "custom_shares").resolve()

    def test_absolute_inside_root_is_accepted(self, tmp_path):
        """An absolute path that lives inside the notebook root is fine."""
        absolute = str(tmp_path / "elsewhere")
        resolved = resolve_shares_dir(str(tmp_path), absolute)
        assert str(resolved) == absolute

    def test_absolute_outside_root_raises(self, tmp_path):
        """An absolute path outside the notebook root is refused at resolve
        time so the server extension fails to start."""
        outside = tmp_path.parent / "elsewhere"
        with pytest.raises(StorageError, match="outside the notebook root"):
            resolve_shares_dir(str(tmp_path), str(outside))

    def test_relative_traversal_escape_raises(self, tmp_path):
        """A relative path that uses `..` to escape the notebook root is
        refused even though the syntax looks innocuous."""
        with pytest.raises(StorageError, match="outside the notebook root"):
            resolve_shares_dir(str(tmp_path), "../escape")

    def test_tilde_expansion_inside_root_is_accepted(self, tmp_path, monkeypatch):
        """`~` in the configured path expands to HOME first; if HOME is the
        notebook root, the path is accepted."""
        monkeypatch.setenv("HOME", str(tmp_path))
        resolved = resolve_shares_dir(str(tmp_path), "~/uploads")
        assert resolved == (tmp_path / "uploads").resolve()

    def test_tilde_expansion_outside_root_raises(self, tmp_path, monkeypatch):
        """`~/anywhere` that expands outside the notebook root is refused."""
        other_home = tmp_path.parent / "other_home"
        other_home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(other_home))
        with pytest.raises(StorageError, match="outside the notebook root"):
            resolve_shares_dir(str(tmp_path), "~/uploads")


# --------------------------------------------------------------------------- #
# ShareStore
# --------------------------------------------------------------------------- #


class TestShareStore:
    def test_create_and_get(self, tmp_path):
        (tmp_path / "hello.txt").write_text("hi")
        store = ShareStore(str(tmp_path))
        manifest = store.create("Test Share", ["hello.txt"])
        assert manifest["name"] == "Test Share"
        assert manifest["kind"] == "share"
        assert len(manifest["id"]) == 8
        assert manifest["slug"] == "Test-Share"
        assert len(manifest["entries"]) == 1
        assert manifest["entries"][0]["name"] == "hello.txt"
        assert manifest["entries"][0]["type"] == "file"

        fetched = store.get(manifest["id"])
        assert fetched["id"] == manifest["id"]
        assert fetched["entries"][0]["size"] == 2  # "hi"

    def test_folder_name_uses_slug_and_id(self, tmp_path):
        (tmp_path / "x.txt").write_text("x")
        store = ShareStore(str(tmp_path))
        manifest = store.create("My Cool Share", ["x.txt"])
        shares_dir = tmp_path / SHARES_DIR_NAME / "shares"
        folder = next(c for c in shares_dir.iterdir() if c.is_dir())
        assert folder.name.endswith(f"-{manifest['id']}")
        assert folder.name.startswith("My-Cool-Share")
        # sidecar manifest sits next to the folder
        sidecar = shares_dir / f"{folder.name}.json"
        assert sidecar.exists()

    def test_create_with_folder_preserves_structure(self, tmp_path):
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        (src / "main.py").write_text("# main")
        (src / "sub" / "helper.py").write_text("# helper")
        store = ShareStore(str(tmp_path))
        manifest = store.create("project", ["src"])
        assert len(manifest["entries"]) == 1
        assert manifest["entries"][0]["type"] == "directory"
        assert manifest["entries"][0]["name"] == "src"

        # contents preserved on disk - directly under the share folder
        data_dir = next(
            c for c in (tmp_path / SHARES_DIR_NAME / "shares").iterdir() if c.is_dir()
        )
        assert (data_dir / "src" / "main.py").exists()
        assert (data_dir / "src" / "sub" / "helper.py").exists()

    def test_create_rejects_traversal(self, tmp_path):
        store = ShareStore(str(tmp_path))
        with pytest.raises(StorageError):
            store.create("evil", ["../../../etc/passwd"])

    def test_create_rejects_missing_source(self, tmp_path):
        store = ShareStore(str(tmp_path))
        with pytest.raises(NotFoundError):
            store.create("ghost", ["does_not_exist.txt"])

    def test_add_items_extends_share(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        store = ShareStore(str(tmp_path))
        share = store.create("pair", ["a.txt"])
        updated = store.add_items(share["id"], ["b.txt"])
        names = {e["name"] for e in updated["entries"]}
        assert names == {"a.txt", "b.txt"}

    def test_remove_items_removes_files(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        store = ShareStore(str(tmp_path))
        share = store.create("pair", ["a.txt", "b.txt"])
        result = store.remove_items(share["id"], ["a.txt"])
        names = {e["name"] for e in result["entries"]}
        assert names == {"b.txt"}

    def test_remove_items_removes_folder_recursively(self, tmp_path):
        (tmp_path / "dir").mkdir()
        (tmp_path / "dir" / "nested.txt").write_text("nested")
        store = ShareStore(str(tmp_path))
        share = store.create("with-folder", ["dir"])
        store.remove_items(share["id"], ["dir"])
        fetched = store.get(share["id"])
        assert fetched["entries"] == []

    def test_remove_items_rejects_path_components(self, tmp_path):
        (tmp_path / "x.txt").write_text("x")
        store = ShareStore(str(tmp_path))
        share = store.create("solo", ["x.txt"])
        with pytest.raises(StorageError):
            store.remove_items(share["id"], ["../x.txt"])

    def test_list_picks_up_dropped_files(self, tmp_path):
        """The bug we fixed: list() re-scans data/ rather than trusting the
        manifest cache. After add_items, the next list() must show new files.
        """
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        store = ShareStore(str(tmp_path))
        share = store.create("growing", ["a.txt"])
        listed_before = next(s for s in store.list() if s["id"] == share["id"])
        assert len(listed_before["entries"]) == 1

        store.add_items(share["id"], ["b.txt"])
        listed_after = next(s for s in store.list() if s["id"] == share["id"])
        assert len(listed_after["entries"]) == 2

    def test_delete_removes_share_directory(self, tmp_path):
        (tmp_path / "x.txt").write_text("x")
        store = ShareStore(str(tmp_path))
        share = store.create("tmp", ["x.txt"])
        store.delete(share["id"])
        assert not store.exists(share["id"])

    def test_delete_missing_raises(self, tmp_path):
        store = ShareStore(str(tmp_path))
        with pytest.raises(NotFoundError):
            store.delete("ABCDEFGH")

    def test_existing_share_with_sidecar_manifest_can_be_read(self, tmp_path):
        """Hand-rolling a share on disk: `<slug>-<id>.json` sidecar +
        `<slug>-<id>/` content directory - the store reads it back without
        having created it."""
        store = ShareStore(str(tmp_path))
        shares_dir = tmp_path / SHARES_DIR_NAME / "shares"
        content = shares_dir / "fixture-ABCDEF23"
        content.mkdir()
        (content / "f.txt").write_text("hand-rolled")
        manifest = {
            "id": "ABCDEF23",
            "name": "fixture",
            "slug": "fixture",
            "kind": "share",
            "created_at": 0,
        }
        (shares_dir / "fixture-ABCDEF23.json").write_text(json.dumps(manifest))
        fetched = store.get("ABCDEF23")
        assert fetched["name"] == "fixture"
        assert fetched["entries"][0]["name"] == "f.txt"

    def test_resolve_data_path_within_share(self, tmp_path):
        (tmp_path / "x.txt").write_text("contents")
        store = ShareStore(str(tmp_path))
        share = store.create("public", ["x.txt"])
        path = store.resolve_data_path(share["id"], "x.txt")
        assert path.read_text() == "contents"

    def test_resolve_data_path_rejects_escape(self, tmp_path):
        (tmp_path / "x.txt").write_text("contents")
        store = ShareStore(str(tmp_path))
        share = store.create("public", ["x.txt"])
        with pytest.raises(StorageError):
            store.resolve_data_path(share["id"], "../../../etc/passwd")

    def test_invalid_id_raises_not_found(self, tmp_path):
        store = ShareStore(str(tmp_path))
        with pytest.raises(NotFoundError):
            store.get("not-base32!")


# --------------------------------------------------------------------------- #
# RequestStore
# --------------------------------------------------------------------------- #


class TestRequestStore:
    def test_create_returns_empty(self, tmp_path):
        store = RequestStore(str(tmp_path))
        manifest = store.create("Homework Week 5")
        assert manifest["name"] == "Homework Week 5"
        assert manifest["upload_count"] == 0
        assert len(manifest["id"]) == 8
        assert manifest["kind"] == "request"

    def test_add_upload_creates_uploader_folder(self, tmp_path):
        store = RequestStore(str(tmp_path))
        req = store.create("submissions")
        result = store.add_upload(req["id"], "alice", "answer.py", b"print(1)")
        assert result["upload_count"] == 1
        uploaders = result["uploaders"]
        assert len(uploaders) == 1
        assert uploaders[0]["name"] == "alice"
        assert uploaders[0]["entries"][0]["name"] == "answer.py"

    def test_add_upload_anonymises_empty_uploader(self, tmp_path):
        store = RequestStore(str(tmp_path))
        req = store.create("inbox")
        result = store.add_upload(req["id"], "", "f.txt", b"data")
        assert result["uploaders"][0]["name"] == "anonymous"

    def test_add_upload_supports_nested_paths(self, tmp_path):
        """Uploading a file with folder components preserves structure."""
        store = RequestStore(str(tmp_path))
        req = store.create("project-uploads")
        store.add_upload(req["id"], "bob", "results/output.csv", b"x")
        requests_dir = tmp_path / SHARES_DIR_NAME / "requests"
        req_folder = next(c for c in requests_dir.iterdir() if c.is_dir())
        assert (req_folder / "bob" / "results" / "output.csv").exists()

    def test_add_upload_rejects_traversal_filename(self, tmp_path):
        store = RequestStore(str(tmp_path))
        req = store.create("inbox")
        # `..` components are stripped; remaining parts become nested folders.
        # `../../etc/passwd` collapses to `etc/passwd`, landing under bob/etc/
        store.add_upload(req["id"], "bob", "../../etc/passwd", b"x")
        # nothing escaped the workspace
        assert not (tmp_path / "etc").exists()
        # the upload sits under the uploader folder, never above it
        requests_dir = tmp_path / SHARES_DIR_NAME / "requests"
        req_folder = next(c for c in requests_dir.iterdir() if c.is_dir())
        assert (req_folder / "bob" / "etc" / "passwd").exists()

    def test_remove_upload(self, tmp_path):
        store = RequestStore(str(tmp_path))
        req = store.create("inbox")
        store.add_upload(req["id"], "bob", "a.txt", b"a")
        store.add_upload(req["id"], "bob", "b.txt", b"b")
        result = store.remove_upload(req["id"], "bob", "a.txt")
        names = {e["name"] for e in result["uploaders"][0]["entries"]}
        assert names == {"b.txt"}

    def test_remove_upload_cleans_empty_uploader(self, tmp_path):
        store = RequestStore(str(tmp_path))
        req = store.create("inbox")
        store.add_upload(req["id"], "alice", "only.txt", b"x")
        result = store.remove_upload(req["id"], "alice", "only.txt")
        assert result["uploaders"] == []

    def test_remove_upload_rejects_invalid_name(self, tmp_path):
        store = RequestStore(str(tmp_path))
        req = store.create("inbox")
        store.add_upload(req["id"], "bob", "a.txt", b"a")
        with pytest.raises(StorageError):
            store.remove_upload(req["id"], "bob", "../a.txt")

    def test_mark_seen_updates_manifest(self, tmp_path):
        store = RequestStore(str(tmp_path))
        req = store.create("inbox")
        store.add_upload(req["id"], "bob", "f.txt", b"x")
        before = store.get(req["id"])
        assert before["last_seen_upload_at"] != before["last_upload_at"]
        store.mark_seen(req["id"])
        after = store.get(req["id"])
        assert after["last_seen_upload_at"] == after["last_upload_at"]


# --------------------------------------------------------------------------- #
# ConnectionStore
# --------------------------------------------------------------------------- #


class TestConnectionStore:
    def test_empty_by_default(self, tmp_path):
        store = ConnectionStore(str(tmp_path))
        assert store.list() == []

    def test_add_and_list(self, tmp_path):
        store = ConnectionStore(str(tmp_path))
        entry = store.add("share", "ABCDEFGH", "https://hub.test", name="Demo", owner="alice")
        assert entry["kind"] == "share"
        assert entry["id"] == "ABCDEFGH"
        assert entry["host"] == "https://hub.test"
        assert entry["key"] == "share:https://hub.test:ABCDEFGH"
        assert store.list() == [entry]

    def test_add_is_idempotent(self, tmp_path):
        store = ConnectionStore(str(tmp_path))
        first = store.add("share", "ABCDEFGH", "https://hub.test")
        second = store.add("share", "ABCDEFGH", "https://hub.test")
        assert first["key"] == second["key"]
        assert len(store.list()) == 1

    def test_remove(self, tmp_path):
        store = ConnectionStore(str(tmp_path))
        entry = store.add("request", "STUVWXYZ", "https://other.test")
        store.remove(entry["key"])
        assert store.list() == []

    def test_get_missing(self, tmp_path):
        store = ConnectionStore(str(tmp_path))
        with pytest.raises(NotFoundError):
            store.get("nothing")

    def test_invalid_kind_rejected(self, tmp_path):
        store = ConnectionStore(str(tmp_path))
        with pytest.raises(StorageError):
            store.add("bogus", "ABCDEFGH", "https://hub.test")

    def test_persistence_across_instances(self, tmp_path):
        store1 = ConnectionStore(str(tmp_path))
        store1.add("share", "ABCDEFGH", "https://hub.test")
        store2 = ConnectionStore(str(tmp_path))
        assert len(store2.list()) == 1

    def test_link_is_persisted(self, tmp_path):
        # The full pasted link must survive across instances - the client
        # cannot reconstruct the owner's `/user/<name>/` prefix on JupyterHub,
        # so a missing link makes a working share look offline (manifest 404).
        link = "https://hub.test/user/alice/jupyterlab-share-files-extension/public/share/ABCDEFGH"
        store1 = ConnectionStore(str(tmp_path))
        store1.add("share", "ABCDEFGH", "https://hub.test", link=link)
        store2 = ConnectionStore(str(tmp_path))
        assert store2.list()[0]["link"] == link

    def test_link_backfilled_on_readd(self, tmp_path):
        # A connection persisted before links were stored (no "link" key) is
        # repaired when the user reconnects with the same link.
        store = ConnectionStore(str(tmp_path))
        store.add("share", "ABCDEFGH", "https://hub.test")  # legacy: no link
        assert store.list()[0].get("link", "") == ""
        link = "https://hub.test/user/alice/jupyterlab-share-files-extension/public/share/ABCDEFGH"
        store.add("share", "ABCDEFGH", "https://hub.test", link=link)
        items = store.list()
        assert len(items) == 1
        assert items[0]["link"] == link


# --------------------------------------------------------------------------- #
# Configurable shares dir
# --------------------------------------------------------------------------- #


class TestConfigurableSharesDir:
    def test_relative_path_creates_directory(self, tmp_path):
        store = ShareStore(str(tmp_path), shares_dir="custom_drops")
        assert (tmp_path / "custom_drops" / "shares").is_dir()

    def test_absolute_path_outside_workspace(self, tmp_path):
        external = tmp_path / "elsewhere"
        store = ShareStore(str(tmp_path), shares_dir=str(external))
        assert (external / "shares").is_dir()

    def test_shares_request_connection_share_same_root(self, tmp_path):
        ShareStore(str(tmp_path), shares_dir="shared_root")
        RequestStore(str(tmp_path), shares_dir="shared_root")
        ConnectionStore(str(tmp_path), shares_dir="shared_root")
        # all three should write under shared_root/
        assert (tmp_path / "shared_root" / "shares").is_dir()
        assert (tmp_path / "shared_root" / "requests").is_dir()
        # connections.json gets created lazily on first write
        ConnectionStore(str(tmp_path), shares_dir="shared_root").add(
            "share", "ABCDEFGH", "https://x"
        )
        assert (tmp_path / "shared_root" / "connections.json").is_file()


# --------------------------------------------------------------------------- #
# Trash behaviour (use_trash)
# --------------------------------------------------------------------------- #


class TestUseTrash:
    """Verify the _remove helper and that stores route deletes through it.

    send2trash itself can fail in containerised CI (no XDG trash mount on
    tmpfs), so we monkeypatch the underlying send2trash callable and assert
    on the call rather than on filesystem trash side-effects.
    """

    def test_remove_permanent_when_trash_disabled(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(storage_mod, "_send2trash", lambda p: called.append(p))
        target = tmp_path / "x.txt"
        target.write_text("x")
        _remove(target, use_trash=False)
        assert not target.exists()
        assert called == []

    def test_remove_uses_send2trash_when_enabled(self, tmp_path, monkeypatch):
        called = []

        def fake_trash(p):
            called.append(p)
            Path(p).unlink()

        monkeypatch.setattr(storage_mod, "_send2trash", fake_trash)
        target = tmp_path / "x.txt"
        target.write_text("x")
        _remove(target, use_trash=True)
        assert called == [str(target)]
        assert not target.exists()

    def test_remove_falls_back_when_send2trash_fails(self, tmp_path, monkeypatch):
        def raising(p):
            raise RuntimeError("no trash mount")

        monkeypatch.setattr(storage_mod, "_send2trash", raising)
        target = tmp_path / "x.txt"
        target.write_text("x")
        _remove(target, use_trash=True)
        # Permanent-delete fallback kicked in - file is gone, no exception
        assert not target.exists()

    def test_remove_falls_back_when_send2trash_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage_mod, "_send2trash", None)
        target = tmp_path / "x.txt"
        target.write_text("x")
        _remove(target, use_trash=True)
        assert not target.exists()

    def test_remove_handles_directory(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(storage_mod, "_send2trash", lambda p: called.append(p))
        d = tmp_path / "d"
        (d / "nested").mkdir(parents=True)
        (d / "nested" / "f.txt").write_text("f")
        _remove(d, use_trash=False)
        assert not d.exists()
        assert called == []

    def test_share_store_delete_routes_through_trash(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(
            storage_mod,
            "_send2trash",
            lambda p: (called.append(p), __import__("shutil").rmtree(p))[1],
        )
        (tmp_path / "x.txt").write_text("x")
        store = ShareStore(str(tmp_path), use_trash=True)
        share = store.create("trashed", ["x.txt"])
        store.delete(share["id"])
        assert called and "trashed" in called[0]

    def test_share_store_remove_items_routes_through_trash(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(
            storage_mod,
            "_send2trash",
            lambda p: (called.append(p), Path(p).unlink())[1],
        )
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        store = ShareStore(str(tmp_path), use_trash=True)
        share = store.create("s", ["a.txt", "b.txt"])
        store.remove_items(share["id"], ["a.txt"])
        assert len(called) == 1
        assert called[0].endswith("a.txt")

    def test_request_store_remove_upload_routes_through_trash(
        self, tmp_path, monkeypatch
    ):
        called = []
        monkeypatch.setattr(
            storage_mod,
            "_send2trash",
            lambda p: (called.append(p), Path(p).unlink())[1],
        )
        store = RequestStore(str(tmp_path), use_trash=True)
        req = store.create("inbox")
        store.add_upload(req["id"], "alice", "note.txt", b"hi")
        store.remove_upload(req["id"], "alice", "note.txt")
        assert called and called[0].endswith("note.txt")

    def test_store_default_use_trash_is_false(self, tmp_path):
        """Stores called without use_trash keep current behaviour."""
        store = ShareStore(str(tmp_path))
        assert store.use_trash is False


# --------------------------------------------------------------------------- #
# Minimal manifest format
# --------------------------------------------------------------------------- #


class TestMinimalManifest:
    """Stored manifests carry only irreducible state. Everything else
    (slug, kind, created_at, upload_count, last_upload_at) is derived
    from disk on read so the manifest + content folder are portable."""

    def test_share_manifest_on_disk_has_only_id_and_name(self, tmp_path):
        (tmp_path / "x.txt").write_text("x")
        store = ShareStore(str(tmp_path))
        share = store.create("My Share", ["x.txt"])
        sidecar = next(
            c
            for c in (tmp_path / SHARES_DIR_NAME / "shares").iterdir()
            if c.is_file() and c.name.endswith(".json")
        )
        on_disk = json.loads(sidecar.read_text())
        assert set(on_disk.keys()) == {"id", "name"}
        assert on_disk["id"] == share["id"]
        assert on_disk["name"] == "My Share"

    def test_request_manifest_on_disk_only_tracks_seen_state(self, tmp_path):
        store = RequestStore(str(tmp_path))
        req = store.create("inbox")
        sidecar = next(
            c
            for c in (tmp_path / SHARES_DIR_NAME / "requests").iterdir()
            if c.is_file() and c.name.endswith(".json")
        )
        on_disk = json.loads(sidecar.read_text())
        # Only fields that cannot be derived from disk: id, name, and the
        # user-side "seen" cursor.
        assert set(on_disk.keys()) == {"id", "name", "last_seen_upload_at"}
        assert on_disk["id"] == req["id"]
        assert on_disk["last_seen_upload_at"] == 0

    def test_share_api_response_derives_slug_kind_created_at(self, tmp_path):
        (tmp_path / "x.txt").write_text("x")
        store = ShareStore(str(tmp_path))
        share = store.create("My Cool Share", ["x.txt"])
        # API response carries the derived fields the frontend expects
        assert share["slug"] == "My-Cool-Share"
        assert share["kind"] == "share"
        assert isinstance(share["created_at"], int) and share["created_at"] > 0

    def test_request_api_response_derives_counts_and_last_upload_at(self, tmp_path):
        store = RequestStore(str(tmp_path))
        req = store.create("inbox")
        assert req["upload_count"] == 0
        assert req["last_upload_at"] == 0
        store.add_upload(req["id"], "alice", "a.txt", b"x")
        store.add_upload(req["id"], "alice", "b.txt", b"y")
        fresh = store.get(req["id"])
        assert fresh["upload_count"] == 2
        assert fresh["last_upload_at"] > 0
        assert fresh["kind"] == "request"
        assert fresh["slug"] == "inbox"

    def test_hand_rolled_minimal_share_manifest_loads(self, tmp_path):
        """Drop a `{id, name}` JSON next to a content folder and the store
        reads it back as a fully-shaped share with derived fields."""
        store = ShareStore(str(tmp_path))
        shares_dir = tmp_path / SHARES_DIR_NAME / "shares"
        (shares_dir / "minimal-ABCDEF23").mkdir(parents=True)
        (shares_dir / "minimal-ABCDEF23" / "f.txt").write_text("hand-rolled")
        (shares_dir / "minimal-ABCDEF23.json").write_text(
            json.dumps({"id": "ABCDEF23", "name": "minimal"})
        )
        share = store.get("ABCDEF23")
        assert share["name"] == "minimal"
        assert share["slug"] == "minimal"
        assert share["kind"] == "share"
        assert share["entries"][0]["name"] == "f.txt"

    def test_hand_rolled_minimal_request_manifest_loads(self, tmp_path):
        store = RequestStore(str(tmp_path))
        requests_dir = tmp_path / SHARES_DIR_NAME / "requests"
        (requests_dir / "inbox-ABCDEF24").mkdir(parents=True)
        (requests_dir / "inbox-ABCDEF24" / "alice").mkdir()
        (requests_dir / "inbox-ABCDEF24" / "alice" / "note.txt").write_text("hi")
        (requests_dir / "inbox-ABCDEF24.json").write_text(
            json.dumps({"id": "ABCDEF24", "name": "inbox", "last_seen_upload_at": 0})
        )
        req = store.get("ABCDEF24")
        assert req["upload_count"] == 1
        assert req["uploaders"][0]["name"] == "alice"
        assert req["last_upload_at"] > 0
        assert req["last_seen_upload_at"] == 0

    def test_mark_seen_writes_last_upload_at_from_disk(self, tmp_path):
        store = RequestStore(str(tmp_path))
        req = store.create("inbox")
        store.add_upload(req["id"], "alice", "f.txt", b"x")
        store.mark_seen(req["id"])
        # mark_seen wrote the current last_upload_at into the manifest;
        # next get() reports the cursor caught up
        fresh = store.get(req["id"])
        assert fresh["last_seen_upload_at"] == fresh["last_upload_at"]


# --------------------------------------------------------------------------- #
# Concurrency / thread safety
# --------------------------------------------------------------------------- #


class TestConcurrentUploads:
    """Multiple recipients can upload to the same request at the same
    time. The storage layer must not lose data or corrupt the manifest
    under concurrent calls."""

    def test_same_filename_uploads_do_not_clobber(self, tmp_path):
        """Two uploads with the same filename land at distinct paths
        via O_EXCL - the loser gets the `-2` suffix."""
        import threading

        store = RequestStore(str(tmp_path))
        req = store.create("inbox")
        errors: list[Exception] = []
        bodies = [b"alpha", b"beta", b"gamma", b"delta", b"epsilon"]

        def upload(payload: bytes) -> None:
            try:
                store.add_upload(req["id"], "alice", "f.txt", payload)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=upload, args=(b,)) for b in bodies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # No payload was lost
        alice_dir = next(
            c
            for c in (tmp_path / SHARES_DIR_NAME / "requests").iterdir()
            if c.is_dir()
        ) / "alice"
        files = sorted(alice_dir.iterdir())
        assert len(files) == len(bodies)
        # Every payload is preserved on disk
        on_disk = {f.read_bytes() for f in files}
        assert on_disk == set(bodies)

    def test_concurrent_uploads_different_filenames_all_persist(self, tmp_path):
        import threading

        store = RequestStore(str(tmp_path))
        req = store.create("inbox")

        def upload(i: int) -> None:
            store.add_upload(req["id"], "bob", f"file-{i}.txt", str(i).encode())

        threads = [threading.Thread(target=upload, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        bob_dir = next(
            c
            for c in (tmp_path / SHARES_DIR_NAME / "requests").iterdir()
            if c.is_dir()
        ) / "bob"
        names = sorted(p.name for p in bob_dir.iterdir())
        assert names == sorted(f"file-{i}.txt" for i in range(20))

    def test_manifest_write_is_atomic(self, tmp_path, monkeypatch):
        """If the JSON encode step blows up halfway through a write,
        the existing manifest must remain readable (write-temp +
        rename gives us that)."""
        store = RequestStore(str(tmp_path))
        req = store.create("inbox")
        original_text = (
            tmp_path / SHARES_DIR_NAME / "requests" / f"{req['slug']}-{req['id']}.json"
        ).read_text()
        original = json.loads(original_text)

        # Force json.dump to fail mid-write
        real_dump = json.dump

        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash")

        monkeypatch.setattr(json, "dump", boom)
        with pytest.raises(RuntimeError):
            store.mark_seen(req["id"])
        monkeypatch.setattr(json, "dump", real_dump)

        # Manifest still loads cleanly - the partial temp file did not
        # replace the canonical path
        fresh = store.get(req["id"])
        assert fresh["id"] == original["id"]
        assert fresh["name"] == original["name"]
