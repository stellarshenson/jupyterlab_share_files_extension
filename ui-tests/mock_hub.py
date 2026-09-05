"""A stand-in for galaxahub's fileshare API, for the hub-mode galata suite.

Speaks the routes the extension calls (`/hub/api/fileshare/*`) for one user,
in memory, and refuses any call whose token is not the one the test server is
spawned with - the same contract a real hub holds. Nothing here reaches a
network beyond the loopback port it listens on.

Share state is decided by the title so a test can ask for a row in any state:
a title containing ``stay-staging`` never leaves ``staging``, one containing
``refuse`` is refused with ``over_cap``, any other share is promoted to
``ready`` on the next listing with one file per submitted path.

``/_control/*`` is the test's own side door: reset the store, change the
capabilities, add an upload, ring the change stream, read the recorded calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
import re

import tornado.ioloop
import tornado.web
from tornado.iostream import StreamClosedError

TOKEN = os.environ.get("MOCK_HUB_TOKEN", "test-token")
PORT = int(os.environ.get("MOCK_HUB_PORT") or "8765")
BASE = f"http://127.0.0.1:{PORT}"


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
        }
        # the group policy's Cloudflare switch - a record may be switched on
        # only while it is on (the real hub's `file_sharing_cloudflare_enabled`)
        self.cloudflare_enabled = True
        # an older hub without the stream route answers 404
        self.stream_supported = True
        self.streams: list[asyncio.Queue] = []
        self.items: list[dict] = []
        self.pending_paths: dict[str, list[str]] = {}
        self.uploads: dict[str, list[dict]] = {}
        self.counter = 0

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
                continue
            paths = self.pending_paths.pop(item["id"], [])
            item["files"] = [
                {"name": posixpath.basename(p.rstrip("/")) or p, "size": 42, "sha256": "0" * 64}
                for p in paths
            ]
            item["bytes"] = 42 * len(item["files"])
            item["state"] = "ready"
            self.nudge()


STORE = Store()


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
    stored - the hub's own address while the record's cloud switch is off,
    the origin the policy prefers (its tunnel, when it has one) while on."""
    base = STORE.capabilities["public_base_url"].rstrip("/") if item.get("cloud") else BASE
    return {**item, "url": f"{base}/s/{item['id']}"}


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
            if not isinstance(paths, list) or not paths:
                return self.answer(400, {"status": 400, "message": "paths must be a non-empty list"})
            for p in paths:
                if not isinstance(p, str) or not p or p.startswith("/") or ".." in p.split("/"):
                    return self.answer(400, {"status": 400, "message": "each path must be inside the workspace"})
        id_ = STORE.new_id("r_" if kind == "request" else "")
        state = "staging" if kind == "share" else "ready"
        STORE.items.append({
            "id": id_, "kind": kind, "owner": "alice", "title": title, "state": state,
            "files": [], "bytes": 0, "skipped": 0,
            "created_at": "2026-09-03T20:00:00Z", "expires_at": "2026-09-17T20:00:00Z",
            "has_password": bool(body.get("password")), "cloud": False,
        })
        if kind == "share":
            STORE.pending_paths[id_] = list(paths)
        row = _with_url(STORE.items[-1])
        self.answer(202 if kind == "share" else 201, {"id": id_, "url": row["url"], "state": state})


class Close(_Hub):
    def delete(self, kind_plural, id_):
        before = len(STORE.items)
        STORE.items = [i for i in STORE.items if i["id"] != id_]
        if len(STORE.items) == before:
            return self.answer(404, {"status": 404, "message": "No such share"})
        STORE.uploads.pop(id_, None)
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


class Cloud(_Hub):
    """The per-record Cloudflare switch (galaxahub ACC-FILE-2920)."""

    def put(self, kind_plural, id_):
        cloud = self.body().get("cloud")
        if not isinstance(cloud, bool):
            return self.answer(400, {"status": 400, "message": "cloud must be a boolean"})
        for item in STORE.items:
            if item["id"] == id_:
                if cloud and not STORE.cloudflare_enabled:
                    return self.answer(403, {"reason": "cloud_not_configured",
                                             "message": "The group policy has Cloudflare turned off"})
                item["cloud"] = cloud
                STORE.nudge()
                return self.answer(204)
        self.answer(404, {"status": 404, "message": "No such share"})


class Stream(_Hub):
    """The change stream (galaxahub ACC-FILE-2919): held open, one
    `event: changed` per ring, a keepalive comment every 25s."""

    async def get(self):
        if not STORE.stream_supported:
            return self.answer(404, {"status": 404, "message": "Not Found"})
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
        if action == "nudge":
            STORE.nudge()
            return self.answer(200, {"ok": True})
        if action == "policy":
            # the group policy knobs the fileshare API answers from
            if "cloudflare_enabled" in body:
                STORE.cloudflare_enabled = bool(body["cloudflare_enabled"])
            if "stream_supported" in body:
                STORE.stream_supported = bool(body["stream_supported"])
            return self.answer(200, {"cloudflare_enabled": STORE.cloudflare_enabled,
                                     "stream_supported": STORE.stream_supported})
        self.answer(404, {"status": 404, "message": "unknown control"})


ID = r"([A-Za-z0-9_-]+)"


def make_app():
    prefix = "/hub/api/fileshare"
    return tornado.web.Application([
        (rf"{prefix}/capabilities", Capabilities),
        (rf"{prefix}/items", Items),
        (rf"{prefix}/(shares|requests)", Create),
        (rf"{prefix}/(shares|requests)/{ID}", Close),
        (rf"{prefix}/(shares|requests)/{ID}/password", Password),
        (rf"{prefix}/(shares|requests)/{ID}/cloud", Cloud),
        (rf"{prefix}/stream", Stream),
        (rf"{prefix}/requests/{ID}/uploads", Uploads),
        (rf"{prefix}/requests/{ID}/uploads/{ID}/fetch", Fetch),
        (r"/_control/([a-z]+)", Control),
    ])


if __name__ == "__main__":
    make_app().listen(PORT, address="127.0.0.1")
    print(f"mock hub listening on {BASE}", flush=True)
    tornado.ioloop.IOLoop.current().start()
