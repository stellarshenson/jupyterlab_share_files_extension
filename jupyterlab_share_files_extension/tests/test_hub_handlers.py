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
import zipfile
from pathlib import Path

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
        # where a fetch writes, set by the fixture to the server's root
        self.workspace_root = ""
        # records another user owns, their passwords, the grants an unlock
        # minted, and the headers each call carried
        self.foreign: list[dict] = []
        self.passwords: dict[str, str] = {}
        self.grants: dict[str, str] = {}
        self.headers: list[dict] = []

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

    def add_foreign(self, kind="share", names=(), password="", owner="bob"):
        """A record another user owns; returns its id."""
        id_ = self._new_id("r_" if kind == "request" else "")
        self.foreign.append({
            "id": id_, "kind": kind, "owner": owner, "title": "Their Record", "state": "ready",
            "files": [{"name": n, "size": 3, "sha256": "0" * 64} for n in names],
            "created_at": "2026-09-03T20:00:00Z", "has_password": bool(password),
        })
        if password:
            self.passwords[id_] = password
        return id_

    def _record(self, id_, action, body, headers):
        """``records/<id>``, as HUB-API-REQUEST-read-a-record-you-do-not-own.md
        asks for it."""
        item = next((i for i in self.items + self.foreign if i["id"] == id_), None)
        if item is None:
            return 404, {"status": 404, "message": "No such record"}
        if item.get("closed"):
            return 410, {"reason": "closed", "message": "The owner closed this record"}
        if action == "unlock":
            if self.passwords.get(id_) != body.get("password"):
                return 403, {"reason": "password_wrong", "message": "Wrong password"}
            grant = f"g-{self._new_id()}"
            self.grants[grant] = id_
            return 200, {"grant": grant, "expires_at": "2026-09-03T21:00:00Z"}
        if id_ in self.passwords and self.grants.get(headers.get("X-Fileshare-Grant", "")) != id_:
            return 401, {"reason": "password_required", "message": "This record needs its password"}
        if not action:
            return 200, self._with_url(item)
        if action == "upload":
            item["last_upload"] = {"state": "running"}
            item["progress"] = {"copied": 1, "total": 4}
            return 202, {}
        name = str(body.get("name") or "")
        files = [f for f in item["files"] if not name or f["name"] == name or f["name"].startswith(name + "/")]
        if name and not files:
            return 404, {"reason": "unknown_entry", "message": f"No entry {name}"}
        dest = Path(self.workspace_root) / str(body.get("dest") or "")
        if dest.exists():
            return 400, {"status": 400, "message": "The files were not copied (bad_path)"}
        dest.mkdir(parents=True)
        for f in files:
            (dest / f["name"]).parent.mkdir(parents=True, exist_ok=True)
            (dest / f["name"]).write_text("from the hub\n", encoding="utf-8")
        return 200, {"path": str(dest)}

    async def request(self, method, path, body=None, headers=None, timeout=None):
        self.calls.append((method, path, body))
        self.headers.append(dict(headers or {}))
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
        m = re.fullmatch(r"shares/([^/]+)/fetch", path)
        if m and method == "POST":
            # the hub creates `dest` and refuses one that exists, measured
            # against the real hub on 2026-09-23 (bad_path)
            if not any(i["id"] == m.group(1) for i in self.items):
                return 404, {"status": 404, "message": "No such share"}
            dest = Path(self.workspace_root or ".") / str(body.get("dest") or "")
            if dest.exists():
                return 400, {"status": 400, "message": "The files were not copied (bad_path)"}
            dest.mkdir(parents=True)
            (dest / "report.csv").write_text("from the hub\n", encoding="utf-8")
            return 200, {"path": str(dest)}
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
        m = re.fullmatch(r"records/([^/]+)(?:/(unlock|fetch|upload))?", path)
        if m:
            return self._record(m.group(1), m.group(2) or "", body or {}, headers or {})
        return 404, {"status": 404, "message": None}


@pytest.fixture
def fake_hub(monkeypatch, tmp_path, jp_root_dir):
    for key, value in HUB_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    # the user the hub spawned this lab for, who owns the fake hub's items
    monkeypatch.setenv("JUPYTERHUB_USER", "alice")
    hub = FakeHub()
    # a fetch writes into the workspace, as the hub does on its own mount
    hub.workspace_root = str(jp_root_dir)
    monkeypatch.setattr(hub_routes, "HubClient", lambda: hub)
    hub_routes._PASSWORDS.clear()
    hub_routes._GRANTS.clear()
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
        # a connected entry goes from the hub into the workspace, never
        # through the lab to the browser (ACC-HUBM-175)
        ("api", "connections", "share:hub:AAAAAAAA", "download"),
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

    async def request(method, path_, body=None, headers=None, timeout=None):
        if (method, path_) == ("GET", path):
            return await stalled.request(method, path_, body, headers, timeout)
        return await answer(method, path_, body, headers, timeout)

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


async def test_a_record_left_behind_the_switch_is_brought_onto_the_tunnel(jp_fetch, fake_hub):
    """DEF-HUB-101: the tunnel is one state, not a property of a record.

    A create whose switch-on never landed leaves the record off while the
    switch stays on, and its link stays on the hub's own network. Listing
    brings the straggler onto the tunnel rather than reporting the drift.
    """
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    assert _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))["tunnel_active"] is True
    # the hub lost the record's switch; `_with_url` then composes its url
    # from the hub's own address, which is the drift the panel showed
    next(i for i in fake_hub.items if i["id"] == share["id"])["tunnel"] = False

    rows = _json(await jp_fetch(NS, "api", "shares"))["shares"]
    assert [r["tunnel"] for r in rows] == [True]
    assert rows[0]["link"].startswith(fake_hub.tunnel_base)
    assert next(i for i in fake_hub.items if i["id"] == share["id"])["tunnel"] is True


async def test_nothing_is_switched_while_the_toggle_is_off(jp_fetch, fake_hub):
    """The reconciliation follows the switch: off means no record is touched."""
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["a.txt"]}))
    assert hub_routes.tunnel_default() is False
    rows = _json(await jp_fetch(NS, "api", "shares"))["shares"]
    assert [r["tunnel"] for r in rows] == [False]
    assert next(i for i in fake_hub.items if i["id"] == share["id"])["tunnel"] is False


async def test_a_switch_off_writes_its_intent_before_it_reads_the_records(jp_fetch, fake_hub):
    """The window to close is the one before the write, not after it.

    A switch off takes a round trip per record, and the listing path reads
    the same switch. While that switch still read on, a listing arriving
    during the handler's own items read reconciled every record back on, so
    the owner kept public links under a header reading off.
    """
    _json(await _post(jp_fetch, "api", "shares", body={"name": "a", "paths": ["a.txt"]}))
    assert _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))["tunnel_active"] is True

    inner = fake_hub.request
    seen: list[tuple[str, bool]] = []

    async def record(method, path, body=None, headers=None, timeout=None):
        # what a listing arriving at this moment would read
        seen.append((path, hub_routes.tunnel_default()))
        return await inner(method, path, body, headers, timeout)

    fake_hub.request = record
    assert _json(await _post(jp_fetch, "api", "tunnel", body={"active": False}))["tunnel_active"] is False
    # from the read of the records onward - the capabilities read ahead of it
    # touches no record and is the hub's own answer to the panel
    after = seen[next(i for i, (path, _) in enumerate(seen) if path == "items"):]
    assert after and not any(on for _, on in after), seen


async def test_a_switch_off_under_a_reconciliation_leaves_no_record_published(jp_fetch, fake_hub):
    """The reconciliation reads the switch after its write, so it can take it
    back: the owner can press off while a write is in flight, and a write
    that lands after the press would otherwise leave that one record on the
    tunnel for good - nothing switches a record off outside this handler."""
    for name in ("a", "b", "c"):
        _json(await _post(jp_fetch, "api", "shares", body={"name": name, "paths": ["a.txt"]}))
    assert _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))["tunnel_active"] is True
    for item in fake_hub.items:
        item["tunnel"] = False  # three records behind the switch

    inner = fake_hub.request
    in_flight = asyncio.Event()
    release = asyncio.Event()
    gate = True

    async def gated(method, path, body=None, headers=None, timeout=None):
        nonlocal gate
        if gate and method == "PUT" and path.endswith("/tunnel") and body.get("tunnel"):
            gate = False
            in_flight.set()
            await release.wait()
        return await inner(method, path, body, headers, timeout)

    fake_hub.request = gated

    async def switch_off():
        await in_flight.wait()
        answer = await _post(jp_fetch, "api", "tunnel", body={"active": False})
        release.set()
        return answer

    _, off = await asyncio.gather(jp_fetch(NS, "api", "shares"), switch_off())
    assert _json(off)["tunnel_active"] is False
    assert hub_routes.tunnel_default() is False
    assert [i["tunnel"] for i in fake_hub.items] == [False, False, False]


async def test_a_refused_switch_off_leaves_the_switch_where_it_stood(jp_fetch, fake_hub):
    """A switch off the hub refuses must not answer by switching sharing on.

    The rollback exists so the switch never reads off while a record is still
    on the tunnel, but writing a fixed "on" turned a failed "stop sharing"
    into "share everything": the next listing reconciled every record onto
    the tunnel. It puts back what the switch said before the request.
    """
    share = _json(await _post(jp_fetch, "api", "shares", body={"name": "a", "paths": ["a.txt"]}))
    assert hub_routes.tunnel_default() is False
    # one record on the tunnel while the switch reads off, and a hub that
    # refuses to take it off again
    next(i for i in fake_hub.items if i["id"] == share["id"])["tunnel"] = True
    fake_hub.overrides[("PUT", f"shares/{share['id']}/tunnel")] = (500, {"message": "no"})

    with pytest.raises(HTTPClientError):
        await _post(jp_fetch, "api", "tunnel", body={"active": False})
    assert hub_routes.tunnel_default() is False


async def test_a_switch_off_that_fails_partway_keeps_the_records_it_closed(jp_fetch, fake_hub):
    """A press that moved records keeps the press.

    Writing back what the switch said before the request was right only when
    the request moved nothing. From the state an owner actually presses off
    from - the switch on, every record published - a hub refusing one record
    put the switch back to on, and the next listing reconciled the records
    the press had already taken off the tunnel straight back onto it.
    """
    a = _json(await _post(jp_fetch, "api", "shares", body={"name": "a", "paths": ["a.txt"]}))
    b = _json(await _post(jp_fetch, "api", "shares", body={"name": "b", "paths": ["a.txt"]}))
    assert _json(await _post(jp_fetch, "api", "tunnel", body={"active": True}))["tunnel_active"] is True
    assert [i["tunnel"] for i in fake_hub.items] == [True, True]

    fake_hub.overrides[("PUT", f"shares/{b['id']}/tunnel")] = (500, {"message": "no"})
    with pytest.raises(HTTPClientError):
        await _post(jp_fetch, "api", "tunnel", body={"active": False})

    assert hub_routes.tunnel_default() is False
    rows = _json(await jp_fetch(NS, "api", "shares"))["shares"]
    # a stays closed; b is the one the hub refused and reports itself on
    assert {r["id"]: r["tunnel"] for r in rows} == {a["id"]: False, b["id"]: True}


async def test_a_path_that_leaves_the_workspace_through_a_link_never_reaches_the_hub(
    jp_fetch, fake_hub, jp_root_dir, tmp_path
):
    """DEF-HUB-105: a path that resolves outside the notebook root is accepted
    by the hub and then refused whole with `bad_filename`, so the lab refuses
    it first."""
    outside = tmp_path / "outside"
    (outside / "my-gpu").mkdir(parents=True)
    (jp_root_dir / "@shared").mkdir()
    (jp_root_dir / "@shared" / "skills").symlink_to(outside)

    for body in (
        {"name": "x", "paths": ["@shared/skills/my-gpu"]},
        {"name": "x", "paths": ["@shared/skills"]},
    ):
        with pytest.raises(HTTPClientError) as err:
            await _post(jp_fetch, "api", "shares", body=body)
        assert err.value.code == 400
        assert "leaves your workspace through a link" in err.value.response.body.decode()
    assert not any(c[0] == "POST" for c in fake_hub.calls)


async def test_an_add_of_a_path_outside_the_workspace_is_refused_by_name(
    jp_fetch, fake_hub, jp_root_dir, tmp_path
):
    """The same guard on the other way in: adding to a share that already
    exists. The refusal names the path, because the panel shows the sentence
    and the owner has to know which of the chosen items to leave out."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (jp_root_dir / "elsewhere").symlink_to(outside)
    id_ = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": []}))["id"]

    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "shares", id_, "items", body={"paths": ["elsewhere"]})
    assert err.value.code == 400
    assert "elsewhere leaves your workspace" in err.value.response.body.decode()
    assert not any(c[1] == f"shares/{id_}/content" for c in fake_hub.calls)


async def test_a_link_that_stays_inside_the_workspace_is_still_sent(
    jp_fetch, fake_hub, jp_root_dir
):
    """The guard is about leaving the volume, not about links: the hub reads
    a link whose target is inside the workspace, and skips it in the copy."""
    (jp_root_dir / "data").mkdir()
    (jp_root_dir / "shortcut").symlink_to(jp_root_dir / "data")
    await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": ["shortcut"]})
    assert any(c[:2] == ("POST", "shares") and c[2]["paths"] == ["shortcut"] for c in fake_hub.calls)


async def test_a_fetch_into_a_folder_outside_the_workspace_never_reaches_the_hub(
    jp_fetch, fake_hub, jp_root_dir, tmp_path
):
    """DEF-HUB-106: the write direction of DEF-HUB-105. The hub creates the
    destination on its own mount of the volume, so a folder reached through a
    link out of the workspace is one it cannot write to. `is_dir` follows the
    link, so the lab sees a folder and only the real locations show it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (jp_root_dir / "elsewhere").symlink_to(outside)
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "Inbox"}))

    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "requests", row["id"], "uploads", "u1", "fetch",
                    body={"target_dir": "elsewhere", "name": "Inbox"})
    assert err.value.code == 400
    assert "elsewhere leaves your workspace" in err.value.response.body.decode()
    assert not any(c[1].endswith("/fetch") for c in fake_hub.calls)


async def test_a_fetch_into_a_link_that_stays_inside_the_workspace_is_still_sent(
    jp_fetch, fake_hub, jp_root_dir
):
    """The guard is about leaving the workspace, not about links: a folder
    the lab reaches through a link that stays inside is one the hub writes."""
    (jp_root_dir / "inbox").mkdir()
    (jp_root_dir / "shortcut").symlink_to(jp_root_dir / "inbox")
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "Inbox"}))
    await _post(jp_fetch, "api", "requests", row["id"], "uploads", "u1", "fetch",
                body={"target_dir": "shortcut", "name": "Inbox"})
    assert any(c[1] == f"requests/{row['id']}/uploads/u1/fetch" and c[2]["dest"] == "shortcut/Inbox"
               for c in fake_hub.calls)


async def test_a_whole_hub_share_is_saved_into_the_chosen_folder(jp_fetch, fake_hub, jp_root_dir):
    """ACC-SAVE-171: the hub owns the bytes, so the lab names a folder that is
    free and the hub writes the record into it."""
    (jp_root_dir / "out").mkdir()
    row = _json(await _post(jp_fetch, "api", "shares", body={"name": "Quarter Report", "paths": []}))
    answer = _json(await _post(jp_fetch, "api", "shares", row["id"], "save", body={"target_dir": "out"}))
    assert answer == {"ok": True, "path": "out/Quarter-Report"}
    assert (jp_root_dir / "out" / "Quarter-Report" / "report.csv").exists()
    dest = next(c[2]["dest"] for c in fake_hub.calls if c[1] == f"shares/{row['id']}/fetch")
    assert re.fullmatch(r"out/\.Quarter-Report-part-[0-9a-f]{8}", dest)
    # a second save lands beside the first, because the hub refuses a folder
    # that exists and the lab picks the free name
    again = _json(await _post(jp_fetch, "api", "shares", row["id"], "save", body={"target_dir": "out"}))
    assert again["path"] == "out/Quarter-Report-2"


async def test_a_hub_share_is_packed_into_a_zip_from_what_the_hub_wrote(
    jp_fetch, fake_hub, jp_root_dir
):
    """The hub packs no archive, so the lab zips what the hub wrote and the
    staging folder goes - the archive is what is left."""
    (jp_root_dir / "out").mkdir()
    row = _json(await _post(jp_fetch, "api", "shares", body={"name": "Packed", "paths": []}))
    answer = _json(await _post(jp_fetch, "api", "shares", row["id"], "save",
                               body={"target_dir": "out", "archive": "zip"}))
    assert answer["path"] == "out/Packed.zip"
    landed = jp_root_dir / "out" / "Packed.zip"
    with zipfile.ZipFile(landed) as bundle:
        assert bundle.namelist() == ["report.csv"]
    assert [p.name for p in (jp_root_dir / "out").iterdir()] == ["Packed.zip"]


async def test_a_hub_request_saves_its_uploads_into_one_folder(
    jp_fetch, fake_hub, jp_root_dir
):
    """The hub carries no route that takes a request's uploads as a set, so
    the lab makes the folder and fetches them into it one at a time."""
    (jp_root_dir / "out").mkdir()
    row = _json(await _post(jp_fetch, "api", "requests", body={"name": "Inbox"}))
    fake_hub.uploads[row["id"]] = [
        {"upload_id": "u1", "filename": "one.txt", "size": 3, "sha256": "x", "uploaded_at": "2026-09-03T21:00:00Z"},
        {"upload_id": "u2", "filename": "two.txt", "size": 3, "sha256": "x", "uploaded_at": "2026-09-03T21:00:00Z"},
    ]
    answer = _json(await _post(jp_fetch, "api", "requests", row["id"], "save", body={"target_dir": "out"}))
    assert answer["path"] == "out/Inbox"
    sent = [c for c in fake_hub.calls if c[1].startswith(f"requests/{row['id']}/uploads/")]
    # fetched into a folder only this save names, then moved to the free name
    staging = sent[0][2]["dest"].rsplit("/", 1)[0]
    assert re.fullmatch(r"out/\.Inbox-part-[0-9a-f]{8}", staging)
    assert [c[2]["dest"] for c in sent] == [f"{staging}/one.txt", f"{staging}/two.txt"]
    assert [p.name for p in (jp_root_dir / "out").iterdir()] == ["Inbox"]


async def test_a_hub_save_refuses_an_archive_it_does_not_know(jp_fetch, fake_hub, jp_root_dir):
    row = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": []}))
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "shares", row["id"], "save", body={"archive": "tar"})
    assert err.value.code == 400
    assert not any(c[1].endswith("/fetch") for c in fake_hub.calls)


async def test_a_hub_save_into_a_folder_outside_the_workspace_never_reaches_the_hub(
    jp_fetch, fake_hub, jp_root_dir, tmp_path
):
    """The same guard the upload fetch carries, on the other route that hands
    the hub a folder to write into (DEF-HUB-106)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (jp_root_dir / "elsewhere").symlink_to(outside)
    row = _json(await _post(jp_fetch, "api", "shares", body={"name": "x", "paths": []}))
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "shares", row["id"], "save", body={"target_dir": "elsewhere"})
    assert err.value.code == 400
    assert "elsewhere leaves your workspace" in err.value.response.body.decode()
    assert not any(c[1].endswith("/fetch") for c in fake_hub.calls)


async def test_one_entry_of_a_hub_share_is_moved_out_of_a_fetched_record(
    jp_fetch, fake_hub, jp_root_dir
):
    """ACC-SAVE-170: the hub reads no single entry, so the record is fetched
    and the named entry is moved out of it; the rest goes."""
    (jp_root_dir / "out").mkdir()
    row = _json(await _post(jp_fetch, "api", "shares", body={"name": "Picked", "paths": []}))
    answer = _json(await _post(jp_fetch, "api", "shares", row["id"], "save",
                               body={"target_dir": "out", "name": "report.csv"}))
    assert answer["path"] == "out/report.csv"
    assert (jp_root_dir / "out" / "report.csv").read_text() == "from the hub\n"
    assert [p.name for p in (jp_root_dir / "out").iterdir()] == ["report.csv"]


async def test_an_entry_the_record_does_not_hold_leaves_nothing_behind(
    jp_fetch, fake_hub, jp_root_dir
):
    (jp_root_dir / "out").mkdir()
    row = _json(await _post(jp_fetch, "api", "shares", body={"name": "Picked", "paths": []}))
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "shares", row["id"], "save",
                    body={"target_dir": "out", "name": "nosuch.csv"})
    assert err.value.code == 404
    assert list((jp_root_dir / "out").iterdir()) == []


# --------------------------------------------------------------------------- #
# Connections: records another user owns (ACC-HUBM-173 to ACC-HUBM-180)
# --------------------------------------------------------------------------- #


def test_a_hub_link_or_a_bare_id_names_the_record():
    parse = hub_routes.parse_hub_link
    assert parse("Abc_123-xyz") == "Abc_123-xyz"
    assert parse("http://hub:8080/s/policy1/Abc_123-xyz") == "Abc_123-xyz"
    assert parse("https://share.example.com/s/policy1/Abc_123-xyz/") == "Abc_123-xyz"
    assert parse("https://lab.example.com/hub/s/policy1/r_Abc_123") == "r_Abc_123"
    for text in ("notes/report.csv", "https://example.com/other/Abc_123-xyz", "ftp://hub/s/p/Abc_123-xyz", ""):
        with pytest.raises(ValueError):
            parse(text)


def test_a_records_files_become_its_top_level_entries():
    files = [{"name": "a.csv", "size": 3}, {"name": "d/x.txt", "size": 2}, {"name": "d/e/y.txt", "size": 5}]
    assert hub_routes.remote_entries(files) == [
        {"name": "a.csv", "type": "file", "size": 3},
        {"name": "d", "type": "directory", "size": 7},
    ]


async def _connect(jp_fetch, link, password=""):
    return _json(await _post(jp_fetch, "api", "connections", body={"link": link, "password": password}))


async def test_connecting_by_link_lists_the_record_and_its_entries(jp_fetch, fake_hub):
    id_ = fake_hub.add_foreign(names=["a.csv", "d/x.txt"])
    entry = await _connect(jp_fetch, f"http://hub:8080/s/policy1/{id_}")
    assert (entry["kind"], entry["id"], entry["owner"]) == ("share", id_, "bob")
    listed = _json(await jp_fetch(NS, "api", "connections"))["connections"]
    assert [c["key"] for c in listed] == [entry["key"]]
    manifest = _json(await jp_fetch(NS, "api", "connections", entry["key"], "manifest"))
    assert manifest["entries"] == [
        {"name": "a.csv", "type": "file", "size": 3},
        {"name": "d", "type": "directory", "size": 3},
    ]
    # every call went to the hub; the lab fetched no recipient page
    assert all(path.startswith("records/") for _, path, _ in fake_hub.calls)


async def test_connecting_to_your_own_record_is_refused(jp_fetch, fake_hub):
    own = _json(await _post(jp_fetch, "api", "shares", body={"name": "Mine", "paths": []}))
    with pytest.raises(HTTPClientError) as err:
        await _connect(jp_fetch, own["id"])
    assert err.value.code == 400
    assert "your own" in json.loads(err.value.response.body)["error"]
    assert _json(await jp_fetch(NS, "api", "connections"))["connections"] == []


async def test_a_protected_record_asks_for_its_password_and_keeps_it(jp_fetch, fake_hub):
    id_ = fake_hub.add_foreign(names=["a.csv"], password="pw")
    for password, error in (("", "password required"), ("nope", "wrong password")):
        with pytest.raises(HTTPClientError) as err:
            await _connect(jp_fetch, id_, password)
        assert err.value.code == 401
        assert json.loads(err.value.response.body) == {"error": error, "password_required": True}
    entry = await _connect(jp_fetch, id_, "pw")
    # a grant lost on restart is earned again from the stored password
    hub_routes._GRANTS.clear()
    manifest = _json(await jp_fetch(NS, "api", "connections", entry["key"], "manifest"))
    assert [e["name"] for e in manifest["entries"]] == ["a.csv"]
    assert fake_hub.headers[-1]["X-Fileshare-Grant"].startswith("g-")


async def test_a_password_the_owner_set_or_changed_after_the_connect_is_named(jp_fetch, fake_hub, jp_root_dir):
    """The hub's password_required is its group-policy sentence and its
    password_wrong is a slug the panel does not know: neither says what
    happened to a connection, so the lab answers with its own reason."""
    open_id = fake_hub.add_foreign(names=["a.csv"])
    locked_id = fake_hub.add_foreign(names=["a.csv"], password="pw")
    open_key = (await _connect(jp_fetch, open_id))["key"]
    locked_key = (await _connect(jp_fetch, locked_id, "pw"))["key"]
    fake_hub.passwords[open_id] = "set-later"
    fake_hub.passwords[locked_id] = "changed"
    hub_routes._GRANTS.clear()
    (jp_root_dir / "out").mkdir()
    for key in (open_key, locked_key):
        for call in (
            lambda: jp_fetch(NS, "api", "connections", key, "manifest"),
            lambda: _post(jp_fetch, "api", "connections", key, "save", body={"target_dir": "out"}),
        ):
            with pytest.raises(HTTPClientError) as err:
                await call()
            assert err.value.code == 401
            assert json.loads(err.value.response.body)["reason"] == "password_changed"
    assert list((jp_root_dir / "out").iterdir()) == []


async def test_a_connected_share_saves_an_entry_the_whole_and_a_zip(jp_fetch, fake_hub, jp_root_dir):
    id_ = fake_hub.add_foreign(names=["a.csv", "d/x.txt"])
    key = (await _connect(jp_fetch, id_))["key"]
    (jp_root_dir / "out").mkdir()
    save = lambda **body: _post(jp_fetch, "api", "connections", key, "save", body={"target_dir": "out", **body})  # noqa: E731
    assert _json(await save(names=["a.csv"]))["saved"] == ["out/a.csv"]
    assert _json(await save(names=["d"]))["saved"] == ["out/d"]
    assert (jp_root_dir / "out/d/x.txt").is_file()
    assert _json(await save())["saved"] == ["out/Their-Record"]
    assert (jp_root_dir / "out/Their-Record/d/x.txt").is_file()
    assert _json(await save(archive="zip"))["saved"] == ["out/Their-Record.zip"]
    with zipfile.ZipFile(jp_root_dir / "out/Their-Record.zip") as zf:
        assert sorted(zf.namelist()) == ["a.csv", "d/x.txt"]
    # the staging folders the hub wrote into are gone
    assert sorted(p.name for p in (jp_root_dir / "out").iterdir()) == [
        "Their-Record", "Their-Record.zip", "a.csv", "d"]


@pytest.mark.parametrize("failure", ["500", "unreachable"])
@pytest.mark.parametrize("body", [{}, {"archive": "zip"}, {"names": ["a.csv"]}])
async def test_a_connected_save_the_hub_fails_leaves_nothing_behind(
    jp_fetch, fake_hub, jp_root_dir, monkeypatch, failure, body
):
    """The hub writes part of the record and then fails: the save answers
    the error and the folder holds nothing it did not hold before."""
    key = (await _connect(jp_fetch, fake_hub.add_foreign(names=["a.csv", "d/x.txt"])))["key"]
    (jp_root_dir / "out").mkdir()
    answer = fake_hub.request

    async def request(method, path, body=None, headers=None, timeout=None):
        code, data = await answer(method, path, body, headers, timeout)
        if path.endswith("/fetch"):
            if failure == "unreachable":
                raise HubUnavailable("could not reach the hub: reset")
            return 500, {"status": 500, "message": "copy failed"}
        return code, data

    monkeypatch.setattr(fake_hub, "request", request)
    with pytest.raises(HTTPClientError):
        await _post(jp_fetch, "api", "connections", key, "save", body={"target_dir": "out", **body})
    assert list((jp_root_dir / "out").iterdir()) == []


@pytest.mark.parametrize("failure", ["500", "unreachable"])
async def test_an_own_share_save_the_hub_fails_leaves_nothing_behind(
    jp_fetch, fake_hub, jp_root_dir, monkeypatch, failure
):
    row = _json(await _post(jp_fetch, "api", "shares", body={"name": "Mine", "paths": []}))
    (jp_root_dir / "out").mkdir()
    answer = fake_hub.request

    async def request(method, path, body=None, headers=None, timeout=None):
        code, data = await answer(method, path, body, headers, timeout)
        if path.endswith("/fetch"):
            if failure == "unreachable":
                raise HubUnavailable("could not reach the hub: reset")
            return 500, {"status": 500, "message": "copy failed"}
        return code, data

    monkeypatch.setattr(fake_hub, "request", request)
    with pytest.raises(HTTPClientError):
        await _post(jp_fetch, "api", "shares", row["id"], "save", body={"target_dir": "out"})
    assert list((jp_root_dir / "out").iterdir()) == []


def _slow_hub(seconds):
    """A hub that answers after ``seconds``, as one still copying does."""

    class Slow(tornado.web.RequestHandler):
        async def get(self, path):
            await asyncio.sleep(seconds)
            self.finish("{}")

    sock, port = bind_unused_port()
    server = tornado.httpserver.HTTPServer(tornado.web.Application([(r"/(.*)", Slow)]))
    server.add_socket(sock)
    return server, HubClient(base=f"http://127.0.0.1:{port}/hub/api/fileshare", token="t0k3n")


@pytest.mark.parametrize("record", ["connected share", "own share", "own request", "own upload"])
async def test_a_hub_copy_longer_than_an_api_call_still_saves(
    jp_fetch, fake_hub, jp_root_dir, monkeypatch, record
):
    """A fetch answers once the hub has copied the record, so it waits as
    long as the record's size asks for, not the seconds of an API call."""
    monkeypatch.setattr("jupyterlab_share_files_extension.hub.REQUEST_TIMEOUT_SECONDS", 0.2)
    (jp_root_dir / "out").mkdir()
    if record == "connected share":
        key = (await _connect(jp_fetch, fake_hub.add_foreign(names=["a.csv"])))["key"]
        route = ("api", "connections", key, "save")
    else:
        kind = "shares" if record == "own share" else "requests"
        row = _json(await _post(jp_fetch, "api", kind, body={"name": "Mine", "paths": []}))
        fake_hub.uploads[row["id"]] = [
            {"upload_id": "u1", "filename": "one.txt", "size": 3, "sha256": "x", "uploaded_at": "2026-09-03T21:00:00Z"}
        ]
        route = ("api", kind, row["id"], "save")
        if record == "own upload":
            route = ("api", kind, row["id"], "uploads", "u1", "fetch")
    server, copying = _slow_hub(0.5)
    answer = fake_hub.request

    async def request(method, path, body=None, headers=None, **kw):
        if path.endswith("/fetch"):
            await copying.request("GET", "copying", None, None, **kw)
        return await answer(method, path, body, headers, **kw)

    monkeypatch.setattr(fake_hub, "request", request)
    try:
        saved = _json(await _post(jp_fetch, *route, body={"target_dir": "out"}))
    finally:
        server.stop()
    assert saved.get("saved") or saved.get("path")


@pytest.mark.parametrize("record", ["connected share", "own share"])
async def test_two_saves_of_one_record_at_once_both_land(jp_fetch, fake_hub, jp_root_dir, monkeypatch, record):
    """Both saves pick the free name before either lands. The hub refuses a
    dest that exists and a refused save removes its staging, so a shared name
    would let the second save remove what the first one wrote."""
    (jp_root_dir / "out").mkdir()
    if record == "connected share":
        key = (await _connect(jp_fetch, fake_hub.add_foreign(names=["a.csv"])))["key"]
        route = ("api", "connections", key, "save")
    else:
        row = _json(await _post(jp_fetch, "api", "shares", body={"name": "Their Record", "paths": []}))
        route = ("api", "shares", row["id"], "save")
    answer = fake_hub.request

    async def request(method, path, body=None, headers=None, timeout=None):
        if not path.endswith("/fetch"):
            return await answer(method, path, body, headers, timeout)
        await asyncio.sleep(0.2)  # both saves have picked a name by now
        result = await answer(method, path, body, headers, timeout)
        await asyncio.sleep(0.2)  # the hub is still copying when the other save arrives
        return result

    monkeypatch.setattr(fake_hub, "request", request)
    results = await asyncio.gather(
        *(_post(jp_fetch, *route, body={"target_dir": "out"}) for _ in range(2)), return_exceptions=True
    )
    landed = []
    for res in results:
        assert not isinstance(res, Exception), res
        body = _json(res)
        landed += body.get("saved") or [body["path"]]
    assert sorted(landed) == ["out/Their-Record", "out/Their-Record-2"]
    assert all((jp_root_dir / path).is_dir() for path in landed)


async def test_a_password_the_hub_refused_is_not_tried_again(jp_fetch, fake_hub):
    """The hub rate-limits unlocks (429). A stored password it refused would
    otherwise be sent at every read and use up the attempts the user needs
    to connect again with the new one."""
    id_ = fake_hub.add_foreign(names=["a.csv"], password="pw")
    key = (await _connect(jp_fetch, id_, "pw"))["key"]
    fake_hub.passwords[id_] = "changed"
    hub_routes._GRANTS.clear()
    for _ in range(3):
        with pytest.raises(HTTPClientError) as err:
            await jp_fetch(NS, "api", "connections", key, "manifest")
        assert json.loads(err.value.response.body)["reason"] == "password_changed"
    unlocks = [c for c in fake_hub.calls if c[1] == f"records/{id_}/unlock"]
    assert len(unlocks) == 2  # the connect, then one refused retry


async def test_a_refused_password_never_drops_the_one_a_connect_just_stored(jp_fetch, fake_hub, monkeypatch):
    """A read sent with the old password can be refused after the user
    connected again with the new one: only the refused password goes."""
    id_ = fake_hub.add_foreign(names=["a.csv"], password="first")
    key = (await _connect(jp_fetch, id_, "first"))["key"]
    fake_hub.passwords[id_] = "second"
    hub_routes._GRANTS.clear()
    answer = fake_hub.request

    async def request(method, path, body=None, headers=None, timeout=None):
        if path.endswith("/unlock") and body == {"password": "first"}:
            # the reconnect with the new password lands while this read waits
            hub_routes._save_hub_connections(
                [{**c, "password": "second"} for c in hub_routes._hub_connections()]
            )
        return await answer(method, path, body, headers, timeout)

    monkeypatch.setattr(fake_hub, "request", request)
    with pytest.raises(HTTPClientError):
        await jp_fetch(NS, "api", "connections", key, "manifest")
    assert [c.get("password") for c in hub_routes._hub_connections()] == ["second"]


def test_a_fetch_waits_by_the_size_it_copies():
    assert hub_routes.fetch_timeout(0) == hub_routes.FETCH_SECONDS_PER_GB
    assert hub_routes.fetch_timeout(5 * 1000**3) == 5 * hub_routes.FETCH_SECONDS_PER_GB


async def test_a_connected_save_refuses_a_zip_of_one_entry_and_a_missing_entry(jp_fetch, fake_hub):
    key = (await _connect(jp_fetch, fake_hub.add_foreign(names=["a.csv"])))["key"]
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "connections", key, "save", body={"names": ["a.csv"], "archive": "zip"})
    assert err.value.code == 400
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "connections", key, "save", body={"names": ["gone.csv"]})
    assert err.value.code == 404


async def test_a_connected_request_upload_is_sent_and_followed_on_the_manifest(jp_fetch, fake_hub, jp_root_dir):
    id_ = fake_hub.add_foreign(kind="request")
    key = (await _connect(jp_fetch, id_))["key"]
    (jp_root_dir / "folder").mkdir()
    (jp_root_dir / "a.txt").write_text("a", encoding="utf-8")
    answer = _json(await _post(jp_fetch, "api", "connections", key, "upload", body={"paths": ["a.txt", "folder"]}))
    assert answer["uploaded"] == ["a.txt", "folder"]
    method, path, body = fake_hub.calls[-1]
    assert (method, path, body["paths"]) == ("POST", f"records/{id_}/upload", ["a.txt", "folder"])
    assert isinstance(body["exclude"], list)
    manifest = _json(await jp_fetch(NS, "api", "connections", key, "manifest"))
    assert (manifest["uploading"], manifest["progress"]) == (True, {"copied": 1, "total": 4})
    item = fake_hub.foreign[0]
    item.pop("progress")
    item["last_upload"] = {"state": "done", "count": 2}
    manifest = _json(await jp_fetch(NS, "api", "connections", key, "manifest"))
    assert (manifest["uploading"], manifest["uploaded"], manifest["progress"]) == (False, 2, None)


async def test_an_upload_leaving_the_workspace_is_refused_before_the_hub(jp_fetch, fake_hub, jp_root_dir, tmp_path):
    key = (await _connect(jp_fetch, fake_hub.add_foreign(kind="request")))["key"]
    (jp_root_dir / "out").symlink_to(tmp_path)
    calls = len(fake_hub.calls)
    with pytest.raises(HTTPClientError) as err:
        await _post(jp_fetch, "api", "connections", key, "upload", body={"paths": ["out"]})
    assert err.value.code == 400
    assert "leaves your workspace" in json.loads(err.value.response.body)["error"]
    assert len(fake_hub.calls) == calls


async def test_disconnect_leaves_the_record_and_a_closed_record_says_why(jp_fetch, fake_hub):
    id_ = fake_hub.add_foreign(names=["a.csv"])
    key = (await _connect(jp_fetch, id_))["key"]
    fake_hub.foreign[0]["closed"] = True
    with pytest.raises(HTTPClientError) as err:
        await jp_fetch(NS, "api", "connections", key, "manifest")
    assert (err.value.code, json.loads(err.value.response.body)["reason"]) == (410, "closed")
    await jp_fetch(NS, "api", "connections", key, method="DELETE")
    assert _json(await jp_fetch(NS, "api", "connections"))["connections"] == []
    assert [i["id"] for i in fake_hub.foreign] == [id_]
