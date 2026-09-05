"""Hub mode without a server: the route-table invariants, the hub client,
the pure translation helpers and the load-time behaviour.

Handler behaviour against a fake hub is in test_hub_handlers.py.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest
import tornado.httpserver
import tornado.web
from traitlets.config import Configurable
from jupyter_server.base.handlers import APIHandler
from tornado.web import StaticFileHandler

import jupyterlab_share_files_extension as ext
from jupyterlab_share_files_extension import hub, hub_routes, hub_stream, routes
from jupyterlab_share_files_extension.config import ShareFilesConfig
from jupyterlab_share_files_extension.routes import _PublicBase

HUB_ENV = {
    "SHARE_FILES_PUBLIC_ZONE": "hub",
    "SHARE_FILES_HUB_API": "/hub/api/fileshare",
    "JUPYTERHUB_API_URL": "http://hub:8080/hub/api",
    "JUPYTERHUB_API_TOKEN": "t0k3n",
    "JUPYTERHUB_BASE_URL": "/",
}
HTTP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")


class _FakeServerApp(Configurable):
    """Configurable so ShareFilesConfig(parent=...) accepts it."""


class _FakeApp:
    def __init__(self):
        self.settings = {"base_url": "/user/alice/"}
        self.handlers: list = []

    def add_handlers(self, host_pattern, handlers):
        self.handlers.extend(handlers)


@pytest.fixture
def hub_env(monkeypatch, tmp_path):
    for key, value in HUB_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))


@pytest.fixture
def standalone_env(monkeypatch, tmp_path):
    for key in HUB_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))


def _table() -> list:
    app = _FakeApp()
    routes.setup_route_handlers(app, config=ShareFilesConfig())
    return app.handlers


# --------------------------------------------------------------------------- #
# Route table
# --------------------------------------------------------------------------- #


def test_hub_mode_is_read_from_the_spawn_variable(monkeypatch):
    monkeypatch.setenv("SHARE_FILES_PUBLIC_ZONE", "hub")
    assert hub.hub_mode()
    monkeypatch.setenv("SHARE_FILES_PUBLIC_ZONE", "HUB ")
    assert hub.hub_mode()
    monkeypatch.setenv("SHARE_FILES_PUBLIC_ZONE", "standalone")
    assert not hub.hub_mode()
    monkeypatch.delenv("SHARE_FILES_PUBLIC_ZONE")
    assert not hub.hub_mode()


def test_hub_mode_registers_no_public_and_no_static_route(hub_env):
    table = _table()
    assert table, "the hub table is empty"
    for spec in table:
        pattern, handler = spec[0], spec[1]
        assert "/public/" not in pattern, pattern
        assert "/static/" not in pattern, pattern
        assert not issubclass(handler, _PublicBase), handler
        assert not issubclass(handler, StaticFileHandler), handler
        assert "/api/" in pattern, pattern


def test_every_hub_route_method_is_authenticated(hub_env):
    """Every HTTP method the extension itself implements on a registered
    handler carries the ``tornado.web.authenticated`` wrapper."""
    checked = 0
    for spec in _table():
        handler = spec[1]
        assert issubclass(handler, APIHandler), handler
        for cls in handler.__mro__:
            if not cls.__module__.startswith("jupyterlab_share_files_extension"):
                break
            for name in HTTP_METHODS:
                if name in cls.__dict__:
                    fn = cls.__dict__[name]
                    assert hasattr(fn, "__wrapped__"), f"{cls.__name__}.{name} is not authenticated"
                    checked += 1
    assert checked >= 15


def test_standalone_table_keeps_public_and_static_routes(standalone_env):
    table = _table()
    patterns = [spec[0] for spec in table]
    assert any("/public/share/" in p for p in patterns)
    assert any(spec[1] is StaticFileHandler for spec in table)
    assert any(issubclass(spec[1], _PublicBase) for spec in table)
    assert len(table) == 27


@pytest.mark.parametrize("missing", ["JUPYTERHUB_API_TOKEN", "SHARE_FILES_HUB_API"])
def test_incomplete_contract_fails_closed(hub_env, monkeypatch, missing):
    monkeypatch.delenv(missing)
    table = _table()
    assert table
    assert not any("/public/" in spec[0] for spec in table)
    with pytest.raises(hub.HubUnavailable):
        hub.HubClient()


# --------------------------------------------------------------------------- #
# Load time
# --------------------------------------------------------------------------- #


def test_load_in_hub_mode_creates_no_store_and_starts_no_tunnel(hub_env, monkeypatch, tmp_path):
    from jupyterlab_share_files_extension import tunnel

    root = tmp_path / "workspace"
    root.mkdir()
    called = []
    monkeypatch.setattr(tunnel, "apply_autostart", lambda *a, **k: called.append(a))
    web_app = _FakeApp()
    web_app.settings["server_root_dir"] = str(root)
    server_app = _FakeServerApp()
    server_app.web_app = web_app
    server_app.log = types.SimpleNamespace(info=lambda *a, **k: None)
    ext._load_jupyter_server_extension(server_app)
    assert called == []
    assert not (root / "uploads").exists()
    assert web_app.handlers and not any("/public/" in s[0] for s in web_app.handlers)


# --------------------------------------------------------------------------- #
# Hub client
# --------------------------------------------------------------------------- #


def test_hub_api_base_joins_the_path_with_the_api_origin(monkeypatch):
    monkeypatch.setenv("SHARE_FILES_HUB_API", "/hub/api/fileshare")
    monkeypatch.setenv("JUPYTERHUB_API_URL", "http://hub:8080/hub/api")
    assert hub.hub_api_base() == "http://hub:8080/hub/api/fileshare"
    assert hub.hub_api_origin() == "http://hub:8080"
    monkeypatch.setenv("SHARE_FILES_HUB_API", "https://hub.example.com/hub/api/fileshare/")
    assert hub.hub_api_base() == "https://hub.example.com/hub/api/fileshare"
    monkeypatch.setenv("SHARE_FILES_HUB_API", "/hub/api/fileshare")
    monkeypatch.delenv("JUPYTERHUB_API_URL")
    assert hub.hub_api_base() == ""
    monkeypatch.delenv("SHARE_FILES_HUB_API")
    assert hub.hub_api_base() == ""


class _FakeResponse:
    def __init__(self, code=200, body=b"{}", error=None):
        self.code = code
        self.body = body
        self.error = error


class _FakeClient:
    """Stands in for tornado's AsyncHTTPClient; records the request."""

    def __init__(self, response=None, raises=None):
        self.response = response or _FakeResponse()
        self.raises = raises
        self.calls: list[dict] = []

    async def fetch(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.raises:
            raise self.raises
        return self.response


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_every_hub_call_carries_the_lab_token(hub_env, monkeypatch):
    fake = _FakeClient(_FakeResponse(200, b'{"allow_share": true}'))
    monkeypatch.setattr(hub.tornado.httpclient, "AsyncHTTPClient", lambda: fake)
    code, data = _run(hub.HubClient().request("GET", "capabilities"))
    assert (code, data) == (200, {"allow_share": True})
    call = fake.calls[0]
    assert call["url"] == "http://hub:8080/hub/api/fileshare/capabilities"
    assert call["headers"] == {"Authorization": "token t0k3n"}
    assert call["body"] is None

    fake.calls.clear()
    _run(hub.HubClient().request("POST", "shares", {"title": "x", "paths": ["a"]}))
    call = fake.calls[0]
    assert call["headers"]["Authorization"] == "token t0k3n"
    assert call["headers"]["Content-Type"] == "application/json"
    assert json.loads(call["body"]) == {"title": "x", "paths": ["a"]}
    assert call["method"] == "POST"


def test_hub_client_maps_transport_failures_to_unavailable(hub_env, monkeypatch):
    monkeypatch.setattr(
        hub.tornado.httpclient, "AsyncHTTPClient",
        lambda: _FakeClient(raises=ConnectionRefusedError("refused")),
    )
    with pytest.raises(hub.HubUnavailable):
        _run(hub.HubClient().request("GET", "capabilities"))
    monkeypatch.setattr(
        hub.tornado.httpclient, "AsyncHTTPClient",
        lambda: _FakeClient(_FakeResponse(599, b"", error=TimeoutError("t"))),
    )
    with pytest.raises(hub.HubUnavailable):
        _run(hub.HubClient().request("GET", "capabilities"))


def test_hub_client_returns_http_errors_as_data(hub_env, monkeypatch):
    body = b'{"reason": "not_granted", "message": "Cannot create this share: not_granted"}'
    monkeypatch.setattr(hub.tornado.httpclient, "AsyncHTTPClient", lambda: _FakeClient(_FakeResponse(403, body)))
    assert _run(hub.HubClient().request("POST", "shares", {})) == (403, json.loads(body))
    monkeypatch.setattr(hub.tornado.httpclient, "AsyncHTTPClient", lambda: _FakeClient(_FakeResponse(204, b"")))
    assert _run(hub.HubClient().request("DELETE", "shares/x")) == (204, {})


# --------------------------------------------------------------------------- #
# Translation
# --------------------------------------------------------------------------- #

LIVE_SHARE = {
    "id": "9LEqaQ_QZgKxjj0De_u1wA", "kind": "share", "owner": "konrad.jelen",
    "title": "live probe share", "state": "ready",
    "files": [{"name": "fileshare-probe.txt", "size": 42, "sha256": "219b64e7"}],
    "bytes": 42, "skipped": 0, "created_at": "2026-09-03T20:43:41Z",
    "expires_at": "2026-09-17T20:43:41Z", "has_password": False,
    "url": "http://hub:8080/s/9LEqaQ_QZgKxjj0De_u1wA",
}
LIVE_REQUEST = {
    "id": "r_PhcmNOMGM_zLhkr7bsuA", "kind": "request", "owner": "konrad.jelen",
    "title": "live probe request", "state": "ready", "files": [], "bytes": 0,
    "skipped": 0, "created_at": "2026-09-03T20:43:05Z",
    "expires_at": "2026-09-17T20:43:05Z", "has_password": True,
    "url": "http://hub:8080/s/r_PhcmNOMGM_zLhkr7bsuA",
}


def _epoch(stamp: str) -> int:
    import datetime as dt
    return int(dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())


def test_share_from_item_maps_the_live_payload():
    row = hub_routes.share_from_item(LIVE_SHARE, "https://hub.example.com/s/9LEqaQ_QZgKxjj0De_u1wA")
    assert row["id"] == LIVE_SHARE["id"]
    assert row["name"] == "live probe share"
    assert row["kind"] == "share"
    assert row["entries"] == [{"name": "fileshare-probe.txt", "type": "file", "size": 42}]
    assert row["link"] == "https://hub.example.com/s/9LEqaQ_QZgKxjj0De_u1wA"
    assert row["state"] == "ready"
    assert row["reason"] == ""
    assert row["has_password"] is False
    assert row["cloud"] is False
    assert hub_routes.share_from_item({**LIVE_SHARE, "cloud": True}, "")["cloud"] is True
    assert row["created_at"] == _epoch("2026-09-03T20:43:41Z")
    assert row["expires_at"] == _epoch("2026-09-03T20:43:41Z") + 14 * 86400
    assert row["bytes"] == 42


def test_share_from_item_keeps_the_refusal_reason():
    row = hub_routes.share_from_item({**LIVE_SHARE, "state": "refused", "reason": "over_cap"}, "")
    assert (row["state"], row["reason"]) == ("refused", "over_cap")


def test_request_from_item_groups_uploads_and_derives_counts():
    uploads = [
        {"upload_id": "u1", "filename": "a.csv", "size": 10, "sha256": "x", "uploaded_at": "2026-09-03T21:00:00Z"},
        {"upload_id": "u2", "filename": "b.csv", "size": 20, "sha256": "y", "uploaded_at": "2026-09-03T22:00:00Z"},
    ]
    row = hub_routes.request_from_item(LIVE_REQUEST, uploads, "https://h/s/r_x", last_seen=5)
    assert row["kind"] == "request"
    assert row["upload_count"] == 2
    assert row["last_upload_at"] == _epoch("2026-09-03T22:00:00Z")
    assert row["last_seen_upload_at"] == 5
    assert row["has_password"] is True
    assert len(row["uploaders"]) == 1
    entries = row["uploaders"][0]["entries"]
    assert [e["upload_id"] for e in entries] == ["u1", "u2"]
    assert entries[1] == {"name": "b.csv", "type": "file", "size": 20, "upload_id": "u2", "mtime": _epoch("2026-09-03T22:00:00Z")}
    empty = hub_routes.request_from_item(LIVE_REQUEST, [], "")
    assert empty["uploaders"] == [] and empty["upload_count"] == 0 and empty["last_upload_at"] == 0


def test_relay_error_keeps_status_and_names_the_reason():
    assert hub_routes.relay_error(403, {"reason": "not_granted", "message": "no"}) == (
        403, {"error": "no", "reason": "not_granted"})
    assert hub_routes.relay_error(400, {"status": 400, "message": "bad path"}) == (400, {"error": "bad path"})
    assert hub_routes.relay_error(429, {"reason": "busy", "message": "later"})[1]["reason"] == "busy"
    assert hub_routes.relay_error(503, "not json") == (503, {"error": "the hub answered 503"})


# --------------------------------------------------------------------------- #
# Links and the cloud toggle
# --------------------------------------------------------------------------- #


def test_rewrite_link_restores_the_browser_facing_origin(hub_env):
    internal = "http://hub:8080/s/ABCDEF12"
    assert hub_routes.rewrite_link(internal, "https://hub.example.com") == "https://hub.example.com/s/ABCDEF12"
    # the tunnel hostname is the hub's choice for a record switched on - kept
    tunnel = "https://share.example.com/s/ABCDEF12"
    assert hub_routes.rewrite_link(tunnel, "https://hub.example.com") == tunnel
    assert hub_routes.rewrite_link("", "https://hub.example.com") == ""


def test_cloud_default_persists_in_the_config_file(hub_env):
    assert hub_routes.cloud_default() is False
    hub_routes.set_cloud_default(True)
    assert hub_routes.cloud_default() is True
    hub_routes.set_cloud_default(False)
    assert hub_routes.cloud_default() is False


# --------------------------------------------------------------------------- #
# The change stream relay
# --------------------------------------------------------------------------- #


def test_parse_events_reads_named_events_across_chunks():
    buf = bytearray()
    assert hub_stream.parse_events(buf, b"retry: 5000\n\n: keepalive\n\nevent: changed\ndata:\n\nevent: cha") == ["changed"]
    assert bytes(buf) == b"event: cha"
    assert hub_stream.parse_events(buf, b"nged\ndata:\n\n") == ["changed"]
    assert bytes(buf) == b""


def test_relay_holds_one_hub_stream_for_all_panels_and_drops_it_with_the_last(monkeypatch):
    log = []

    async def hold(on_open, on_event):
        log.append("open")
        on_open()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            log.append("cancelled")
            raise
        return 599

    monkeypatch.setattr(hub_stream, "hold", hold)

    async def scenario():
        relay = hub_stream.Relay()
        a = relay.subscribe()
        b = relay.subscribe()
        await asyncio.sleep(0.01)
        assert log == ["open"] and relay.connected
        # the open rang both; a second ring while one waits is dropped, not queued
        assert a.get_nowait() == hub_stream.CHANGED and b.get_nowait() == hub_stream.CHANGED
        relay.ring(hub_stream.CHANGED)
        relay.ring(hub_stream.CHANGED)
        assert a.qsize() == 1
        relay.unsubscribe(a)
        await asyncio.sleep(0.01)
        assert relay.connected  # b still listens
        relay.unsubscribe(b)
        await asyncio.sleep(0.01)
        assert log == ["open", "cancelled"] and not relay.connected

    asyncio.run(scenario())


def test_relay_answers_poll_on_an_older_hub_and_retries_only_on_resubscribe(monkeypatch):
    opens = []

    async def hold(on_open, on_event):
        opens.append(1)
        return 404

    monkeypatch.setattr(hub_stream, "hold", hold)

    async def scenario():
        relay = hub_stream.Relay()
        a = relay.subscribe()
        await asyncio.sleep(0.01)
        assert await asyncio.wait_for(a.get(), 1) == hub_stream.POLL
        b = relay.subscribe()  # the verdict is remembered while a listens
        assert b.get_nowait() == hub_stream.POLL and len(opens) == 1
        relay.unsubscribe(a)
        relay.unsubscribe(b)
        c = relay.subscribe()  # a fresh subscription asks the hub again
        await asyncio.sleep(0.01)
        assert len(opens) == 2 and c.get_nowait() == hub_stream.POLL
        relay.unsubscribe(c)

    asyncio.run(scenario())


def test_relay_retries_an_unreachable_hub_while_a_panel_listens(monkeypatch):
    opens = []

    async def hold(on_open, on_event):
        opens.append(1)
        raise hub.HubUnavailable("refused")

    monkeypatch.setattr(hub_stream, "hold", hold)
    monkeypatch.setattr(hub_stream, "RETRY_SECONDS", 0.01)

    async def scenario():
        relay = hub_stream.Relay()
        a = relay.subscribe()
        await asyncio.sleep(0.1)
        assert len(opens) >= 3 and a.empty()
        relay.unsubscribe(a)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# hold(): the raw stream read against an in-process hub
# --------------------------------------------------------------------------- #


class _SseHub:
    """A tornado app speaking the hub's stream route: `retry:` on open, one
    `event: changed` per ring, and it records when the lab hangs up."""

    def __init__(self, status=200):
        self.status = status
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = asyncio.Event()
        self.auth = ""
        hub = self

        class Stream(tornado.web.RequestHandler):
            async def get(self):
                hub.auth = self.request.headers.get("Authorization", "")
                if hub.status != 200:
                    self.set_status(hub.status)
                    return self.finish(json.dumps({"status": hub.status}))
                self.set_header("Content-Type", "text/event-stream")
                self.write("retry: 5000\n\n")
                await self.flush()
                while True:
                    self.write(await hub.queue.get())
                    await self.flush()

            def on_connection_close(self):
                hub.closed.set()

        self.app = tornado.web.Application([(r"/hub/api/fileshare/stream", Stream)])

    def listen(self):
        from tornado.testing import bind_unused_port
        sock, port = bind_unused_port()
        server = tornado.httpserver.HTTPServer(self.app)
        server.add_socket(sock)
        return server, port


def test_hold_reads_the_hub_stream_and_the_cancel_closes_the_socket(monkeypatch, tmp_path):
    for key, value in HUB_ENV.items():
        monkeypatch.setenv(key, value)

    async def scenario():
        sse = _SseHub()
        server, port = sse.listen()
        monkeypatch.setenv("SHARE_FILES_HUB_API", f"http://127.0.0.1:{port}/hub/api/fileshare")
        seen = []
        opened = asyncio.Event()
        task = asyncio.ensure_future(hub_stream.hold(opened.set, seen.append))
        await asyncio.wait_for(opened.wait(), 5)
        assert sse.auth == "token t0k3n"
        sse.queue.put_nowait(": keepalive\n\n")
        sse.queue.put_nowait("event: changed\ndata:\n\n")
        sse.queue.put_nowait("event: changed\ndata:\n\n")
        for _ in range(50):
            if len(seen) == 2:
                break
            await asyncio.sleep(0.02)
        assert seen == ["changed", "changed"]
        assert not sse.closed.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # the hub sees the lab hang up at once - no slot is held behind
        await asyncio.wait_for(sse.closed.wait(), 5)
        server.stop()

    asyncio.run(scenario())


def test_hold_returns_the_hubs_status_and_unavailability(monkeypatch):
    for key, value in HUB_ENV.items():
        monkeypatch.setenv(key, value)

    async def scenario():
        sse = _SseHub(status=404)
        server, port = sse.listen()
        monkeypatch.setenv("SHARE_FILES_HUB_API", f"http://127.0.0.1:{port}/hub/api/fileshare")
        assert await hub_stream.hold(lambda: None, lambda _n: None) == 404
        server.stop()
        monkeypatch.setenv("SHARE_FILES_HUB_API", "http://127.0.0.1:9/hub/api/fileshare")
        with pytest.raises(hub.HubUnavailable):
            await hub_stream.hold(lambda: None, lambda _n: None)

    asyncio.run(scenario())
