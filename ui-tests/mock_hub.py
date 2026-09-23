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

A record another user owns is never in this user's ``items`` and the hub API
answers 404 for it. It is reached through its link, as a recipient's browser
reaches it (question board topic t01): the page at ``/s/<policy id>/<id>``,
``/unlock`` (a form that sets the ``fs_unlock`` cookie), ``/d/<path>``,
``/archive`` and ``/u`` (one raw file under ``X-Filename``). The hub rings no
stream for another user's change, so a lab learns one by reading the link
again. ``MOCK_HUB_ROOT`` is the lab's root folder; with it, a fetch of this
user's own share writes its files there as the hub does through its own mount
of the volume. A record made with ``tls`` is reached on a second listener
over https, on a port the system picks, which presents the self-signed test
certificate ``peer-a`` until ``/_control/certificate`` loads ``peer-b``.

``/_control/*`` is the test's own side door: reset the store, change the
capabilities, the policy, the tunnel (its base, its delay and its ready
verdict), the recipient page and the status a Cloudflare switch off answers,
add an upload, add or close another user's record, swap the https
certificate, ring the change stream, read the recorded calls.
"""

from __future__ import annotations

import asyncio
import html
import io
import json
import os
import posixpath
import ssl
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

import tornado.httpserver
import tornado.ioloop
import tornado.netutil
import tornado.web
from tornado.iostream import StreamClosedError

TOKEN = os.environ.get("MOCK_HUB_TOKEN", "test-token")
PORT = int(os.environ.get("MOCK_HUB_PORT") or "8765")
BASE = f"http://127.0.0.1:{PORT}"
# the group's policy id, the first segment of every public link
POLICY_ID = "mockpolicy"
# the lab's root folder, which the hub reaches through its own mount
ROOT = os.environ.get("MOCK_HUB_ROOT", "")
# the https listener: the extension's self-signed test certificates, and the
# base its port gives once it listens
CERTIFICATES = Path(__file__).resolve().parent.parent / "jupyterlab_share_files_extension/tests/fixtures"
TLS = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
TLS_BASE = ""


ADD_SECONDS = 0.4


class Store:
    def __init__(self):
        self.reset()

    def reset(self):
        TLS.load_cert_chain(CERTIFICATES / "peer-a.pem")
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
        # a record's password by id, and what each fs_unlock cookie unlocked:
        # (record id, the password it was given), so a changed password
        # retires every cookie of the old one
        self.passwords: dict[str, str] = {}
        self.cookies: dict[str, tuple[str, str]] = {}
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
    base = TLS_BASE if item.get("tls") else STORE.tunnel_base.rstrip("/") if on_tunnel else BASE
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


def _content(name: str) -> bytes:
    """What a file of another user's record holds: its name, so a saved copy
    shows which file it came from."""
    return f"{name}\n".encode()


def _page(item: dict, unlocked: bool) -> str:
    """A record's page in the fileshare app's markup, as measured on the
    live hub on 2026-09-23."""
    root = f"/s/{POLICY_ID}/{item['id']}"
    if not unlocked:
        return (
            '<html><body><div class="panel"><h1>Password required</h1>'
            f'<form method="post" action="{root}/unlock"><input name="password" type="password"></form>'
            "</div></body></html>"
        )
    title = html.escape(item["title"])
    if item["kind"] == "request":
        return (
            f'<html><body><div class="panel"><h1>{title}</h1>'
            f'<p class="sub">{item["owner"]} asked you to upload a file.</p>'
            f"<script>(function(){{var cap={STORE.capabilities['max_upload_bytes']};}})();</script>"
            "</div></body></html>"
        )
    rows = "".join(
        f'<tr><td>{html.escape(f["name"])}</td><td class="size">{f["size"]} B</td>'
        f'<td class="act"><a href="{root}/d/{quote(f["name"], safe="")}">Download</a></td></tr>'
        for f in item["files"]
    )
    return (
        f'<html><body><div class="panel"><h1>{title}</h1><p class="sub">Shared by {item["owner"]}</p>'
        f"<table><tbody>{rows}</tbody></table>"
        f'<p class="row"><a href="{root}/archive">Download all (.zip)</a></p></div></body></html>'
    )


class _Link(tornado.web.RequestHandler):
    """A record's link on the hub's fileshare app: unauthenticated, as the
    real one, and unlocked by the ``fs_unlock`` cookie."""

    def prepare(self):
        STORE.calls.append({
            "method": self.request.method, "path": self.request.path, "body": "",
            "auth": self.request.headers.get("Authorization", ""),
            "filename": unquote(self.request.headers.get("X-Filename", "")),
        })

    def foreign(self, id_):
        item = next((i for i in STORE.foreign if i["id"] == id_), None)
        return item if item and not item.get("closed") else None

    def unlocked(self, id_) -> bool:
        return id_ not in STORE.passwords or STORE.cookies.get(self.get_cookie("fs_unlock") or "") == (
            id_, STORE.passwords[id_])

    def not_found(self):
        self.set_status(404)
        self.finish("<html><body><h1>Not found</h1></body></html>")

    def refuse(self, code: int, slug: str):
        self.set_status(code)
        self.finish({"error": slug, "message": slug})


class RecipientPage(_Link):
    """The record's page. This user's own record answers ``page_status``
    (what the lab's link check opens), another user's its page."""

    def get(self, id_):
        item = self.foreign(id_)
        if item is not None:
            return self.finish(_page(item, self.unlocked(id_)))
        exists = any(i["id"] == id_ for i in STORE.items)
        self.set_status(STORE.page_status if exists else 404)
        self.finish("<html><body>recipient page</body></html>")


class LinkUnlock(_Link):
    def post(self, id_):
        if self.foreign(id_) is None:
            return self.not_found()
        if STORE.passwords.get(id_, "") != self.get_body_argument("password", ""):
            self.set_status(403)
            return self.finish("<html><body>That password did not match.</body></html>")
        cookie = f"cookie-{STORE.new_id()}"
        STORE.cookies[cookie] = (id_, STORE.passwords[id_])
        self.set_cookie("fs_unlock", cookie, path=f"/s/{POLICY_ID}/{id_}", httponly=True)
        self.redirect(f"/s/{POLICY_ID}/{id_}", status=303)


class LinkDownload(_Link):
    def get(self, id_, name):
        item = self.foreign(id_)
        if item is None or item["kind"] != "share":
            return self.not_found()
        if not self.unlocked(id_):
            self.set_status(403)
            return self.finish("<html><body>Password required</body></html>")
        if not any(f["name"] == name for f in item["files"]):
            return self.not_found()
        self.set_header("Content-Type", "application/octet-stream")
        self.finish(_content(name))


class LinkArchive(_Link):
    def get(self, id_):
        item = self.foreign(id_)
        if item is None or item["kind"] != "share" or not self.unlocked(id_):
            return self.not_found()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for f in item["files"]:
                zf.writestr(f["name"], _content(f["name"]))
        self.set_header("Content-Type", "application/zip")
        self.finish(buf.getvalue())


class LinkUpload(_Link):
    """One file per request, the raw body under ``X-Filename``; it lands
    after ``add_seconds``, and a name holding ``refuse-upload`` is over the
    request's limit."""

    async def post(self, id_):
        item = self.foreign(id_)
        if item is None or item["kind"] != "request":
            return self.not_found()
        if not self.unlocked(id_):
            return self.refuse(403, "password_required")
        name = unquote(self.request.headers.get("X-Filename", ""))
        if not name or "/" in name:
            return self.refuse(400, "bad_filename")
        if "refuse-upload" in name:
            return self.refuse(413, "over_cap")
        await asyncio.sleep(STORE.add_seconds)
        STORE.uploads.setdefault(id_, []).append({
            "upload_id": STORE.new_id("u_"), "filename": name, "size": len(self.request.body),
            "sha256": "0" * 64, "uploaded_at": "2026-09-03T21:00:00Z",
        })
        self.finish({"ok": True, "filename": name, "size": len(self.request.body)})


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
                "files": [{"name": str(n), "size": len(_content(str(n))), "sha256": "0" * 64}
                          for n in (body.get("names") or [])],
                "skipped": 0,
                "created_at": "2026-09-03T20:00:00Z", "expires_at": "2026-09-17T20:00:00Z",
                "has_password": bool(body.get("password")), "tunnel": False,
                "tls": bool(body.get("tls")),
            })
            if body.get("password"):
                STORE.passwords[id_] = str(body["password"])
            return self.answer(200, {"id": id_, "url": _with_url(STORE.foreign[-1])["url"]})
        if action == "certificate":
            # the https listener presents another certificate from now on
            name = "peer-b" if body.get("name") == "peer-b" else "peer-a"
            TLS.load_cert_chain(CERTIFICATES / f"{name}.pem")
            return self.answer(200, {"ok": True})
        if action == "password":
            # another user set or changed their record's password: the
            # cookies of the old one stop working, and no stream rings
            STORE.passwords[str(body.get("id") or "")] = str(body.get("password") or "")
            return self.answer(200, {"ok": True})
        if action == "close":
            # another user closed their record: its link answers 404 from
            # now on, and no stream rings
            for item in STORE.foreign:
                if item["id"] == str(body.get("id") or ""):
                    item["closed"] = True
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
        (rf"/s/{POLICY_ID}/{ID}", RecipientPage),
        (rf"/s/{POLICY_ID}/{ID}/unlock", LinkUnlock),
        (rf"/s/{POLICY_ID}/{ID}/d/(.+)", LinkDownload),
        (rf"/s/{POLICY_ID}/{ID}/archive", LinkArchive),
        (rf"/s/{POLICY_ID}/{ID}/u", LinkUpload),
        (r"/_control/([a-z]+)", Control),
    ])


if __name__ == "__main__":
    app = make_app()
    app.listen(PORT, address="127.0.0.1")
    socks = tornado.netutil.bind_sockets(0, "127.0.0.1")
    tornado.httpserver.HTTPServer(app, ssl_options=TLS).add_sockets(socks)
    TLS_BASE = f"https://127.0.0.1:{socks[0].getsockname()[1]}"
    print(f"mock hub listening on {BASE} and {TLS_BASE}", flush=True)
    tornado.ioloop.IOLoop.current().start()
