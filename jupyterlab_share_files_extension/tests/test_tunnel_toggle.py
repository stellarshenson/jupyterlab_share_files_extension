"""The standalone Cloudflare toggle (api/tunnel): links go public only while a
connector serves them, and the handler runs the connector work off the event
loop."""

from __future__ import annotations

import json

import pytest
from tornado.httpclient import HTTPClientError

from jupyterlab_share_files_extension import tunnel

pytest_plugins = ["pytest_jupyter.jupyter_server"]

NS = "jupyterlab-share-files-extension"


@pytest.fixture
def configured_tunnel(monkeypatch, tmp_path):
    # standalone mode: the developer's own lab may run under a hub
    monkeypatch.delenv("SHARE_FILES_PUBLIC_ZONE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    tunnel._save_config(
        {
            "cloudflare_tunnel_token": "conn-token",
            "public_base_url": "https://share.example.com",
            "tunnel_active": False,
        }
    )
    stopped = []
    monkeypatch.setattr(tunnel, "stop_connector", lambda: stopped.append(True) or True)
    monkeypatch.setattr(tunnel, "_connector_running", lambda: False)
    return stopped


@pytest.fixture
def jp_server_config(configured_tunnel, jp_server_config):
    return {"ServerApp": {"jpserver_extensions": {"jupyterlab_share_files_extension": True}}}


async def _toggle(jp_fetch, active):
    return await jp_fetch(NS, "api", "tunnel", method="POST", body=json.dumps({"active": active}))


async def test_switching_on_without_a_connector_is_refused_and_links_stay_private(
    jp_fetch, configured_tunnel, monkeypatch
):
    monkeypatch.setattr(tunnel, "ensure_connector", lambda *a, **kw: False)
    with pytest.raises(HTTPClientError) as err:
        await _toggle(jp_fetch, True)
    assert err.value.code == 400
    body = json.loads(err.value.response.body)
    assert body["error"] == f"cloudflared did not start - see {tunnel.CONNECTOR_LOG}"
    assert json.loads(tunnel.config_path().read_text())["tunnel_active"] is False
    assert configured_tunnel == [True]


async def test_switching_on_with_a_connector_marks_the_links_public(
    jp_fetch, configured_tunnel, monkeypatch
):
    monkeypatch.setattr(tunnel, "ensure_connector", lambda *a, **kw: True)
    resp = await _toggle(jp_fetch, True)
    assert resp.code == 200
    assert json.loads(resp.body)["tunnel_active"] is True
    assert json.loads(tunnel.config_path().read_text())["tunnel_active"] is True
    assert configured_tunnel == []


async def test_switching_off_stops_the_connector(jp_fetch, configured_tunnel):
    resp = await _toggle(jp_fetch, False)
    assert resp.code == 200
    assert json.loads(resp.body)["tunnel_active"] is False
    assert configured_tunnel == [True]
