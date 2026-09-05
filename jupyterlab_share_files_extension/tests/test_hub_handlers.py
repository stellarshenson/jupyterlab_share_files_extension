"""Hub-mode handlers driven through a live jupyter_server against a fake hub.

The extension is loaded with the spawn contract in the environment, so the
hub table is what the server mounts; ``HubClient`` is replaced by an
in-memory hub that records every call. Nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from tornado.httpclient import AsyncHTTPClient, HTTPClientError
from tornado.simple_httpclient import HTTPTimeoutError

from jupyterlab_share_files_extension import hub_routes, hub_stream
from jupyterlab_share_files_extension.hub import HubUnavailable

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
        """The hub's own address while the record's cloud switch is off, the
        origin the policy prefers while it is on (`record_base_url`)."""
        base = self.capabilities["public_base_url"] if item.get("cloud") else "http://hub:8080"
        return {**item, "url": f"{base}/s/{item['id']}"}

    async def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.unavailable or path in self.raise_for:
            raise HubUnavailable("could not reach the hub: refused")
        if (method, path) in self.overrides:
            return self.overrides[(method, path)]
        base = self.capabilities["public_base_url"]
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
                "id": id_, "url": f"http://hub:8080/s/{id_}", "state": state}
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


async def test_link_check_reports_the_hubs_serving_verdict(jp_fetch, fake_hub):
    fake_hub.capabilities.update({"serving": False, "reason": "sidecar_not_serving"})
    res = _json(await jp_fetch(NS, "api", "link-check", params={"kind": "share", "id": "Fake_id_0001"}))
    assert res["reachable"] is False and res["error"] == "sidecar_not_serving" and res["status"] == 503
    fake_hub.capabilities.update({"serving": True})
    res = _json(await jp_fetch(NS, "api", "link-check", params={"kind": "share", "id": "Fake_id_0001"}))
    assert res["reachable"] is True
    assert not any(c[1].startswith("s/") for c in fake_hub.calls)


async def test_cloud_toggle_flips_every_record_and_sets_the_default(jp_fetch, fake_hub):
    fake_hub.capabilities["public_base_url"] = "https://share.example.com"
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    req = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    # born off: the hub's own address, restored to the browser origin
    assert share["cloud"] is False and re.fullmatch(r"http://[^/]+/s/" + share["id"], share["link"])
    state = _json(await jp_fetch(NS, "api", "tunnel"))
    assert state == {"tunnel_configured": True, "tunnel_active": False, "tunnel_autostart": False, "tunnel_running": True}
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
    fake_hub.capabilities["public_base_url"] = "https://share.example.com"
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
