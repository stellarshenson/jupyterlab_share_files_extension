"""Unit tests for public-origin detection and link rewriting.

Exercises `_configured_public_origin` / `_public_origin` /
`_own_link_prefixes` directly with stub handlers - no Tornado server. The
configured origin comes either from the `public_base_url` trait or from the
CLI config file written by `cloudflare --setup`; with neither, links keep the
old behaviour (the host the browser is on).
"""

from __future__ import annotations

import json
import os

import pytest

from jupyterlab_share_files_extension import cli, routes
from jupyterlab_share_files_extension.config import ShareFilesConfig


class _FakeRequest:
    def __init__(self, headers=None, protocol="http", host="hub.local:8000"):
        self.headers = headers or {}
        self.protocol = protocol
        self.host = host


class _FakeHandler:
    def __init__(self, cfg=None, base_url="/user/alice/", **request_kwargs):
        self.request = _FakeRequest(**request_kwargs)
        self.settings = {"share_files_config": cfg, "base_url": base_url}


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def _write_cli_config(values: dict) -> None:
    path = cli.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values), encoding="utf-8")


def test_no_config_falls_back_to_request_host(config_home):
    handler = _FakeHandler()
    assert routes._public_origin(handler) == "http://hub.local:8000"


def test_no_config_honours_forwarded_headers(config_home):
    handler = _FakeHandler(
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "hub.example.com"}
    )
    assert routes._public_origin(handler) == "https://hub.example.com"


def test_cli_config_public_base_url_wins(config_home):
    _write_cli_config({"public_base_url": "https://share.example.com"})
    handler = _FakeHandler(headers={"X-Forwarded-Host": "hub.example.com"})
    assert routes._public_origin(handler) == "https://share.example.com"


def test_trait_beats_cli_config(config_home):
    _write_cli_config({"public_base_url": "https://share.example.com"})
    cfg = ShareFilesConfig(public_base_url="https://override.example.com")
    handler = _FakeHandler(cfg=cfg)
    assert routes._public_origin(handler) == "https://override.example.com"


def test_configured_origin_keeps_scheme_and_host_only(config_home):
    cfg = ShareFilesConfig(public_base_url="https://share.example.com/some/path")
    handler = _FakeHandler(cfg=cfg)
    assert routes._configured_public_origin(handler) == "https://share.example.com"


def test_reset_reverts_to_old_behaviour(config_home):
    """After `cloudflare --reset` removes public_base_url the next request
    falls back to the request host - no restart needed (mtime cache)."""
    _write_cli_config({"public_base_url": "https://share.example.com"})
    handler = _FakeHandler()
    assert routes._public_origin(handler) == "https://share.example.com"

    _write_cli_config({})
    # force a distinct mtime - back-to-back writes can land in the same tick
    path = cli.config_path()
    stamp = path.stat().st_mtime + 10
    os.utime(path, (stamp, stamp))
    assert routes._public_origin(handler) == "http://hub.local:8000"


def test_share_url_uses_configured_origin_with_detected_path(config_home):
    _write_cli_config({"public_base_url": "https://share.example.com"})
    handler = _FakeHandler(base_url="/user/alice/")
    link = routes._public_share_url(handler, "ABC123")
    assert link == (
        "https://share.example.com/user/alice/"
        "jupyterlab-share-files-extension/public/share/ABC123"
    )


def test_own_cloudflare_link_counts_as_self(config_home):
    _write_cli_config({"public_base_url": "https://share.example.com"})
    handler = _FakeHandler(base_url="/user/alice/")
    prefixes = routes._own_link_prefixes(handler)
    assert "https://share.example.com/user/alice/" in prefixes
    assert "http://hub.local:8000/user/alice/" in prefixes


def test_tunnel_inactive_reverts_links_to_private(config_home):
    """The tunnel toggle (api/tunnel, cloudflare start/stop): tunnel_active
    false -> links carry the private (request) address even though
    public_base_url stays configured; flipping it back restores public
    links without a restart."""
    _write_cli_config(
        {"public_base_url": "https://share.example.com", "tunnel_active": False}
    )
    handler = _FakeHandler()
    assert routes._public_origin(handler) == "http://hub.local:8000"
    path = cli.config_path()
    _write_cli_config(
        {"public_base_url": "https://share.example.com", "tunnel_active": True}
    )
    os.utime(path, (os.path.getmtime(path) + 2,) * 2)
    assert routes._public_origin(handler) == "https://share.example.com"


def test_own_cloudflare_link_recognised_while_tunnel_off(config_home):
    """Self-connect detection is toggle-independent: one's own Cloudflare
    link is still 'us' while the tunnel is switched off."""
    _write_cli_config(
        {"public_base_url": "https://share.example.com", "tunnel_active": False}
    )
    handler = _FakeHandler()
    assert (
        "https://share.example.com/user/alice/"
        in routes._own_link_prefixes(handler)
    )
