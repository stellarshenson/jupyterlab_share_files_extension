"""Unit tests for the ShareFilesConfig traits.

These tests exercise the Configurable directly - no Jupyter server, no
Tornado. They verify the defaults documented in `config.py` and that
options round-trip through traitlets when set explicitly (either by
constructor kwargs or via a `traitlets.config.Config` instance,
mirroring how `jupyter_server_config.py` is applied).
"""

from __future__ import annotations

import pytest
from traitlets.config import Config

from jupyterlab_share_files_extension.config import ShareFilesConfig


class TestShareFilesConfigDefaults:
    def test_shares_dir_default_is_empty_string(self):
        """Empty `shares_dir` means 'use the built-in default
        (<notebook_root>/uploads/)'."""
        cfg = ShareFilesConfig()
        assert cfg.shares_dir == ""

    def test_use_trash_default_is_true(self):
        """Match Jupyter Server's own `FileContentsManager.delete_to_trash`
        default so panel deletes behave like file-browser deletes."""
        cfg = ShareFilesConfig()
        assert cfg.use_trash is True

    def test_verify_peer_tls_default_is_true(self):
        cfg = ShareFilesConfig()
        assert cfg.verify_peer_tls is True


class TestShareFilesConfigOverrides:
    def test_shares_dir_accepts_relative_path(self):
        cfg = ShareFilesConfig(shares_dir="data/uploads")
        assert cfg.shares_dir == "data/uploads"

    def test_shares_dir_accepts_absolute_path(self):
        cfg = ShareFilesConfig(shares_dir="/srv/drops")
        assert cfg.shares_dir == "/srv/drops"

    def test_use_trash_can_be_disabled(self):
        cfg = ShareFilesConfig(use_trash=False)
        assert cfg.use_trash is False

    def test_verify_peer_tls_can_be_disabled(self):
        # Self-signed peers (e.g. a JupyterHub) need this off.
        cfg = ShareFilesConfig(verify_peer_tls=False)
        assert cfg.verify_peer_tls is False

    def test_config_object_applies_overrides(self):
        """Mirrors how `jupyter_server_config.py` writes settings - via a
        `traitlets.config.Config` instance passed to the Configurable."""
        c = Config()
        c.ShareFilesConfig.shares_dir = "team-drops"
        c.ShareFilesConfig.use_trash = False
        cfg = ShareFilesConfig(config=c)
        assert cfg.shares_dir == "team-drops"
        assert cfg.use_trash is False


class TestShareFilesConfigTypes:
    def test_use_trash_rejects_non_bool(self):
        """Trait coercion guard - a non-boolean value should fail rather
        than silently truthify."""
        from traitlets import TraitError

        with pytest.raises(TraitError):
            ShareFilesConfig(use_trash="yes")

    def test_shares_dir_rejects_non_string(self):
        from traitlets import TraitError

        with pytest.raises(TraitError):
            ShareFilesConfig(shares_dir=123)


class TestUseTrashReachesEveryHandler:
    """One `use_trash` policy for the panel and the public pages - an uploader
    removing their file from the request page is treated like the owner
    removing it from the panel. Stub handler, no server."""

    @pytest.mark.parametrize("use_trash", [True, False])
    def test_public_upload_removal_follows_use_trash(self, tmp_path, monkeypatch, use_trash):
        import os
        import types

        from jupyterlab_share_files_extension import routes, storage

        store = storage.RequestStore(str(tmp_path))
        req = store.create("Inbox")
        store.add_upload(req["id"], "HASHAB", "alice", "a.txt", b"x")
        trashed = []
        monkeypatch.setattr(storage, "_send2trash", lambda p: (trashed.append(p), os.remove(p)))
        app = types.SimpleNamespace(
            settings={
                "share_files_config": ShareFilesConfig(use_trash=use_trash),
                "server_root_dir": str(tmp_path),
            }
        )
        handler = object.__new__(routes.PublicRequestUploadHandler)
        handler.application = app
        handler.request = types.SimpleNamespace(headers={})
        handler.get_cookie = lambda name, default="": "HASHAB"
        handler.get_argument = lambda name, default="": "a.txt"
        handler.set_status = lambda code: setattr(handler, "status", code)
        handler.set_header = lambda *a: None
        handler.finish = lambda payload="": setattr(handler, "payload", payload)
        handler.delete(req["id"])
        assert handler.payload == '{"ok": true}'
        assert len(trashed) == (1 if use_trash else 0)
        assert store.get(req["id"])["upload_count"] == 0

        panel = object.__new__(routes.RequestUploadsHandler)
        panel.application = app
        assert panel.request_store.use_trash is handler.request_store.use_trash is use_trash
        assert panel.share_store.use_trash is handler.share_store.use_trash is use_trash
