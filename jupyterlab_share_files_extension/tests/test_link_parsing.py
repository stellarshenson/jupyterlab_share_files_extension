"""Unit tests for share-link parsing and self-connect detection.

These exercise the pure-Python `_parse_share_link` helper directly - no
Tornado, no fixture - so they run in any environment.

The self-connect detection is the load-bearing logic here: on JupyterHub
two users share a host but live at different `/user/<name>/` prefixes,
so a plain host compare would wrongly flag every link from another user
on the same hub as the recipient's own and block legitimate uploads.
"""

from __future__ import annotations

import pytest

from jupyterlab_share_files_extension.routes import _parse_share_link


class TestParseShareLink:
    def test_standalone_share_link(self):
        link = "http://localhost:8888/jupyterlab-share-files-extension/public/share/ABCDEF23"
        out = _parse_share_link(link)
        assert out["kind"] == "share"
        assert out["id"] == "ABCDEF23"
        assert out["host"] == "http://localhost:8888"
        assert out["base_path"] == "/"

    def test_standalone_request_link(self):
        link = "https://lab.example.com/jupyterlab-share-files-extension/public/request/Q7IITTPI"
        out = _parse_share_link(link)
        assert out["kind"] == "request"
        assert out["id"] == "Q7IITTPI"
        assert out["host"] == "https://lab.example.com"
        assert out["base_path"] == "/"

    def test_jupyterhub_user_prefix_is_captured(self):
        """The user prefix `/user/alice/` is what separates one hub user
        from another - the parser must surface it so self-connect
        detection can compare it."""
        link = (
            "https://hub.example.com/user/alice/jupyterlab-share-files-extension"
            "/public/share/ABCDEF23"
        )
        out = _parse_share_link(link)
        assert out["host"] == "https://hub.example.com"
        assert out["base_path"] == "/user/alice/"
        assert out["kind"] == "share"
        assert out["id"] == "ABCDEF23"

    def test_nested_user_prefix(self):
        """Some setups front the hub with another path - capture all of it."""
        link = (
            "https://corp.example.com/jhub/user/bob/jupyterlab-share-files-extension"
            "/public/request/XYZ12345"
        )
        out = _parse_share_link(link)
        assert out["base_path"] == "/jhub/user/bob/"

    def test_rejects_non_absolute_url(self):
        with pytest.raises(ValueError, match="absolute URL"):
            _parse_share_link("/relative/path")

    def test_rejects_link_without_public_marker(self):
        with pytest.raises(ValueError, match="not a share/request URL"):
            _parse_share_link("http://localhost:8888/lab")

    def test_rejects_unknown_kind(self):
        link = "http://localhost:8888/jupyterlab-share-files-extension/public/garbage/ID"
        with pytest.raises(ValueError, match="Unknown kind"):
            _parse_share_link(link)


class TestSelfConnectDetection:
    """The recipient-side check: a parsed link belongs to us iff its
    (host + base_path) matches our own. Validates the exact comparison
    logic used in `ConnectionsHandler.post`."""

    @staticmethod
    def _is_self(parsed: dict, own_protocol: str, own_host: str, own_base: str) -> bool:
        if not own_base.endswith("/"):
            own_base += "/"
        own_prefix = own_protocol + "://" + own_host + own_base
        link_prefix = parsed["host"] + parsed.get("base_path", "/")
        return link_prefix == own_prefix

    def test_same_user_same_hub_is_self(self):
        link = (
            "https://hub.example.com/user/alice/jupyterlab-share-files-extension"
            "/public/share/ABCDEF23"
        )
        parsed = _parse_share_link(link)
        assert self._is_self(parsed, "https", "hub.example.com", "/user/alice/")

    def test_different_user_same_hub_is_NOT_self(self):
        """The reported bug - alice should be able to accept bob's link
        even though they share a host. Pre-fix this returned True."""
        bob_link = (
            "https://hub.example.com/user/bob/jupyterlab-share-files-extension"
            "/public/request/Q7IITTPI"
        )
        parsed = _parse_share_link(bob_link)
        assert not self._is_self(parsed, "https", "hub.example.com", "/user/alice/")

    def test_same_user_different_host_is_NOT_self(self):
        link = (
            "https://other.example.com/user/alice/jupyterlab-share-files-extension"
            "/public/share/ABCDEF23"
        )
        parsed = _parse_share_link(link)
        assert not self._is_self(parsed, "https", "hub.example.com", "/user/alice/")

    def test_standalone_self(self):
        link = "http://localhost:8888/jupyterlab-share-files-extension/public/share/X"
        parsed = _parse_share_link(link)
        assert self._is_self(parsed, "http", "localhost:8888", "/")

    def test_standalone_vs_jupyterhub_user_NOT_self(self):
        """Standalone server pasting a hub user's link must not self-block."""
        link = (
            "https://hub.example.com/user/alice/jupyterlab-share-files-extension"
            "/public/share/X"
        )
        parsed = _parse_share_link(link)
        assert not self._is_self(parsed, "http", "localhost:8888", "/")

    def test_own_base_without_trailing_slash_still_compares(self):
        """`base_url` from settings may or may not have the trailing
        slash - the comparison must normalise so both forms match."""
        link = (
            "https://hub.example.com/user/alice/jupyterlab-share-files-extension"
            "/public/share/X"
        )
        parsed = _parse_share_link(link)
        assert self._is_self(parsed, "https", "hub.example.com", "/user/alice")
