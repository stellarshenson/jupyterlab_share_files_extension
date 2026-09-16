"""Hub-mode handlers driven through a live jupyter_server against a fake hub.

The extension is loaded with the spawn contract in the environment, so the
hub table is what the server mounts; ``HubClient`` is replaced by an
in-memory hub that records every call. Nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import errno
import json
import re
import socket
import types

import pytest
import tornado.httpserver
import tornado.web
from tornado.httpclient import AsyncHTTPClient, HTTPClientError
from tornado.simple_httpclient import HTTPTimeoutError
from tornado.testing import bind_unused_port

from jupyterlab_share_files_extension import hub_routes, hub_stream, routes
from jupyterlab_share_files_extension.hub import HubClient, HubUnavailable

pytest_plugins = ["pytest_jupyter.jupyter_server"]

NS = "jupyterlab-share-files-extension"
HUB_ENV = {
    "SHARE_FILES_PUBLIC_ZONE": "hub",
    "SHARE_FILES_HUB_API": "/hub/api/fileshare",
    "JUPYTERHUB_API_URL": "http://hub:8080/hub/api",
    "JUPYTERHUB_API_TOKEN": "t0k3n",
    "JUPYTERHUB_BASE_URL": "/",
}


class FakeHub:
    """The hub fileshare API in memory, one user, recording every call."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.capabilities = {
            "allow_share": True, "allow_request": True,
            "max_upload_bytes": 10, "max_share_bytes": 5, "max_shares": 20,
            "retention_days": 14, "public_base_url": "http://hub:8080", "serving": True,
            "password_required": False,
        }
        self.cloudflare_enabled = True
        # the hub's own address, and its Cloudflare tunnel: a cloud-on record's
        # url carries the tunnel hostname only while the tunnel is registered
        self.own_base = "http://hub:8080"
        self.tunnel_base = "https://share.example.com"
        self.tunnel_registered = True
        self.items: list[dict] = []
        self.uploads: dict[str, list[dict]] = {}
        self.overrides: dict[tuple[str, str], tuple[int, dict]] = {}
        self.unavailable = False
        self.raise_for: set[str] = set()
        self.counter = 0

    def _new_id(self, prefix=""):
        self.counter += 1
        return f"{prefix}Fake_id_{self.counter:04d}"

    def _with_url(self, item):
        """The tunnel hostname while the record's cloud switch is on and the
        tunnel is registered, the hub's own address otherwise
        (`record_base_url`, galaxahub v4.4.58)."""
        on_tunnel = item.get("cloud") and self.tunnel_registered
        base = self.tunnel_base if on_tunnel else self.own_base
        return {**item, "url": f"{base}/s/{item['id']}"}

    async def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.unavailable or path in self.raise_for:
            raise HubUnavailable("could not reach the hub: refused")
        if (method, path) in self.overrides:
            return self.overrides[(method, path)]
        if method == "GET" and path == "capabilities":
            return 200, dict(self.capabilities)
        if method == "GET" and path == "items":
            return 200, {"items": [self._with_url(i) for i in self.items]}
        if method == "POST" and path in ("shares", "requests"):
            kind = "share" if path == "shares" else "request"
            id_ = self._new_id("r_" if kind == "request" else "")
            state = "staging" if kind == "share" else "ready"
            if self.capabilities.get("password_required") and not (body.get("password") or "").strip():
                return 400, {"reason": "password_required", "message": "Your group requires a password"}
            self.items.append({
                "id": id_, "kind": kind, "owner": "alice", "title": body["title"],
                "state": state, "files": [], "bytes": 0, "skipped": 0,
                "created_at": "2026-09-03T20:00:00Z", "expires_at": "2026-09-17T20:00:00Z",
                "has_password": bool(body.get("password")), "cloud": False,
            })
            return (202 if kind == "share" else 201), {
                "id": id_, "url": f"{self.own_base}/s/{id_}", "state": state}
        m = re.fullmatch(r"(shares|requests)/([^/]+)", path)
        if m and method == "DELETE":
            before = len(self.items)
            self.items = [i for i in self.items if i["id"] != m.group(2)]
            return (204, {}) if len(self.items) < before else (404, {"status": 404, "message": "No such share"})
        m = re.fullmatch(r"(shares|requests)/([^/]+)/password", path)
        if m and method == "PUT":
            for item in self.items:
                if item["id"] == m.group(2):
                    item["has_password"] = bool(body.get("password"))
                    return 204, {}
            return 404, {"status": 404, "message": "No such share"}
        m = re.fullmatch(r"(shares|requests)/([^/]+)/cloud", path)
        if m and method == "PUT":
            for item in self.items:
                if item["id"] == m.group(2):
                    if body["cloud"] and not self.cloudflare_enabled:
                        return 403, {"reason": "cloud_not_configured", "message": "Cloudflare is off"}
                    item["cloud"] = bool(body["cloud"])
                    return 204, {}
            return 404, {"status": 404, "message": "No such share"}
        m = re.fullmatch(r"requests/([^/]+)/uploads", path)
        if m and method == "GET":
            return 200, {"uploads": list(self.uploads.get(m.group(1), []))}
        m = re.fullmatch(r"requests/([^/]+)/uploads/([^/]+)/fetch", path)
        if m and method == "POST":
            return 200, {"path": body["dest"] + "/report.csv"}
        return 404, {"status": 404, "message": None}


@pytest.fixture
def fake_hub(monkeypatch, tmp_path):
    for key, value in HUB_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    hub = FakeHub()
    monkeypatch.setattr(hub_routes, "HubClient", lambda: hub)
    monkeypatch.setattr(hub_routes, "CLOUD_WAIT", hub_routes.CloudWait())
    hub_routes._PASSWORDS.clear()
    return hub


@pytest.fixture
def jp_server_config(fake_hub, jp_server_config):
    return {"ServerApp": {"jpserver_extensions": {"jupyterlab_share_files_extension": True}}}


def _json(resp):
    return json.loads(resp.body)


async def _post(jp_fetch, *parts, body):
    return await jp_fetch(NS, *parts, method="POST", body=json.dumps(body))


# --------------------------------------------------------------------------- #


async def test_public_and_static_paths_are_404_in_hub_mode(jp_fetch, fake_hub):
    for parts in (
        ("public", "share", "AAAAAAAA"),
        ("public", "share", "AAAAAAAA", "manifest"),
        ("public", "request", "AAAAAAAA"),
        ("static", "standalone.html"),
        ("api", "connections"),
    ):
        with pytest.raises(HTTPClientError) as err:
            await jp_fetch(NS, *parts)
        assert err.value.code == 404, parts
    assert fake_hub.calls == []


async def test_info_reflects_capabilities(jp_fetch, fake_hub):
    fake_hub.capabilities.update({"allow_share": False, "reason": "share_not_granted", "serving": False})
    info = _json(await jp_fetch(NS, "api", "info"))
    assert info["mode"] == "hub"
    assert info["storage_path"] == ""
    # the toggle is always offered while the hub answers - the hub decides
    # per record whether Cloudflare may be switched on
    assert info["tunnel_configured"] is True
    assert info["tunnel_active"] is False
    assert info["tunnel_running"] is False
    assert info["public_base_url"] == ""
    assert info["hub"] == {
        "available": True, "allow_share": False, "allow_request": True,
        "reason": "share_not_granted", "serving": False, "password_required": False,
        "max_share_bytes": 5, "max_upload_bytes": 10, "max_shares": 20, "retention_days": 14,
    }


async def test_info_reports_an_unavailable_hub_without_failing(jp_fetch, fake_hub):
    fake_hub.unavailable = True
    info = _json(await jp_fetch(NS, "api", "info"))
    assert info["mode"] == "hub"
    assert info["hub"]["available"] is False
    assert info["hub"]["reason"] == "hub_unavailable"


async def test_create_share_sends_paths_and_returns_a_staging_row(jp_fetch, fake_hub):
    resp = await _post(jp_fetch, "api", "shares", body={"name": "Report", "paths": ["notes/report.csv"], "password": "pw"})
    row = _json(resp)
    assert ("POST", "shares", {"title": "Report", "paths": ["notes/report.csv"], "password": "pw"}) in fake_hub.calls
    assert row["state"] == "staging"
    assert row["name"] == "Report"
    assert row["kind"] == "share"
    assert row["entries"] == []
    assert row["has_password"] is True
    assert re.fullmatch(r"http://[^/]+/s/Fake_id_0001", row["link"]), row["link"]
    listing = _json(await jp_fetch(NS, "api", "shares"))
    assert [s["id"] for s in listing["shares"]] == [row["id"]]
    assert listing["shares"][0]["link"] == row["link"]


async def test_create_share_refuses_an_unsafe_path_before_calling_the_hub(jp_fetch, fake_hub):
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["../etc/passwd"]})
    assert err.value.code == 400
    assert not any(c[0] == "POST" for c in fake_hub.calls)


async def test_hub_refusal_is_relayed_with_its_reason(jp_fetch, fake_hub):
    fake_hub.overrides[("POST", "shares")] = (403, {"reason": "downloads_blocked", "message": "Cannot create this share: downloads_blocked"})
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]})
    assert err.value.code == 403
    body = json.loads(err.value.response.body)
    assert body == {"error": "Cannot create this share: downloads_blocked", "reason": "downloads_blocked"}


async def test_unreachable_hub_answers_502(jp_fetch, fake_hub):
    fake_hub.unavailable = True
    with pytest.raises(HTTPClientError) as err:
        await jp_fetch(NS, "api", "shares")
    assert err.value.code == 502
    assert json.loads(err.value.response.body)["reason"] == "hub_unavailable"


def _stall(fake_hub, monkeypatch, path):
    """Send the fake hub's ``GET <path>`` through the real client to a hub
    that accepts the connection and never answers; returns its socket."""
    monkeypatch.setattr("jupyterlab_share_files_extension.hub.REQUEST_TIMEOUT_SECONDS", 0.2)
    sock, port = bind_unused_port()  # listens; the kernel accepts, nobody answers
    stalled = HubClient(base=f"http://127.0.0.1:{port}/hub/api/fileshare", token="t0k3n")
    answer = fake_hub.request

    async def request(method, path_, body=None):
        if (method, path_) == ("GET", path):
            return await stalled.request(method, path_, body)
        return await answer(method, path_, body)

    monkeypatch.setattr(fake_hub, "request", request)
    return sock


async def test_a_hub_that_never_answers_answers_502(jp_fetch, fake_hub, monkeypatch):
    sock = _stall(fake_hub, monkeypatch, "items")
    try:
        with pytest.raises(HTTPClientError) as err:
            await jp_fetch(NS, "api", "shares")
    finally:
        sock.close()
    assert err.value.code == 502
    assert json.loads(err.value.response.body)["reason"] == "hub_unavailable"


async def test_hub_outage_during_the_upload_listing_answers_502(jp_fetch, fake_hub):
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "Inbox"}))
    fake_hub.raise_for.add(f"requests/{row['id']}/uploads")
    with pytest.raises(HTTPClientError) as err:
        await jp_fetch(NS, "api", "requests")
    assert err.value.code == 502
    assert json.loads(err.value.response.body)["reason"] == "hub_unavailable"


async def test_cloud_toggle_is_not_persisted_when_the_hub_is_unreachable(jp_fetch, fake_hub):
    fake_hub.unavailable = True
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "tunnel", body={"active": True})
    assert err.value.code == 502
    assert hub_routes.cloud_default() is False


async def test_delete_share_forwards_and_404_stays_404(jp_fetch, fake_hub):
    row = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    resp = await jp_fetch(NS, "api", "shares", row["id"], method="DELETE")
    assert _json(resp) == {"ok": True}
    assert ("DELETE", f"shares/{row['id']}", None) in fake_hub.calls
    with pytest.raises(HTTPClientError) as err:
        await jp_fetch(NS, "api", "shares", row["id"], method="DELETE")
    assert err.value.code == 404


async def test_request_lifecycle_with_uploads_and_fetch(jp_fetch, fake_hub, jp_root_dir):
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "Inbox", "password": ""}))
    assert ("POST", "requests", {"title": "Inbox", "password": ""}) in fake_hub.calls
    assert row["kind"] == "request" and row["state"] == "ready" and row["upload_count"] == 0
    fake_hub.uploads[row["id"]] = [
        {"upload_id": "u1", "filename": "report.csv", "size": 7, "sha256": "x", "uploaded_at": "2026-09-03T21:00:00Z"}
    ]
    listing = _json(await jp_fetch(NS, "api", "requests"))
    req = listing["requests"][0]
    assert req["upload_count"] == 1
    assert req["uploaders"][0]["entries"][0]["upload_id"] == "u1"
    (jp_root_dir / "inbox").mkdir()
    resp = await _post(jp_fetch, "api", "requests", row["id"], "uploads", "u1", "fetch",
                       body={"target_dir": "inbox", "name": "Inbox"})
    answer = _json(resp)
    assert answer["ok"] is True
    assert answer["path"] == "inbox/Inbox/report.csv"
    assert ("POST", f"requests/{row['id']}/uploads/u1/fetch", {"dest": "inbox/Inbox"}) in fake_hub.calls
    resp = await jp_fetch(NS, "api", "requests", row["id"], method="DELETE")
    assert _json(resp) == {"ok": True}


async def test_fetch_picks_a_fresh_directory_and_refuses_a_missing_folder(jp_fetch, fake_hub, jp_root_dir):
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "Inbox"}))
    (jp_root_dir / "Inbox").mkdir()
    answer = _json(await _post(jp_fetch, "api", "requests", row["id"], "uploads", "u1", "fetch",
                               body={"target_dir": "", "name": "Inbox"}))
    assert answer["path"].startswith("Inbox-2/") or answer["path"].startswith("Inbox-1/"), answer
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "requests", row["id"], "uploads", "u1", "fetch",
                    body={"target_dir": "does-not-exist"})
    assert err.value.code == 404


async def test_password_set_read_back_and_cleared(jp_fetch, fake_hub):
    row = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    assert _json(await jp_fetch(NS, "api", "shares", row["id"], "password"))["password"] == ""
    resp = await _post(jp_fetch, "api", "shares", row["id"], "password", body={"password": "s3cret"})
    assert _json(resp)["has_password"] is True
    assert ("PUT", f"shares/{row['id']}/password", {"password": "s3cret"}) in fake_hub.calls
    assert _json(await jp_fetch(NS, "api", "shares", row["id"], "password"))["password"] == "s3cret"
    await _post(jp_fetch, "api", "shares", row["id"], "password", body={"password": ""})
    assert _json(await jp_fetch(NS, "api", "shares", row["id"], "password"))["password"] == ""
    hub_routes._PASSWORDS["other"] = "x"
    hub_routes._PASSWORDS.clear()
    assert _json(await jp_fetch(NS, "api", "shares", row["id"], "password"))["password"] == ""


def _recipient_page(page):
    """The hub's recipient page on a loopback port: every GET is recorded and
    answers ``page["status"]`` (302 redirects to ``/elsewhere``), after
    ``page["release"]`` is set when the test holds the answer back."""

    class Page(tornado.web.RequestHandler):
        async def get(self, path):
            page["paths"].append(path)
            if "release" in page:
                await page["release"].wait()
            if page["status"] == 302:
                return self.redirect("/elsewhere")
            self.set_status(page["status"])
            self.finish("recipient page")

    sock, port = bind_unused_port()
    server = tornado.httpserver.HTTPServer(tornado.web.Application([(r"/(.*)", Page)]))
    server.add_socket(sock)
    return server, f"http://127.0.0.1:{port}"


async def test_link_check_opens_the_link_whatever_the_serving_verdict(jp_fetch, fake_hub, monkeypatch):
    """DEF-HUB-28: reachable is what the link itself answered."""
    page = {"status": 200, "paths": []}
    server, origin = _recipient_page(page)
    fake_hub.own_base = origin
    monkeypatch.setenv("SHARE_FILES_HUB_API", f"{origin}/hub/api/fileshare")
    fake_hub.capabilities.update({"serving": False, "reason": "sidecar_not_serving"})
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "x"}))
    res = _json(await jp_fetch(NS, "api", "link-check", params={"kind": "request", "id": row["id"]}))
    # the url the hub composed, on the hub's own origin - not the rewritten link
    assert res == {"link": f"{origin}/s/{row['id']}", "reachable": True, "status": 200}
    assert page["paths"] == [f"s/{row['id']}"]
    server.stop()


async def test_link_check_names_what_answered_when_the_link_fails(jp_fetch, fake_hub, monkeypatch):
    page = {"status": 404, "paths": []}
    server, origin = _recipient_page(page)
    fake_hub.own_base = origin
    monkeypatch.setenv("SHARE_FILES_HUB_API", f"{origin}/hub/api/fileshare")
    row = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    link = f"{origin}/s/{row['id']}"

    async def check():
        return _json(await jp_fetch(NS, "api", "link-check", params={"kind": "share", "id": row["id"]}))

    assert await check() == {"link": link, "reachable": False, "status": 404}
    # a redirect is reported as the status it is, never followed
    page["status"] = 302
    assert await check() == {"link": link, "reachable": False, "status": 302}
    assert "elsewhere" not in page["paths"]
    monkeypatch.setattr(routes, "LINK_CHECK_TIMEOUT_SECONDS", 0.2)
    page["release"] = asyncio.Event()
    assert await check() == {"link": link, "reachable": False, "error": "no answer within 0.2 s"}
    page["release"].set()
    await asyncio.sleep(0.05)
    server.stop()
    assert await check() == {"link": link, "reachable": False, "error": "connection refused"}
    with pytest.raises(HTTPClientError) as err:
        await jp_fetch(NS, "api", "link-check", params={"kind": "share", "id": "Fake_id_9999"})
    assert err.value.code == 404


async def test_link_check_says_why_nothing_answered_in_plain_words(monkeypatch):
    """DEF-PANEL-46: a short phrase per failure, never the exception text."""

    async def plain_http(reader, writer):
        await reader.read(1024)
        writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await writer.drain()
        writer.close()

    async def hang_up(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.close()

    async def probe(scheme, answer):
        server = await asyncio.start_server(answer, "127.0.0.1", 0)
        try:
            return (await routes.probe_link(f"{scheme}://127.0.0.1:{server.sockets[0].getsockname()[1]}/s/x"))["error"]
        finally:
            server.close()

    assert await probe("https", plain_http) == "a TLS error"
    assert await probe("http", hang_up) == "a closed connection"

    def no_address(*args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", no_address)
    assert (await routes.probe_link("https://share.example.invalid/s/x"))["error"] == "no address for the host name"

    def unreachable(*args, **kwargs):
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    monkeypatch.setattr(socket, "getaddrinfo", unreachable)
    assert (await routes.probe_link("https://share.example.invalid/s/x"))["error"] == "a network error"


async def test_cloud_toggle_flips_every_record_and_sets_the_default(jp_fetch, fake_hub):
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    req = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    # born off: the hub's own address, restored to the browser origin
    assert share["cloud"] is False and re.fullmatch(r"http://[^/]+/s/" + share["id"], share["link"])
    state = _json(await jp_fetch(NS, "api", "tunnel"))
    assert state == {"tunnel_configured": True, "tunnel_active": False, "tunnel_autostart": False,
                     "tunnel_running": True, "tunnel_waiting": False, "tunnel_reason": ""}
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))
    assert state["tunnel_active"] is True
    assert [c for c in fake_hub.calls if c[0] == "PUT" and c[1].endswith("/cloud")] == [
        ("PUT", f"shares/{share['id']}/cloud", {"cloud": True}),
        ("PUT", f"requests/{req['id']}/cloud", {"cloud": True}),
    ]
    listing = _json(await jp_fetch(NS, "api", "shares"))
    assert listing["shares"][0]["cloud"] is True
    assert listing["shares"][0]["link"] == f"https://share.example.com/s/{share['id']}"
    # a record minted while the toggle is on is switched on after the create
    # and answers with the url the hub composed for it
    new = _json(await _post(jp_fetch, "api", "requests", body={"name": "z"}))
    assert new["cloud"] is True and new["link"] == f"https://share.example.com/s/{new['id']}"
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": False}))
    assert state["tunnel_active"] is False
    listing = _json(await jp_fetch(NS, "api", "requests"))
    assert all(r["cloud"] is False for r in listing["requests"])
    assert all(re.fullmatch(r"http://[^/]+/s/" + r["id"], r["link"]) for r in listing["requests"])


async def test_a_record_gone_mid_switch_does_not_abort_the_bulk_switch(jp_fetch, fake_hub):
    """DEF-HUB-54: one record deleted in another panel answers 404; the bulk
    switch counts it as done, like CloudWait._switch_back does, and the
    default lands on the side the owner chose."""
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    req = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))
    assert state["tunnel_active"] is True
    # the share is deleted elsewhere before the switch off reaches it
    fake_hub.overrides[("PUT", f"shares/{share['id']}/cloud")] = (404, {"status": 404, "message": "No such share"})
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": False}))
    assert state["tunnel_active"] is False
    assert hub_routes.cloud_default() is False
    assert [c for c in fake_hub.calls if c[0] == "PUT" and c[1].endswith("/cloud")][-1] == (
        "PUT", f"requests/{req['id']}/cloud", {"cloud": False})
    listing = _json(await jp_fetch(NS, "api", "requests"))
    assert listing["requests"][0]["cloud"] is False


async def test_cloud_toggle_on_is_refused_while_the_policy_has_cloudflare_off(jp_fetch, fake_hub):
    fake_hub.cloudflare_enabled = False
    await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]})
    with pytest.raises(HTTPClientError) as exc:
        await _post(jp_fetch, "api", "tunnel", body={"active": True})
    assert exc.value.code == 403 and _json(exc.value.response)["reason"] == "cloud_not_configured"
    assert hub_routes.cloud_default() is False
    # with no record to refuse on, the preference stands until the first
    # create is refused - then it is dropped and the row says why
    fake_hub.items.clear()
    assert _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))["tunnel_active"] is True
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    assert row["cloud"] is False and row["cloud_reason"] == "cloud_not_configured"
    assert hub_routes.cloud_default() is False


async def test_one_record_is_switched_on_its_own(jp_fetch, fake_hub):
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    res = _json(await _post(jp_fetch, "api", "shares", share["id"], "cloud", body={"cloud": True}))
    assert res == {"id": share["id"], "cloud": True}
    row = _json(await jp_fetch(NS, "api", "shares", share["id"]))
    assert row["cloud"] is True and row["link"].startswith("https://share.example.com/")
    with pytest.raises(HTTPClientError) as exc:
        await _post(jp_fetch, "api", "shares", share["id"], "cloud", body={"cloud": "yes"})
    assert exc.value.code == 400
    with pytest.raises(HTTPClientError) as exc:
        await _post(jp_fetch, "api", "requests", "r_Fake_id_9999", "cloud", body={"cloud": False})
    assert exc.value.code == 404


async def test_password_required_is_reported_and_relayed(jp_fetch, fake_hub):
    fake_hub.capabilities["password_required"] = True
    info = _json(await jp_fetch(NS, "api", "info"))
    assert info["hub"]["password_required"] is True
    with pytest.raises(HTTPClientError) as exc:
        await _post(jp_fetch, "api", "requests", body={"name": "x"})
    assert exc.value.code == 400 and _json(exc.value.response)["reason"] == "password_required"
    assert fake_hub.items == []
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "x", "password": "pw"}))
    assert row["has_password"] is True


# --------------------------------------------------------------------------- #
# The Cloudflare confirmation wait
# --------------------------------------------------------------------------- #


@pytest.fixture
def rings(monkeypatch):
    """Short bounds for the wait; the rings it sends to the open panels."""
    monkeypatch.setattr(hub_routes, "CONFIRM_POLL_SECONDS", 0.05)
    monkeypatch.setattr(hub_routes, "CONFIRM_TIMEOUT_SECONDS", 0.5)
    sent: list[str] = []
    monkeypatch.setattr(hub_routes, "RELAY", types.SimpleNamespace(ring=sent.append))
    return sent


def _items_calls(hub) -> int:
    return sum(1 for c in hub.calls if c[:2] == ("GET", "items"))


async def _until(predicate, seconds=3.0):
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("not reached in time")


async def test_switch_on_waits_for_the_tunnel_link_and_rings_the_panels_once(jp_fetch, fake_hub, rings):
    fake_hub.tunnel_registered = False
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))
    assert state["tunnel_active"] is True and state["tunnel_waiting"] is True
    await asyncio.sleep(0.2)
    assert _json(await jp_fetch(NS, "api", "info"))["tunnel_waiting"] is True
    assert rings == []
    # the tunnel registers; the hub rings nothing for it
    fake_hub.tunnel_registered = True
    await _until(lambda: not hub_routes.CLOUD_WAIT.waiting)
    assert rings == ["changed"]
    state = _json(await jp_fetch(NS, "api", "tunnel"))
    assert (state["tunnel_active"], state["tunnel_waiting"], state["tunnel_reason"]) == (True, False, "")
    listing = _json(await jp_fetch(NS, "api", "shares"))
    assert listing["shares"][0]["link"] == f"https://share.example.com/s/{share['id']}"
    calls = _items_calls(fake_hub)
    await asyncio.sleep(0.3)
    assert _items_calls(fake_hub) == calls and rings == ["changed"]


async def test_confirmation_checks_are_bounded_and_none_while_nothing_waits(jp_fetch, fake_hub, rings):
    fake_hub.tunnel_registered = False
    await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]})
    idle = _items_calls(fake_hub)
    await asyncio.sleep(0.3)
    assert _items_calls(fake_hub) == idle
    await _post(jp_fetch, "api", "tunnel", body={"active": True})
    start = _items_calls(fake_hub)
    await _until(lambda: not hub_routes.CLOUD_WAIT.waiting)
    # 0.5 s at no more than one check per 0.05 s
    assert 1 <= _items_calls(fake_hub) - start <= 11
    end = _items_calls(fake_hub)
    await asyncio.sleep(0.3)
    assert _items_calls(fake_hub) == end


async def test_two_switch_ons_share_one_wait(jp_fetch, fake_hub, rings, monkeypatch):
    monkeypatch.setattr(hub_routes, "CONFIRM_TIMEOUT_SECONDS", 5)
    fake_hub.tunnel_registered = False
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    req = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    await _post(jp_fetch, "api", "shares", share["id"], "cloud", body={"cloud": True})
    await _post(jp_fetch, "api", "requests", req["id"], "cloud", body={"cloud": True})
    start = _items_calls(fake_hub)
    await asyncio.sleep(0.5)
    # one wait's cadence - two waits would check twice as often
    assert _items_calls(fake_hub) - start <= 11
    fake_hub.tunnel_registered = True
    await _until(lambda: not hub_routes.CLOUD_WAIT.waiting)
    assert rings == ["changed"]


async def test_unconfirmed_switch_on_goes_back_off_with_a_reason(jp_fetch, fake_hub, rings):
    fake_hub.tunnel_registered = False
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    req = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    await _post(jp_fetch, "api", "tunnel", body={"active": True})
    await _until(lambda: not hub_routes.CLOUD_WAIT.waiting)
    offs = [c for c in fake_hub.calls if c[0] == "PUT" and c[2] == {"cloud": False}]
    assert sorted(offs) == [
        ("PUT", f"requests/{req['id']}/cloud", {"cloud": False}),
        ("PUT", f"shares/{share['id']}/cloud", {"cloud": False}),
    ]
    assert all(i["cloud"] is False for i in fake_hub.items)
    assert hub_routes.cloud_default() is False
    state = _json(await jp_fetch(NS, "api", "tunnel"))
    assert (state["tunnel_active"], state["tunnel_waiting"], state["tunnel_reason"]) == (
        False, False, "cloud_not_confirmed")
    assert _json(await jp_fetch(NS, "api", "info"))["tunnel_reason"] == "cloud_not_confirmed"
    assert rings == ["changed"]
    # the next switch-on clears the reason; switching off ends its wait at once
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))
    assert (state["tunnel_waiting"], state["tunnel_reason"]) == (True, "")
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": False}))
    assert (state["tunnel_waiting"], state["tunnel_reason"]) == (False, "")


async def test_a_check_the_hub_never_answers_fails_and_the_wait_still_goes_back_off(jp_fetch, fake_hub, rings, monkeypatch):
    fake_hub.tunnel_registered = False
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    await _post(jp_fetch, "api", "tunnel", body={"active": True})
    assert hub_routes.CLOUD_WAIT.waiting is True
    sock = _stall(fake_hub, monkeypatch, "items")
    try:
        await _until(lambda: not hub_routes.CLOUD_WAIT.waiting)
    finally:
        sock.close()
    assert ("PUT", f"shares/{share['id']}/cloud", {"cloud": False}) in fake_hub.calls
    assert fake_hub.items[0]["cloud"] is False
    assert hub_routes.cloud_default() is False
    assert hub_routes.CLOUD_WAIT.reason == "cloud_not_confirmed"
    assert rings == ["changed"]


async def test_a_switch_back_the_hub_refuses_keeps_the_default_and_says_so(jp_fetch, fake_hub, rings):
    fake_hub.tunnel_registered = False
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    await _post(jp_fetch, "api", "tunnel", body={"active": True})
    assert hub_routes.CLOUD_WAIT.waiting is True
    fake_hub.raise_for.add(f"shares/{share['id']}/cloud")
    await _until(lambda: not hub_routes.CLOUD_WAIT.waiting)
    assert ("PUT", f"shares/{share['id']}/cloud", {"cloud": False}) in fake_hub.calls
    # the record is still on at the hub, so the default and the header stay on
    assert fake_hub.items[0]["cloud"] is True
    assert hub_routes.cloud_default() is True
    assert hub_routes.CLOUD_WAIT.reason == "hub_unavailable"
    assert rings == ["changed"]


async def test_a_switch_back_the_hub_answers_with_an_error_says_it_stayed_on(jp_fetch, fake_hub, rings):
    fake_hub.tunnel_registered = False
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    await _post(jp_fetch, "api", "tunnel", body={"active": True})
    fake_hub.overrides[("PUT", f"shares/{share['id']}/cloud")] = (500, {"status": 500, "message": "boom"})
    await _until(lambda: not hub_routes.CLOUD_WAIT.waiting)
    # the hub answered, so the reason is not an unreachable hub
    assert fake_hub.items[0]["cloud"] is True
    assert hub_routes.cloud_default() is True
    state = _json(await jp_fetch(NS, "api", "tunnel"))
    assert (state["tunnel_active"], state["tunnel_waiting"], state["tunnel_reason"]) == (
        True, False, "cloud_not_switched_off")
    # the next header switch on waits for the record still on, and does not
    # answer on before the hub confirms it
    del fake_hub.overrides[("PUT", f"shares/{share['id']}/cloud")]
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))
    assert (state["tunnel_waiting"], state["tunnel_reason"]) == (True, "")
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": False}))
    assert state["tunnel_waiting"] is False


async def test_a_default_switch_on_after_create_names_why_it_failed(jp_fetch, fake_hub):
    await _post(jp_fetch, "api", "tunnel", body={"active": True})
    # the hub answers with an error: its own lab slug, not an unreachable hub
    fake_hub.overrides[("PUT", "requests/r_Fake_id_0001/cloud")] = (500, {"status": 500, "message": "boom"})
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    assert row["cloud_reason"] == "cloud_not_switched_on"
    # the hub does not answer
    fake_hub.raise_for.add("requests/r_Fake_id_0002/cloud")
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "z"}))
    assert row["cloud_reason"] == "hub_unavailable"


async def test_a_header_switch_on_that_fails_partway_waits_for_the_records_it_switched(jp_fetch, fake_hub, rings):
    fake_hub.tunnel_registered = False
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    req = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    fake_hub.overrides[("PUT", f"requests/{req['id']}/cloud")] = (500, {"status": 500, "message": "boom"})
    with pytest.raises(HTTPClientError) as exc:
        await _post(jp_fetch, "api", "tunnel", body={"active": True})
    assert exc.value.code == 500
    # the share went on: the header waits for it instead of reading off
    assert fake_hub.items[0]["cloud"] is True
    state = _json(await jp_fetch(NS, "api", "tunnel"))
    assert (state["tunnel_active"], state["tunnel_waiting"]) == (True, True)
    # unconfirmed, it goes back off with the default
    await _until(lambda: not hub_routes.CLOUD_WAIT.waiting)
    assert ("PUT", f"shares/{share['id']}/cloud", {"cloud": False}) in fake_hub.calls
    assert fake_hub.items[0]["cloud"] is False
    assert hub_routes.cloud_default() is False
    # the hub stops answering partway: the same wait
    fake_hub.tunnel_registered = True
    fake_hub.overrides.clear()
    fake_hub.raise_for.add(f"requests/{req['id']}/cloud")
    with pytest.raises(HTTPClientError) as exc:
        await _post(jp_fetch, "api", "tunnel", body={"active": True})
    assert exc.value.code == 502
    state = _json(await jp_fetch(NS, "api", "tunnel"))
    # the tunnel stands, so the share is confirmed at once and the header reads on
    assert (state["tunnel_active"], state["tunnel_waiting"]) == (True, False)


async def test_no_wait_without_a_record_or_for_a_link_already_on_the_tunnel(jp_fetch, fake_hub, rings):
    # no record: the toggle stores the default and shows on at once
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))
    assert (state["tunnel_active"], state["tunnel_waiting"]) == (True, False)
    # the tunnel stands: a record born on already carries its hostname
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    assert row["cloud"] is True and row["link"].startswith("https://share.example.com/")
    assert hub_routes.CLOUD_WAIT.waiting is False
    calls = _items_calls(fake_hub)
    await asyncio.sleep(0.2)
    assert _items_calls(fake_hub) == calls and rings == []
    # the tunnel is down: the next record switched on starts the wait
    fake_hub.tunnel_registered = False
    await _post(jp_fetch, "api", "requests", body={"name": "z"})
    assert _json(await jp_fetch(NS, "api", "tunnel"))["tunnel_waiting"] is True
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": False}))
    assert (state["tunnel_waiting"], state["tunnel_reason"]) == (False, "")


# --------------------------------------------------------------------------- #
# The change stream
# --------------------------------------------------------------------------- #


async def _read_stream(url, seconds):
    """Everything the lab wrote on its stream within ``seconds``; the stream
    is held open, so the read ends on the client's own timeout."""
    chunks = []
    with pytest.raises(HTTPTimeoutError):
        await AsyncHTTPClient().fetch(
            url, streaming_callback=chunks.append, request_timeout=seconds, raise_error=False)
    return b"".join(chunks).decode()


@pytest.fixture
def fake_hub_stream(monkeypatch):
    """`hub_stream.hold` replaced by a scripted hub stream: it answers
    ``status``, rings ``rings`` times and then stays open until cancelled."""
    script = {"status": 200, "rings": 0, "opens": 0}

    async def hold(on_open, on_event):
        script["opens"] += 1
        if script["status"] != 200:
            return script["status"]
        on_open()
        for _ in range(script["rings"]):
            await asyncio.sleep(0.05)
            on_event("changed")
        await asyncio.sleep(3600)
        return 599

    monkeypatch.setattr(hub_stream, "hold", hold)
    monkeypatch.setattr(hub_stream, "RELAY", hub_stream.Relay())
    monkeypatch.setattr(hub_routes, "RELAY", hub_stream.RELAY)
    return script


def _stream_url(jp_http_port, jp_base_url, jp_auth_header=None):
    """The stream needs a client that returns before the response ends, so
    it is read with a raw AsyncHTTPClient; ``jp_fetch`` is still requested
    by each test because its fixture chain is what serves the socket."""
    url = f"http://127.0.0.1:{jp_http_port}{jp_base_url}{NS}/api/stream"
    if jp_auth_header:
        url += f"?token={jp_auth_header['Authorization'].split()[-1]}"
    return url


async def test_stream_relays_the_hub_rings(jp_fetch, jp_http_port, jp_base_url, jp_auth_header, fake_hub, fake_hub_stream):
    fake_hub_stream["rings"] = 2
    url = _stream_url(jp_http_port, jp_base_url, jp_auth_header)
    text = await _read_stream(url, 1)
    assert text.startswith("retry: 5000\n\n")
    # one ring for the hub stream opening, then the two the hub sent
    assert text.count("event: changed\ndata:\n\n") == 3
    await asyncio.sleep(0.1)  # the server notices the closed connection
    assert hub_stream.RELAY.connected is False  # the last reader took the hub stream down


async def test_stream_tells_the_panel_to_poll_on_an_older_hub(jp_fetch, jp_http_port, jp_base_url, jp_auth_header, fake_hub, fake_hub_stream):
    fake_hub_stream["status"] = 404
    url = _stream_url(jp_http_port, jp_base_url, jp_auth_header)
    text = await _read_stream(url, 1)
    assert "event: poll\ndata:\n\n" in text
    assert "event: changed" not in text
    assert fake_hub_stream["opens"] == 1


async def test_stream_answers_403_without_the_lab_credentials(jp_fetch, jp_http_port, jp_base_url, fake_hub, fake_hub_stream):
    resp = await AsyncHTTPClient().fetch(_stream_url(jp_http_port, jp_base_url), raise_error=False, request_timeout=5)
    assert resp.code == 403
    assert fake_hub_stream["opens"] == 0


async def test_hub_address_links_are_rewritten_to_the_browser_origin(jp_fetch, fake_hub):
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "x"}))
    assert not row["link"].startswith("http://hub:8080"), row["link"]
    assert row["link"].endswith(f"/s/{row['id']}")
