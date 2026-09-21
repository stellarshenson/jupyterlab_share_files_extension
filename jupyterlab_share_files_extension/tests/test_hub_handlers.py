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

import pytest
import tornado.httpserver
import tornado.web
from tornado.httpclient import AsyncHTTPClient, HTTPClientError
from tornado.simple_httpclient import HTTPTimeoutError
from tornado.testing import bind_unused_port

from jupyterlab_share_files_extension import hub_routes, hub_stream, routes, tunnel
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
        # the group policy has a tunnel at all - capabilities.tunnel_available
        self.cloudflare_enabled = True
        # the hub's own address, and its Cloudflare tunnel: a switched-on
        # record's url carries the tunnel hostname only while the tunnel is
        # registered, which is capabilities.tunnel_ready
        self.own_base = "http://hub:8080"
        self.tunnel_base = "https://share.example.com"
        self.tunnel_registered = True
        self.items: list[dict] = []
        self.uploads: dict[str, list[dict]] = {}
        self.overrides: dict[tuple[str, str], tuple[int, dict]] = {}
        self.unavailable = False
        self.raise_for: set[str] = set()
        self.counter = 0
        # one add in flight: (share id, names); ``land_add`` settles it
        self.pending_add: tuple[str, list[str]] | None = None

    def land_add(self, reason: str = "") -> None:
        """Settle the add in flight the way the hub's row reports it."""
        id_, names = self.pending_add
        self.pending_add = None
        item = next(i for i in self.items if i["id"] == id_)
        item.pop("progress", None)
        if reason:
            item["last_add"] = {"state": "refused", "reason": reason, "skipped": 0, "at": "2026-09-21T12:00:00Z"}
            return
        item["files"] += [{"name": n, "size": 3, "sha256": "0" * 64} for n in names]
        item["last_add"] = {"state": "done", "skipped": 0, "at": "2026-09-21T12:00:00Z"}

    def _content(self, id_, body):
        item = next((i for i in self.items if i["id"] == id_), None)
        if item is None:
            return 404, {"status": 404, "message": "No such share"}
        action = body["action"]
        refuse = lambda reason, code=400: (code, {"reason": reason, "message": f"The {action} was refused: {reason}"})  # noqa: E731
        if self.pending_add:
            return refuse("busy", 409)
        names = [f["name"] for f in item["files"]]
        held = lambda n: [x for x in names if x == n or x.startswith(n + "/")]  # noqa: E731
        if action == "add":
            added = [p.rstrip("/").split("/")[-1] for p in body["paths"]]
            if any(held(n) for n in added):
                return refuse("name_taken")
            self.pending_add = (id_, added)
            item["last_add"] = {"state": "running"}
            item["progress"] = {"copied": 1, "total": 3}
            return 202, {}
        if not held(body["name"]):
            return refuse("unknown_entry")
        if action == "remove":
            item["files"] = [f for f in item["files"] if f["name"] not in held(body["name"])]
        elif held(body["new_name"]):
            return refuse("name_taken")
        else:
            for f in item["files"]:
                if f["name"] in held(body["name"]):
                    f["name"] = body["new_name"] + f["name"][len(body["name"]):]
        return 204, {}

    def _new_id(self, prefix=""):
        self.counter += 1
        return f"{prefix}Fake_id_{self.counter:04d}"

    def _with_url(self, item):
        """The tunnel hostname while the record's tunnel switch is on and the
        tunnel is registered, the hub's own address otherwise
        (`record_base_url`, galaxahub v4.4.58)."""
        switched_on = item.get("tunnel") and self.tunnel_registered
        base = self.tunnel_base if switched_on else self.own_base
        return {**item, "url": f"{base}/s/{item['id']}"}

    async def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.unavailable or path in self.raise_for:
            raise HubUnavailable("could not reach the hub: refused")
        if (method, path) in self.overrides:
            return self.overrides[(method, path)]
        if method == "GET" and path == "capabilities":
            return 200, {**self.capabilities,
                         "tunnel_available": self.cloudflare_enabled,
                         "tunnel_ready": self.tunnel_registered}
        if method == "GET" and path == "items":
            return 200, {"items": [self._with_url(i) for i in self.items]}
        if method == "POST" and path in ("shares", "requests"):
            kind = "share" if path == "shares" else "request"
            id_ = self._new_id("r_" if kind == "request" else "")
            state = "staging" if kind == "share" and body["paths"] else "ready"
            if self.capabilities.get("password_required") and not (body.get("password") or "").strip():
                return 400, {"reason": "password_required", "message": "Your group requires a password"}
            self.items.append({
                "id": id_, "kind": kind, "owner": "alice", "title": body["title"],
                "state": state, "files": [], "bytes": 0, "skipped": 0,
                "created_at": "2026-09-03T20:00:00Z", "expires_at": "2026-09-17T20:00:00Z",
                "has_password": bool(body.get("password")), "tunnel": False,
            })
            return (202 if state == "staging" else 201), {
                "id": id_, "url": f"{self.own_base}/s/{id_}", "state": state}
        m = re.fullmatch(r"shares/([^/]+)/content", path)
        if m and method == "POST":
            return self._content(m.group(1), body)
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
        m = re.fullmatch(r"(shares|requests)/([^/]+)/tunnel", path)
        if m and method == "PUT":
            for item in self.items:
                if item["id"] == m.group(2):
                    if body["tunnel"] and not self.cloudflare_enabled:
                        return 403, {"reason": "tunnel_not_available", "message": "Cloudflare is off"}
                    item["tunnel"] = bool(body["tunnel"])
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
    # the hub's own verdicts, and the lab's stored default
    assert info["tunnel_available"] is True
    assert info["tunnel_ready"] is True
    assert info["tunnel_default"] is False
    assert "tunnel_waiting" not in info and "tunnel_reason" not in info
    assert info["public_base_url"] == ""
    assert info["hub"] == {
        "available": True, "allow_share": False, "allow_request": True,
        "reason": "share_not_granted", "serving": False, "password_required": False,
        "max_share_bytes": 5, "max_upload_bytes": 10, "max_shares": 20, "retention_days": 14,
    }
    # the tunnel state is re-read from the hub, so a connector that drops is
    # reported on the next read without a click (ACC-HUBM-161)
    fake_hub.tunnel_registered = False
    assert _json(await jp_fetch(NS, "api", "info"))["tunnel_ready"] is False


async def test_info_reports_an_unavailable_hub_without_failing(jp_fetch, fake_hub):
    fake_hub.unavailable = True
    info = _json(await jp_fetch(NS, "api", "info"))
    assert info["mode"] == "hub"
    assert info["hub"]["available"] is False
    assert info["hub"]["reason"] == "hub_unavailable"


async def test_create_share_sends_paths_and_returns_a_staging_row(jp_fetch, fake_hub):
    resp = await _post(jp_fetch, "api", "shares", body={"name": "Report", "paths": ["notes/report.csv"], "password": "pw"})
    row = _json(resp)
    sent = next(c[2] for c in fake_hub.calls if c[:2] == ("POST", "shares"))
    assert {k: sent[k] for k in ("title", "paths", "password")} == {
        "title": "Report", "paths": ["notes/report.csv"], "password": "pw"}
    assert "__pycache__" in sent["exclude"]
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


async def test_an_empty_share_is_created_ready(jp_fetch, fake_hub):
    """ACC-HUBM-163: the hub creates a share with no files, born ready; the
    owner fills it afterwards."""
    row = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": []}))
    assert row["state"] == "ready" and row["entries"] == []
    assert any(c[:2] == ("POST", "shares") and c[2]["paths"] == [] for c in fake_hub.calls)


async def _share_row(jp_fetch, id_):
    return _json(await jp_fetch(NS, "api", "shares", id_))


async def test_an_add_is_sent_filtered_and_the_row_relays_the_hubs_last_add(jp_fetch, fake_hub):
    """ACC-DRAG-156: files go into an existing hub share. The hub's row
    carries ``last_add`` and ``progress`` while it copies; the lab relays them
    and sends the catalogue's plain names for the hub to apply below a folder."""
    id_ = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": [], "password": "pw"}))["id"]
    await _post(jp_fetch, "api", "shares", id_, "items", body={"paths": ["data/q3.csv", "src/__pycache__"]})
    method, path, body = fake_hub.calls[-1]
    assert (method, path) == ("POST", f"shares/{id_}/content")
    assert body["paths"] == ["data/q3.csv"] and body["password"] == "pw"
    assert "__pycache__" in body["exclude"] and not any("*" in n for n in body["exclude"])
    row = await _share_row(jp_fetch, id_)
    assert row["adding"] is True and row["progress"] == {"copied": 1, "total": 3}
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "shares", id_, "items", body={"paths": ["b.txt"]})
    assert err.value.code == 409 and json.loads(err.value.response.body)["reason"] == "busy"
    fake_hub.land_add()
    row = await _share_row(jp_fetch, id_)
    assert [e["name"] for e in row["entries"]] == ["q3.csv"]
    assert row["adding"] is False and row["add_reason"] == "" and row["progress"] is None


async def test_an_add_the_hub_refuses_carries_its_reason_on_the_row(jp_fetch, fake_hub):
    """The hub records a refused add as ``last_add`` with the reason; the row
    carries it until the next add."""
    id_ = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": []}))["id"]
    await _post(jp_fetch, "api", "shares", id_, "items", body={"paths": ["big.bin"]})
    fake_hub.land_add("over_cap")
    row = await _share_row(jp_fetch, id_)
    assert row["entries"] == [] and row["add_reason"] == "over_cap" and row["adding"] is False
    await _post(jp_fetch, "api", "shares", id_, "items", body={"paths": ["small.txt"]})
    fake_hub.land_add()
    assert (await _share_row(jp_fetch, id_))["add_reason"] == ""


async def test_an_add_to_a_password_share_the_lab_forgot_says_how_to_recover(jp_fetch, fake_hub):
    """The password lives in this process only; after a restart the hub
    refuses the add with `password_required` (probed live 2026-09-21)."""
    id_ = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": []}))["id"]
    fake_hub.overrides[("POST", f"shares/{id_}/content")] = (
        400, {"reason": "password_required", "message": "The add was refused: password_required"})
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "shares", id_, "items", body={"paths": ["a.txt"]})
    body = json.loads(err.value.response.body)
    assert err.value.code == 400 and "enter the same password again" in body["error"] and "reason" not in body


async def test_a_taken_or_repeated_name_is_refused_by_name_before_the_hub_is_asked(jp_fetch, fake_hub):
    """The hub refuses a taken name without saying which, and drops two paths
    of one name after its 202."""
    id_ = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": []}))["id"]
    fake_hub.items[0]["files"] = [{"name": "data/q3.csv", "size": 1, "sha256": ""}]
    for paths, words in ((["new/data"], "already holds data"), (["a/x.txt", "b/x.txt"], "named x.txt")):
        with pytest.raises(HTTPClientError) as err:
            await _post(jp_fetch, "api", "shares", id_, "items", body={"paths": paths})
        assert err.value.code == 400 and words in json.loads(err.value.response.body)["error"]
    assert not any(c[1].endswith("/content") for c in fake_hub.calls)


async def test_remove_and_rename_go_through_the_content_route(jp_fetch, fake_hub):
    """ACC-EDIT-165, 166, 168: a name may hold a slash, and a rename under
    another folder is the move."""
    id_ = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": []}))["id"]
    fake_hub.items[0]["files"] = [{"name": n, "size": 1, "sha256": ""} for n in ("a.txt", "sub/c.txt")]
    await jp_fetch(NS, "api", "shares", id_, "items", method="PUT",
                   body=json.dumps({"name": "a.txt", "new_name": "sub/a.txt"}))
    assert fake_hub.calls[-1][2] == {"action": "rename", "name": "a.txt", "new_name": "sub/a.txt"}
    with pytest.raises(HTTPClientError) as err:
        await jp_fetch(NS, "api", "shares", id_, "items", method="PUT",
                       body=json.dumps({"name": "sub/a.txt", "new_name": "sub/c.txt"}))
    assert err.value.code == 400 and json.loads(err.value.response.body)["reason"] == "name_taken"
    await jp_fetch(NS, "api", "shares", id_, "items", method="DELETE", params={"name": "sub"})
    assert (await _share_row(jp_fetch, id_))["entries"] == []


async def test_an_excluded_name_is_not_sent_to_the_hub(jp_fetch, fake_hub):
    """ACC-EXCL-158: the hub copies what it is sent, so the catalogue is
    applied to the dropped items before the call."""
    await _post(
        jp_fetch,
        "api",
        "shares",
        body={"name": "x", "paths": ["notes.txt", ".ipynb_checkpoints", "src/__pycache__"]},
    )
    sent = next(c for c in fake_hub.calls if c[0] == "POST" and c[1] == "shares")
    assert sent[2]["paths"] == ["notes.txt"]


async def test_a_share_of_only_excluded_names_is_refused(jp_fetch, fake_hub):
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": [".DS_Store"]})
    assert err.value.code == 400
    assert "excluded list" in json.loads(err.value.response.body)["error"]
    assert not any(c[0] == "POST" for c in fake_hub.calls)


async def test_an_add_of_only_excluded_names_is_refused_with_the_same_sentence(jp_fetch, fake_hub):
    """ACC-EDIT-167: the catalogue applies to a drop on a share as it does at create."""
    id_ = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": []}))["id"]
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "shares", id_, "items", body={"paths": ["a/.ipynb_checkpoints", ".DS_Store"]})
    assert err.value.code == 400
    assert "excluded list" in json.loads(err.value.response.body)["error"]
    assert not any(c[1].endswith("/content") for c in fake_hub.calls)


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


async def test_the_toggle_is_not_persisted_when_the_hub_is_unreachable(jp_fetch, fake_hub):
    fake_hub.unavailable = True
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "tunnel", body={"active": True})
    assert err.value.code == 502
    assert hub_routes.tunnel_default() is False


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


async def test_the_toggle_flips_every_record_and_sets_the_default(jp_fetch, fake_hub):
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    req = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    # born off: the hub's own address, restored to the browser origin
    assert share["tunnel"] is False and re.fullmatch(r"http://[^/]+/s/" + share["id"], share["link"])
    state = _json(await jp_fetch(NS, "api", "tunnel"))
    assert state == {"tunnel_configured": True, "tunnel_active": False, "tunnel_autostart": False,
                     "tunnel_running": True, "tunnel_available": True, "tunnel_ready": True,
                     "tunnel_default": False}
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))
    assert state["tunnel_active"] is True and state["tunnel_default"] is True
    assert [c for c in fake_hub.calls if c[0] == "PUT" and c[1].endswith("/tunnel")] == [
        ("PUT", f"shares/{share['id']}/tunnel", {"tunnel": True}),
        ("PUT", f"requests/{req['id']}/tunnel", {"tunnel": True}),
    ]
    listing = _json(await jp_fetch(NS, "api", "shares"))
    assert listing["shares"][0]["tunnel"] is True
    assert listing["shares"][0]["link"] == f"https://share.example.com/s/{share['id']}"
    # a record minted while the toggle is on is switched on after the create
    # and answers with the url the hub composed for it
    new = _json(await _post(jp_fetch, "api", "requests", body={"name": "z"}))
    assert new["tunnel"] is True and new["link"] == f"https://share.example.com/s/{new['id']}"
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": False}))
    assert state["tunnel_active"] is False
    listing = _json(await jp_fetch(NS, "api", "requests"))
    assert all(r["tunnel"] is False for r in listing["requests"])
    assert all(re.fullmatch(r"http://[^/]+/s/" + r["id"], r["link"]) for r in listing["requests"])


async def test_a_record_gone_mid_switch_does_not_abort_the_bulk_switch(jp_fetch, fake_hub):
    """DEF-HUB-54: one record deleted in another panel answers 404; the bulk
    switch counts it as done, and the default lands on the side the owner
    chose."""
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    req = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))
    assert state["tunnel_active"] is True
    # the share is deleted elsewhere before the switch off reaches it
    fake_hub.overrides[("PUT", f"shares/{share['id']}/tunnel")] = (404, {"status": 404, "message": "No such share"})
    state = _json(await _post(jp_fetch, "api", "tunnel", body={"active": False}))
    assert state["tunnel_active"] is False
    assert hub_routes.tunnel_default() is False
    assert [c for c in fake_hub.calls if c[0] == "PUT" and c[1].endswith("/tunnel")][-1] == (
        "PUT", f"requests/{req['id']}/tunnel", {"tunnel": False})
    listing = _json(await jp_fetch(NS, "api", "requests"))
    assert listing["requests"][0]["tunnel"] is False


async def test_the_toggle_on_is_refused_while_the_policy_has_cloudflare_off(jp_fetch, fake_hub):
    fake_hub.cloudflare_enabled = False
    await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]})
    with pytest.raises(HTTPClientError) as exc:
        await _post(jp_fetch, "api", "tunnel", body={"active": True})
    assert exc.value.code == 403 and _json(exc.value.response)["reason"] == "tunnel_not_available"
    assert hub_routes.tunnel_default() is False
    # with no record to refuse on, the preference stands until the first
    # create is refused - then it is dropped and the row says why
    fake_hub.items.clear()
    assert _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))["tunnel_active"] is True
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    assert row["tunnel"] is False and row["tunnel_reason"] == "tunnel_not_available"
    assert hub_routes.tunnel_default() is False


async def test_one_record_is_switched_on_its_own(jp_fetch, fake_hub):
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    res = _json(await _post(jp_fetch, "api", "shares", share["id"], "tunnel", body={"tunnel": True}))
    assert res == {"id": share["id"], "tunnel": True}
    row = _json(await jp_fetch(NS, "api", "shares", share["id"]))
    assert row["tunnel"] is True and row["link"].startswith("https://share.example.com/")
    with pytest.raises(HTTPClientError) as exc:
        await _post(jp_fetch, "api", "shares", share["id"], "tunnel", body={"tunnel": "yes"})
    assert exc.value.code == 400
    with pytest.raises(HTTPClientError) as exc:
        await _post(jp_fetch, "api", "requests", "r_Fake_id_9999", "tunnel", body={"tunnel": False})
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


async def test_a_default_switch_on_after_create_names_why_it_failed(jp_fetch, fake_hub):
    await _post(jp_fetch, "api", "tunnel", body={"active": True})
    # the hub answers with an error: its own lab slug, not an unreachable hub
    fake_hub.overrides[("PUT", "requests/r_Fake_id_0001/tunnel")] = (500, {"status": 500, "message": "boom"})
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    assert row["tunnel_reason"] == "tunnel_not_switched_on"
    # the hub does not answer
    fake_hub.raise_for.add("requests/r_Fake_id_0002/tunnel")
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "z"}))
    assert row["tunnel_reason"] == "hub_unavailable"


async def test_a_header_switch_on_that_fails_partway_keeps_the_records_it_switched(jp_fetch, fake_hub):
    await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]})
    req = _json(await _post(jp_fetch, "api", "requests", body={"name": "y"}))
    fake_hub.overrides[("PUT", f"requests/{req['id']}/tunnel")] = (500, {"status": 500, "message": "boom"})
    with pytest.raises(HTTPClientError) as exc:
        await _post(jp_fetch, "api", "tunnel", body={"active": True})
    assert exc.value.code == 500
    # the share went on: the default must not read off over a record that is on
    assert fake_hub.items[0]["tunnel"] is True
    assert hub_routes.tunnel_default() is True
    state = _json(await jp_fetch(NS, "api", "tunnel"))
    assert (state["tunnel_active"], state["tunnel_default"]) == (True, True)


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


async def test_stream_answers_403_without_the_lab_credentials(jp_fetch, jp_http_port, jp_base_url, fake_hub, fake_hub_stream):
    resp = await AsyncHTTPClient().fetch(_stream_url(jp_http_port, jp_base_url), raise_error=False, request_timeout=5)
    assert resp.code == 403
    assert fake_hub_stream["opens"] == 0


async def test_hub_address_links_are_rewritten_to_the_browser_origin(jp_fetch, fake_hub):
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "x"}))
    assert not row["link"].startswith("http://hub:8080"), row["link"]
    assert row["link"].endswith(f"/s/{row['id']}")
