"""A stand-in for galaxahub's fileshare API, for the hub-mode galata suite.

Speaks the routes the extension calls (`/hub/api/fileshare/*`) for one user,
in memory, and refuses any call whose token is not the one the test server is
spawned with - the same contract a real hub holds. Nothing here reaches a
network beyond the loopback port it listens on.

Share state is decided by the title so a test can ask for a row in any state:
a title containing ``stay-staging`` never leaves ``staging``, one containing
``refuse`` is refused with ``over_cap``, any other share is promoted to
``ready`` on the next listing with one file per submitted path.

Links follow the galaxahub deployed on 2026-09-22:
``capabilities.public_base_url`` is always the hub's own address, and a
tunnel-on record's url carries the tunnel base only once
``capabilities.tunnel_ready`` is true. The first record switched on
starts the connector, which is ready after ``tunnel_delay`` seconds (never
while ``tunnel_registers`` is false); the hub rings the change stream every
time that verdict flips. ``/s/<policy id>/<id>`` is the recipient page the
lab's link check opens - the hub puts the group's policy id before the
record id.

``records/<id>`` is the route set the lab asked the hub for in
HUB-API-REQUEST-read-a-record-you-do-not-own.md: read, unlock, fetch from and
upload into a record another user owns. ``MOCK_HUB_ROOT`` is the lab's root
folder; with it, a fetch writes the record's files there as the hub does
through its own mount of the volume.

``/_control/*`` is the test's own side door: reset the store, change the
capabilities, the policy, the tunnel (its base, its delay and its ready
verdict), the recipient page and the status a Cloudflare switch off answers,
add an upload, add or close another user's record, ring the change stream,
read the recorded calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
from pathlib import Path

import tornado.ioloop
import tornado.web
from tornado.iostream import StreamClosedError

TOKEN = os.environ.get("MOCK_HUB_TOKEN", "test-token")
PORT = int(os.environ.get("MOCK_HUB_PORT") or "8765")
BASE = f"http://127.0.0.1:{PORT}"
# the group's policy id, the first segment of every public link
POLICY_ID = "mockpolicy"
# the lab's root folder, which the hub reaches through its own mount
ROOT = os.environ.get("MOCK_HUB_ROOT", "")


ADD_SECONDS = 0.4


class Store:
    def __init__(self):
        self.reset()

    def reset(self):
        self.calls: list[dict] = []
        self.capabilities = {
            "allow_share": True, "allow_request": True,
            "max_upload_bytes": 10737418240, "max_share_bytes": 5368709120,
            "max_shares": 20, "retention_days": 14,
            "public_base_url": BASE, "serving": True, "password_required": False,
            # the group policy has a tunnel at all, and the connector is up
            "tunnel_available": True, "tunnel_ready": False,
        }
        # the group policy's Cloudflare switch - a record may be switched on
        # only while it is on (the real hub's `file_sharing_cloudflare_enabled`)
        self.cloudflare_enabled = True
        # how long an add stays running before it settles
        self.add_seconds = ADD_SECONDS
        # the hub's Cloudflare tunnel; the default base is a second origin on
        # this loopback port, so a tunnel link resolves without a network
        if getattr(self, "tunnel_timer", None) is not None:
            self.tunnel_timer.cancel()
        self.tunnel_timer = None
        self.tunnel_base = f"http://localhost:{PORT}"
        self.tunnel_delay = 0.0
        self.tunnel_registers = True
        # the status the recipient page answers for a record that exists
        self.page_status = 200
        # the status a Cloudflare switch off answers for a record that exists
        self.tunnel_off_status = 204
        self.streams: list[asyncio.Queue] = []
        self.items: list[dict] = []
        self.pending_paths: dict[str, list[str]] = {}
        self.adding: set[str] = set()
        self.uploads: dict[str, list[dict]] = {}
        # records other users own: never in this user's `items`
        self.foreign: list[dict] = []
        # a record's password by id, and the grants an unlock minted
        self.passwords: dict[str, str] = {}
        self.grants: dict[str, str] = {}
        self.counter = 0

    def start_tunnel(self):
        """The first record switched on starts the connector; it is ready
        after the delay."""
        if (self.capabilities["tunnel_ready"] or self.tunnel_timer is not None
                or not self.tunnel_registers):
            return
        if not self.tunnel_delay:
            return self.set_ready(True)
        self.tunnel_timer = asyncio.get_running_loop().call_later(
            self.tunnel_delay, lambda: self.set_ready(True))

    def set_ready(self, ready: bool):
        """The connector's verdict flips - the hub rings the change stream
        for it, so a panel learns a tunnel coming up or dropping."""
        self.capabilities["tunnel_ready"] = bool(ready)
        self.nudge()

    def nudge(self):
        """Ring every open stream - the hub's `fileshare_stream.nudge`."""
        for queue in self.streams:
            try:
                queue.put_nowait("changed")
            except asyncio.QueueFull:
                pass

    def new_id(self, prefix=""):
        self.counter += 1
        return f"{prefix}MockId_{self.counter:016d}"[:24]

    def promote(self):
        """The mediator's verdict, delivered on the next listing."""
        for item in self.items:
            if item["kind"] != "share" or item["state"] != "staging":
                continue
            title = item["title"]
            if "stay-staging" in title:
                continue
            if "refuse" in title:
                item["state"] = "refused"
                item["reason"] = "over_cap"
                item.pop("progress", None)
                continue
            paths = self.pending_paths.pop(item["id"], [])
            item["files"] = _files(paths)
            item["bytes"] = 42 * len(item["files"])
            item["state"] = "ready"
            item.pop("progress", None)
            self.nudge()


def _files(paths) -> list[dict]:
    """The files one copy lands: a name with no dot stands for a folder and
    lands as one file under it."""
    names = [posixpath.basename(p.rstrip("/")) or p for p in paths]
    return [
        {"name": n if "." in n else f"{n}/inside.txt", "size": 42, "sha256": "0" * 64}
        for n in names
    ]


STORE = Store()


def _write_files(dest: str, files: list[dict]) -> bool:
    """Write ``files`` under ``dest`` in the lab's root, as the hub writes
    into a workspace. False when ``dest`` exists - the hub refuses it."""
    if not ROOT:
        return True
    target = Path(ROOT) / dest
    if target.exists():
        return False
    target.mkdir(parents=True)
    for f in files:
        path = target / f["name"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"0" * int(f.get("size") or 0))
    return True


class _Base(tornado.web.RequestHandler):
    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def body(self) -> dict:
        try:
            data = json.loads(self.request.body or b"{}")
        except ValueError:
            raise tornado.web.HTTPError(400)
        return data if isinstance(data, dict) else {}

    def answer(self, code: int, payload=None):
        self.set_status(code)
        if payload is None:
            self.finish()
        else:
            self.finish(json.dumps(payload))

    def write_error(self, status_code, **kwargs):
        self.finish(json.dumps({"status": status_code, "message": self._reason}))


class _Hub(_Base):
    """Every fileshare route: token-gated like the real hub's handlers."""

    def prepare(self):
        STORE.calls.append({
            "method": self.request.method, "path": self.request.path,
            "body": (self.request.body or b"").decode("utf-8", "replace"),
            "auth": self.request.headers.get("Authorization", ""),
        })
        if self.request.headers.get("Authorization", "") != f"token {TOKEN}":
            self.answer(403, {"status": 403, "message": "Authentication required"})
            raise tornado.web.Finish()


class Capabilities(_Hub):
    def get(self):
        self.answer(200, STORE.capabilities)


def _with_url(item: dict) -> dict:
    """The row as the hub answers it: the url is composed per request, never
    stored - the tunnel base while the record's tunnel switch is on and the
    connector is ready, the hub's own address otherwise."""
    on_tunnel = item.get("tunnel") and STORE.capabilities["tunnel_ready"]
    base = STORE.tunnel_base.rstrip("/") if on_tunnel else BASE
    return {**item, "url": f"{base}/s/{POLICY_ID}/{item['id']}"}


class Items(_Hub):
    def get(self):
        STORE.promote()
        self.answer(200, {"items": [_with_url(i) for i in STORE.items]})


class Create(_Hub):
    def post(self, kind_plural):
        kind = "share" if kind_plural == "shares" else "request"
        body = self.body()
        title = str(body.get("title") or "").strip()
        if not title:
            return self.answer(400, {"status": 400, "message": "title is required"})
        allowed = STORE.capabilities["allow_share" if kind == "share" else "allow_request"]
        if not allowed:
            reason = STORE.capabilities.get("reason") or f"{kind}_not_granted"
            return self.answer(403, {"reason": reason, "message": f"Cannot create this {kind}: {reason}"})
        if STORE.capabilities.get("password_required") and not str(body.get("password") or "").strip():
            return self.answer(400, {"reason": "password_required",
                                     "message": f"Your group requires a password on every {kind}"})
        paths = body.get("paths") or []
        if kind == "share":
            if not isinstance(paths, list):
                return self.answer(400, {"status": 400, "message": "paths must be a list"})
            for p in paths:
                if not isinstance(p, str) or not p or p.startswith("/") or ".." in p.split("/"):
                    return self.answer(400, {"status": 400, "message": "each path must be inside the workspace"})
        id_ = STORE.new_id("r_" if kind == "request" else "")
        # a share with no paths has nothing to copy and is born ready
        state = "staging" if kind == "share" and paths else "ready"
        STORE.items.append({
            "id": id_, "kind": kind, "owner": "alice", "title": title, "state": state,
            "files": [], "bytes": 0, "skipped": 0,
            "created_at": "2026-09-03T20:00:00Z", "expires_at": "2026-09-17T20:00:00Z",
            "has_password": bool(body.get("password")), "tunnel": False,
        })
        if state == "staging":
            # the hub carries bytes while it stages, and says how far along
            STORE.items[-1]["progress"] = {"copied": 21, "total": 42}
        if kind == "share":
            STORE.pending_paths[id_] = list(paths)
        row = _with_url(STORE.items[-1])
        self.answer(202 if state == "staging" else 201, {"id": id_, "url": row["url"], "state": state})


class Close(_Hub):
    def delete(self, kind_plural, id_):
        before = len(STORE.items)
        STORE.items = [i for i in STORE.items if i["id"] != id_]
        if len(STORE.items) == before:
            return self.answer(404, {"status": 404, "message": "No such share"})
        STORE.uploads.pop(id_, None)
        STORE.nudge()
        self.answer(204)



class Content(_Hub):
    """POST shares/<id>/content - add, remove, rename. An add answers 202 and
    lands later; meanwhile the row carries ``last_add`` running with
    ``progress`` and every content call is refused ``busy``. An added name
    holding ``refuse-add`` settles as refused ``over_cap``. ``exclude`` must
    be a list, as on the hub."""

    def refuse(self, action, reason, code=400):
        self.answer(code, {"reason": reason, "message": f"The {action} was refused: {reason}"})

    def post(self, id_):
        item = next((i for i in STORE.items if i["id"] == id_ and i["kind"] == "share"), None)
        if item is None:
            return self.answer(404, {"status": 404, "message": "No such share"})
        body = self.body()
        action = body.get("action")
        if action not in ("add", "remove", "rename"):
            return self.answer(400, {"status": 400, "message": "action must be one of: add, remove, rename"})
        if id_ in STORE.adding:
            return self.refuse(action, "busy", 409)
        names = [f["name"] for f in item["files"]]
        held = lambda n: [x for x in names if x == n or x.startswith(n + "/")]  # noqa: E731
        if action == "add":
            paths = body.get("paths") or []
            if not isinstance(body.get("exclude", []), list):
                return self.answer(400, {"status": 400, "message": "exclude must be a list"})
            if not paths:
                return self.answer(204)
            added = [posixpath.basename(p.rstrip("/")) for p in paths]
            if any(held(n) for n in added):
                return self.refuse(action, "name_taken")
            STORE.adding.add(id_)
            item["last_add"] = {"state": "running"}
            item["progress"] = {"copied": 21, "total": 42}

            def land():
                STORE.adding.discard(id_)
                if item not in STORE.items:
                    return
                item.pop("progress", None)
                item["last_add"] = {"state": "done", "skipped": 0, "at": "2026-09-03T20:00:01Z"}
                if any("refuse-add" in n for n in added):
                    item["last_add"] = {"state": "refused", "reason": "over_cap", "skipped": 0, "at": "2026-09-03T20:00:01Z"}
                else:
                    item["files"] += _files(added)
                    item["bytes"] = 42 * len(item["files"])
                # the hub rings on a refused add as well as on a landed one
                STORE.nudge()

            tornado.ioloop.IOLoop.current().call_later(STORE.add_seconds, land)
            return self.answer(202)
        name = str(body.get("name") or "")
        if not name or ".." in name.split("/"):
            return self.refuse(action, "bad_filename")
        if not held(name):
            return self.refuse(action, "unknown_entry")
        if action == "remove":
            item["files"] = [f for f in item["files"] if f["name"] not in held(name)]
        else:
            new = str(body.get("new_name") or "")
            if not new or ".." in new.split("/"):
                return self.refuse(action, "bad_filename")
            if held(new):
                return self.refuse(action, "name_taken")
            for f in item["files"]:
                if f["name"] in held(name):
                    f["name"] = new + f["name"][len(name):]
        item["bytes"] = 42 * len(item["files"])
        STORE.nudge()
        self.answer(204)


class Password(_Hub):
    def put(self, kind_plural, id_):
        password = str(self.body().get("password") or "")
        if STORE.capabilities.get("password_required") and not password.strip():
            return self.answer(400, {"reason": "password_required",
                                     "message": "The owner's group requires a password"})
        for item in STORE.items:
            if item["id"] == id_:
                item["has_password"] = bool(password)
                STORE.nudge()
                return self.answer(204)
        self.answer(404, {"status": 404, "message": "No such share"})


class Tunnel(_Hub):
    """The per-record tunnel switch (galaxahub ACC-FILE-2920). The route it
    replaced, ``<id>/cloud``, is not mounted and answers 404."""

    def put(self, kind_plural, id_):
        tunnel = self.body().get("tunnel")
        if not isinstance(tunnel, bool):
            return self.answer(400, {"status": 400, "message": "tunnel must be a boolean"})
        for item in STORE.items:
            if item["id"] == id_:
                if tunnel and not STORE.cloudflare_enabled:
                    return self.answer(403, {"reason": "tunnel_not_available",
                                             "message": "The group policy has Cloudflare turned off"})
                if not tunnel and STORE.tunnel_off_status != 204:
                    return self.answer(STORE.tunnel_off_status, {"status": STORE.tunnel_off_status,
                                                                "message": "switch off failed"})
                item["tunnel"] = tunnel
                if tunnel:
                    STORE.start_tunnel()
                STORE.nudge()
                return self.answer(204)
        self.answer(404, {"status": 404, "message": "No such share"})


class Stream(_Hub):
    """The change stream (galaxahub ACC-FILE-2919): held open, one
    `event: changed` per ring, a keepalive comment every 25s."""

    async def get(self):
        self.set_header("Content-Type", "text/event-stream")
        self.set_header("Cache-Control", "no-cache")
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._queue = queue
        STORE.streams.append(queue)
        try:
            self.write("retry: 5000\n\n")
            await self.flush()
            while True:
                try:
                    if await asyncio.wait_for(queue.get(), 25) is None:
                        break
                    self.write("event: changed\ndata:\n\n")
                except asyncio.TimeoutError:
                    self.write(": keepalive\n\n")
                await self.flush()
        except StreamClosedError:
            pass
        finally:
            if queue in STORE.streams:
                STORE.streams.remove(queue)

    def on_connection_close(self):
        """The lab hung up - end the stream now, as the real hub does."""
        queue = getattr(self, "_queue", None)
        if queue is not None:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)


class Uploads(_Hub):
    def get(self, id_):
        if not any(i["id"] == id_ for i in STORE.items):
            return self.answer(404, {"status": 404, "message": "No such request"})
        self.answer(200, {"uploads": STORE.uploads.get(id_, [])})


class Fetch(_Hub):
    def post(self, id_, upload_id):
        dest = str(self.body().get("dest") or "")
        if not dest or dest.startswith("/") or ".." in dest.split("/"):
            return self.answer(400, {"status": 400, "message": "dest must be a directory inside the workspace"})
        for u in STORE.uploads.get(id_, []):
            if u["upload_id"] == upload_id:
                return self.answer(200, {"path": f"{dest}/{u['filename']}"})
        self.answer(404, {"status": 404, "message": "No such upload"})


class ShareFetch(_Hub):
    """POST shares/<id>/fetch - the hub writes the whole record into the
    owner's workspace on its own mount of the volume. Measured against the
    real hub on 2026-09-23: it creates `dest`, refuses one that exists, and
    ignores an `archive` key.
    """

    def post(self, id_):
        dest = str(self.body().get("dest") or "")
        if not dest or dest.startswith("/") or ".." in dest.split("/"):
            return self.answer(400, {"status": 400, "message": "dest must be a directory inside the workspace"})
        item = next((i for i in STORE.items if i["id"] == id_), None)
        if item is None:
            return self.answer(404, {"status": 404, "message": "No such share"})
        if not _write_files(dest, item["files"]):
            return self.answer(400, {"reason": "bad_path", "message": "dest exists"})
        self.answer(200, {"path": dest})


class _Record(_Hub):
    """``records/<id>``: a record found by its id whoever owns it, behind the
    grant its password earned."""

    def record(self, id_):
        """The record, or ``None`` with the refusal written."""
        item = next((i for i in STORE.items + STORE.foreign if i["id"] == id_), None)
        if item is None:
            self.answer(404, {"status": 404, "message": "No such record"})
            return None
        if item.get("closed"):
            self.answer(410, {"reason": "closed", "message": "The owner closed this record"})
            return None
        grant = self.request.headers.get("X-Fileshare-Grant", "")
        if id_ in STORE.passwords and STORE.grants.get(grant) != id_:
            self.answer(401, {"reason": "password_required", "message": "This record needs its password"})
            return None
        return item


class Record(_Record):
    def get(self, id_):
        item = self.record(id_)
        if item is not None:
            self.answer(200, _with_url(item))


class RecordUnlock(_Hub):
    def post(self, id_):
        if not any(i["id"] == id_ for i in STORE.items + STORE.foreign):
            return self.answer(404, {"status": 404, "message": "No such record"})
        if STORE.passwords.get(id_) != str(self.body().get("password") or ""):
            return self.answer(403, {"reason": "password_wrong", "message": "Wrong password"})
        grant = f"grant-{STORE.new_id()}"
        STORE.grants[grant] = id_
        self.answer(200, {"grant": grant, "expires_at": "2026-09-03T21:00:00Z"})


class RecordFetch(_Record):
    """POST records/<id>/fetch - as ``shares/<id>/fetch``; a ``name`` writes
    that file, or that folder and all under it, at ``<dest>/<name>``."""

    def post(self, id_):
        item = self.record(id_)
        if item is None:
            return
        if item["kind"] != "share":
            return self.answer(400, {"status": 400, "message": "only a share is fetched"})
        body = self.body()
        dest, name = str(body.get("dest") or ""), str(body.get("name") or "")
        if not dest or dest.startswith("/") or ".." in dest.split("/"):
            return self.answer(400, {"status": 400, "message": "dest must be a directory inside the workspace"})
        files = [f for f in item["files"] if not name or f["name"] == name or f["name"].startswith(name + "/")]
        if name and not files:
            return self.answer(404, {"reason": "unknown_entry", "message": f"No entry {name}"})
        if not _write_files(dest, files):
            return self.answer(400, {"reason": "bad_path", "message": "dest exists"})
        self.answer(200, {"path": dest})


class RecordUpload(_Record):
    """POST records/<id>/upload - the caller's workspace paths into a
    request, copied after the 202. While it runs the record carries
    ``last_upload`` running with ``progress``, a quarter at the start and
    three quarters half way, where the hub rings; a path holding
    ``refuse-upload`` settles as refused ``over_cap``."""

    def post(self, id_):
        item = self.record(id_)
        if item is None:
            return
        if item["kind"] != "request":
            return self.answer(400, {"status": 400, "message": "only a request takes uploads"})
        if (item.get("last_upload") or {}).get("state") == "running":
            return self.answer(409, {"reason": "busy", "message": "An upload is already running"})
        body = self.body()
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths or not isinstance(body.get("exclude", []), list):
            return self.answer(400, {"status": 400, "message": "paths must be a non-empty list"})
        for p in paths:
            if not isinstance(p, str) or not p or p.startswith("/") or ".." in p.split("/"):
                return self.answer(400, {"status": 400, "message": "each path must be inside the workspace"})
        item["last_upload"] = {"state": "running"}
        item["progress"] = {"copied": 10, "total": 40}

        def tick():
            if (item.get("last_upload") or {}).get("state") == "running":
                item["progress"] = {"copied": 30, "total": 40}
                STORE.nudge()

        def land():
            item.pop("progress", None)
            if any("refuse-upload" in p for p in paths):
                item["last_upload"] = {"state": "refused", "reason": "over_cap"}
            else:
                item["last_upload"] = {"state": "done", "count": len(paths)}
                STORE.uploads.setdefault(id_, []).extend(
                    {"upload_id": STORE.new_id("u_"), "filename": posixpath.basename(p.rstrip("/")),
                     "size": 42, "sha256": "0" * 64, "uploaded_at": "2026-09-03T21:00:00Z"}
                    for p in paths
                )
            STORE.nudge()

        tornado.ioloop.IOLoop.current().call_later(STORE.add_seconds / 2, tick)
        tornado.ioloop.IOLoop.current().call_later(STORE.add_seconds, land)
        self.answer(202)


class RecipientPage(tornado.web.RequestHandler):
    """The recipient page, unauthenticated as the real one: ``page_status``
    for a record that exists, 404 for any other id."""

    def get(self, id_):
        STORE.calls.append({"method": "GET", "path": self.request.path, "body": "", "auth": ""})
        exists = any(i["id"] == id_ for i in STORE.items)
        self.set_status(STORE.page_status if exists else 404)
        self.finish("<html><body>recipient page</body></html>")


class Control(_Base):
    """The test's side door - never token-gated, never part of the contract."""

    def get(self, action):
        if action == "health":
            return self.answer(200, {"ok": True})
        if action == "calls":
            return self.answer(200, {"calls": STORE.calls})
        if action == "items":
            return self.answer(200, {"items": [_with_url(i) for i in STORE.items]})
        if action == "streams":
            return self.answer(200, {"open": len(STORE.streams)})
        self.answer(404, {"status": 404, "message": "unknown control"})

    def post(self, action):
        body = self.body()
        if action == "reset":
            STORE.reset()
            return self.answer(200, {"ok": True})
        if action == "capabilities":
            STORE.capabilities.update(body)
            for key in [k for k, v in body.items() if v is None]:
                STORE.capabilities.pop(key, None)
            return self.answer(200, STORE.capabilities)
        if action == "upload":
            rid = str(body.get("request_id") or "")
            STORE.uploads.setdefault(rid, []).append({
                "upload_id": str(body.get("upload_id") or "u1"),
                "filename": str(body.get("filename") or "report.csv"),
                "size": int(body.get("size") or 7), "sha256": "0" * 64,
                "uploaded_at": "2026-09-03T21:00:00Z",
            })
            STORE.nudge()
            return self.answer(200, {"ok": True})
        if action == "add":
            STORE.add_seconds = float(body.get("seconds") or ADD_SECONDS)
            return self.answer(200, {"ok": True})
        if action == "files":
            # put files on a share's row without running a transfer
            item = next((i for i in STORE.items if i["id"] == str(body.get("id") or "")), None)
            if item is None:
                return self.answer(404, {"status": 404, "message": "No such share"})
            item["files"] = [
                {"name": str(n), "size": 7, "sha256": "0" * 64} for n in (body.get("names") or [])
            ]
            item["bytes"] = 7 * len(item["files"])
            STORE.nudge()
            return self.answer(200, {"ok": True})
        if action == "foreign":
            # a record another user owns: kind, title, file names, password
            kind = "request" if body.get("kind") == "request" else "share"
            id_ = STORE.new_id("r_" if kind == "request" else "")
            STORE.foreign.append({
                "id": id_, "kind": kind, "owner": str(body.get("owner") or "bob"),
                "title": str(body.get("title") or "Their Record"), "state": "ready",
                "files": [{"name": str(n), "size": 7, "sha256": "0" * 64} for n in (body.get("names") or [])],
                "bytes": 7 * len(body.get("names") or []), "skipped": 0,
                "created_at": "2026-09-03T20:00:00Z", "expires_at": "2026-09-17T20:00:00Z",
                "has_password": bool(body.get("password")), "tunnel": False,
            })
            if body.get("password"):
                STORE.passwords[id_] = str(body["password"])
            return self.answer(200, {"id": id_, "url": _with_url(STORE.foreign[-1])["url"]})
        if action == "password":
            # the owner set or changed a record's password: the grants it
            # minted stop working, as the hub's do
            id_ = str(body.get("id") or "")
            STORE.passwords[id_] = str(body.get("password") or "")
            STORE.grants = {g: r for g, r in STORE.grants.items() if r != id_}
            STORE.nudge()
            return self.answer(200, {"ok": True})
        if action == "close":
            # the owner closed a record: its id answers 410 from now on
            for item in STORE.items + STORE.foreign:
                if item["id"] == str(body.get("id") or ""):
                    item["closed"] = True
                    STORE.nudge()
                    return self.answer(200, {"ok": True})
            return self.answer(404, {"status": 404, "message": "No such record"})
        if action == "nudge":
            STORE.nudge()
            return self.answer(200, {"ok": True})
        if action == "policy":
            # the group policy knobs the fileshare API answers from
            if "cloudflare_enabled" in body:
                STORE.cloudflare_enabled = bool(body["cloudflare_enabled"])
            return self.answer(200, {"cloudflare_enabled": STORE.cloudflare_enabled})
        if action == "tunnel":
            # base, delay (seconds after the first switch-on), registers
            # (ever), ready (the connector's verdict, which rings the stream)
            if "base" in body:
                STORE.tunnel_base = str(body["base"])
            if "delay" in body:
                STORE.tunnel_delay = float(body["delay"])
            if "registers" in body:
                STORE.tunnel_registers = bool(body["registers"])
            if "ready" in body:
                STORE.set_ready(body["ready"])
            return self.answer(200, {"base": STORE.tunnel_base, "delay": STORE.tunnel_delay,
                                     "registers": STORE.tunnel_registers,
                                     "ready": STORE.capabilities["tunnel_ready"]})
        if action == "page":
            # the status the recipient page answers
            STORE.page_status = int(body.get("status") or 200)
            return self.answer(200, {"status": STORE.page_status})
        if action == "cloudoff":
            # the status a Cloudflare switch off answers
            STORE.tunnel_off_status = int(body.get("status") or 204)
            return self.answer(200, {"status": STORE.tunnel_off_status})
        self.answer(404, {"status": 404, "message": "unknown control"})


ID = r"([A-Za-z0-9_-]+)"


def make_app():
    prefix = "/hub/api/fileshare"
    return tornado.web.Application([
        (rf"{prefix}/capabilities", Capabilities),
        (rf"{prefix}/items", Items),
        (rf"{prefix}/(shares|requests)", Create),
        (rf"{prefix}/(shares|requests)/{ID}", Close),
        (rf"{prefix}/shares/{ID}/content", Content),
        (rf"{prefix}/shares/{ID}/fetch", ShareFetch),
        (rf"{prefix}/(shares|requests)/{ID}/password", Password),
        (rf"{prefix}/(shares|requests)/{ID}/tunnel", Tunnel),
        (rf"{prefix}/stream", Stream),
        (rf"{prefix}/requests/{ID}/uploads", Uploads),
        (rf"{prefix}/requests/{ID}/uploads/{ID}/fetch", Fetch),
        (rf"{prefix}/records/{ID}", Record),
        (rf"{prefix}/records/{ID}/unlock", RecordUnlock),
        (rf"{prefix}/records/{ID}/fetch", RecordFetch),
        (rf"{prefix}/records/{ID}/upload", RecordUpload),
        (rf"/s/{POLICY_ID}/{ID}", RecipientPage),
        (r"/_control/([a-z]+)", Control),
    ])


if __name__ == "__main__":
    make_app().listen(PORT, address="127.0.0.1")
    print(f"mock hub listening on {BASE}", flush=True)
    tornado.ioloop.IOLoop.current().start()
