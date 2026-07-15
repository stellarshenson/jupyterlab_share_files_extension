"""Public manifests must not leak owner-only filesystem metadata.

The stores stamp each entry with the owner's workspace-relative `path` and
the file `mtime` for the authenticated owner panel. `_strip_owner_fields`
removes them at the public boundary so a recipient with only the link never
sees the owner's on-disk layout or file timestamps. The route-level end-to-end
tests live in test_routes.py but are skipped (pytest-jupyter fixture timeout),
so these run the strip directly and through a real store.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from jupyterlab_share_files_extension.routes import _strip_owner_fields
from jupyterlab_share_files_extension.storage import ShareStore


def test_strip_removes_path_and_mtime_keeps_display_fields():
    manifest = {
        "id": "AAA",
        "name": "S",
        "path": "projects/secret/S",
        "entries": [
            {"name": "a.txt", "type": "file", "size": 3, "mtime": 111, "path": "projects/secret/a.txt"},
            {"name": "sub", "type": "directory", "size": 9, "mtime": 222, "path": "projects/secret/sub"},
        ],
    }
    _strip_owner_fields(manifest)
    assert "path" not in manifest
    for entry in manifest["entries"]:
        assert "path" not in entry
        assert "mtime" not in entry
        assert {"name", "type", "size"} <= set(entry)


def test_strip_covers_request_uploader_entries():
    manifest = {
        "uploaders": [
            {"hash": "h", "name": "u", "entries": [
                {"name": "up.txt", "type": "file", "size": 1, "mtime": 5, "path": "requests/x/h/up.txt"},
            ]},
        ],
    }
    _strip_owner_fields(manifest)
    entry = manifest["uploaders"][0]["entries"][0]
    assert "path" not in entry
    assert "mtime" not in entry


def test_owner_get_carries_fields_public_strip_removes_them():
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / "data.txt").write_bytes(b"hello")
        (Path(root) / "sub").mkdir()
        (Path(root) / "sub" / "a.txt").write_bytes(b"x")
        store = ShareStore(workspace_root=root, shares_dir=os.path.join(root, ".shares"))
        share = store.create(name="Public", source_paths=["data.txt", "sub"])

        owner_view = store.get(share["id"])
        assert "path" in owner_view
        assert all("path" in e and "mtime" in e for e in owner_view["entries"])

        public_view = store.get(share["id"])
        _strip_owner_fields(public_view)
        assert "path" not in public_view
        for entry in public_view["entries"]:
            assert "path" not in entry
            assert "mtime" not in entry
            assert {"name", "type", "size"} <= set(entry)
