"""Hub-mode handlers: the panel's ``api/*`` surface backed by the hub API.

Registered instead of the standalone table when :func:`hub.hub_mode` is true.
Every handler here is an ``APIHandler`` guarded by ``tornado.web.authenticated``;
the module never defines a public or static route. The panel keeps calling
the same ``api/*`` paths with the same response shapes - only the backend
changes: paths are sent to the hub, which copies the bytes with its own
transfer job, and recipients are served by the hub's fileshare app.

What has no hub equivalent is simply not mounted: adding files to an existing
share (a share is a snapshot), removing a single upload, peer connections,
and the per-user Cloudflare tunnel. The cloud toggle survives with a new
meaning - every hub record carries its own Cloudflare switch, and the toggle
flips them all and sets the default for the next one.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tornado.web
from jupyter_server.utils import url_path_join
from tornado.iostream import StreamClosedError

from .hub import HubClient, HubUnavailable, hub_api_origin
from .hub_stream import CHANGED, CLOSE, KEEPALIVE_SECONDS, RELAY, RETRY_SECONDS
from .routes import (
    GeneratePasswordHandler,
    _Base,
    _request_origin,
    probe_link,
)
from .storage import _is_safe_relative, _resolve_unique_target, _safe_name
from .tunnel import _load_config, _save_config

# galaxahub share ids are url-safe base64; requests carry an `r_` prefix.
# Upload ids are shorter opaque tokens; both are matched loosely here and
# validated by the hub, which owns the id space.
HUB_ID = r"([A-Za-z0-9_-]{6,64})"
UPLOAD_ID = r"([A-Za-z0-9_.-]{1,128})"
_HUB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")

# Passwords set through THIS server process, by id. The hub stores only a
# hash and never returns the value; the link dialog shows what the owner
# typed here. Lost on restart - the dialog then shows "set" without a value.
_PASSWORDS: dict[str, str] = {}

# Config-file key for the cloud toggle in hub mode: True switches every new
# share and request on to the hub's Cloudflare front, False leaves it on the
# hub's own address (reachable on the hub's network only). The hub mints
# every record with the switch off; the lab applies the preference after.
CLOUD_KEY = "hub_cloud"

# The wait for the hub to confirm a Cloudflare switch-on (CloudWait): the
# hub's items are read at most every CONFIRM_POLL_SECONDS, for at most
# CONFIRM_TIMEOUT_SECONDS. CONFIRM_TIMEOUT_VAR overrides the bound - the
# hub-mode galata suite sets it to test the timeout.
CONFIRM_POLL_SECONDS = 5
CONFIRM_TIMEOUT_VAR = "SHARE_FILES_CLOUD_CONFIRM_SECONDS"
CONFIRM_TIMEOUT_SECONDS = float(os.environ.get(CONFIRM_TIMEOUT_VAR) or 120)
# the reason api/info and api/tunnel carry after the wait switched an
# unconfirmed switch-on back off
UNCONFIRMED_REASON = "cloud_not_confirmed"
# the reasons for a Cloudflare switch the hub answered with an error: the
# wait's switch back off (the record stays on) and a create's switch on
# (the record stays off); a hub that does not answer is hub_unavailable
NOT_OFF_REASON = "cloud_not_switched_off"
NOT_ON_REASON = "cloud_not_switched_on"


# --------------------------------------------------------------------------- #
# Pure translation (unit-tested without a server)
# --------------------------------------------------------------------------- #


def _ts(value: Any) -> int:
    """ISO-8601 UTC stamp -> unix seconds; 0 for anything else."""
    if not isinstance(value, str) or not value:
        return 0
    try:
        stamp = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_dt.timezone.utc)
    return int(stamp.timestamp())


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def share_from_item(item: dict, link: str) -> dict:
    """One hub ``items`` row of kind share -> the panel's share shape."""
    files = item.get("files") or []
    return {
        "id": item.get("id", ""),
        "name": item.get("title") or item.get("id", ""),
        "slug": item.get("id", ""),
        "kind": "share",
        "created_at": _ts(item.get("created_at")),
        "entries": [
            {"name": f.get("name", ""), "type": "file", "size": int(f.get("size") or 0)}
            for f in files
        ],
        "link": link,
        "has_password": bool(item.get("has_password")),
        "cloud": bool(item.get("cloud")),
        "state": item.get("state") or "ready",
        "reason": item.get("reason") or "",
        "expires_at": _ts(item.get("expires_at")),
        "bytes": int(item.get("bytes") or 0),
        "skipped": int(item.get("skipped") or 0),
    }


def request_from_item(
    item: dict, uploads: list[dict], link: str, last_seen: int = 0
) -> dict:
    """One hub ``items`` row of kind request plus its uploads -> the panel's
    request shape. The hub does not group uploads by uploader, so every
    upload sits in one group; ``upload_id`` rides on the entry for fetch."""
    entries = []
    for u in uploads or []:
        entries.append({
            "name": u.get("filename", ""),
            "type": "file",
            "size": int(u.get("size") or 0),
            "upload_id": u.get("upload_id", ""),
            "mtime": _ts(u.get("uploaded_at")),
        })
    last_upload = max((e["mtime"] for e in entries), default=0)
    return {
        "id": item.get("id", ""),
        "name": item.get("title") or item.get("id", ""),
        "slug": item.get("id", ""),
        "kind": "request",
        "created_at": _ts(item.get("created_at")),
        "upload_count": len(entries),
        "last_upload_at": last_upload,
        "last_seen_upload_at": last_seen,
        "uploaders": (
            [{"hash": "Uploads", "name": "Uploads", "entries": entries}] if entries else []
        ),
        "link": link,
        "has_password": bool(item.get("has_password")),
        "cloud": bool(item.get("cloud")),
        "state": item.get("state") or "ready",
        "reason": item.get("reason") or "",
        "expires_at": _ts(item.get("expires_at")),
    }


def relay_error(code: int, data: Any) -> tuple[int, dict]:
    """A hub error answer -> the status and body the lab relays.

    The hub refuses with ``{reason, message}`` and errors with
    ``{status, message}``; the panel reads ``error`` and, when present,
    ``reason`` (the closed slug set it translates).
    """
    data = data if isinstance(data, dict) else {}
    message = data.get("message") or data.get("error") or f"the hub answered {code}"
    body = {"error": message}
    if data.get("reason"):
        body["reason"] = data["reason"]
    return code, body


def cloud_default() -> bool:
    """The cloud toggle: switch every new share and request on to Cloudflare."""
    return bool(_load_config().get(CLOUD_KEY, False))


def set_cloud_default(value: bool) -> None:
    cfg = _load_config()
    cfg[CLOUD_KEY] = bool(value)
    _save_config(cfg)


def rewrite_link(url: str, browser_origin: str) -> str:
    """The link the panel hands out for a hub ``url``.

    The hub composes a record's url from the Host header of the request it
    answers, so a lab-originated call yields the hub's internal address; it
    is replaced by the origin the browser reaches the lab on. Any other
    origin is the Cloudflare hostname the hub chose for a record with its
    cloud switch on, and is kept as the hub composed it.
    """
    parsed = urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return url or ""
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin != hub_api_origin():
        return url
    return browser_origin + parsed.path + (f"?{parsed.query}" if parsed.query else "")


def on_tunnel(url: str) -> bool:
    """True when a hub ``url`` carries an origin other than the hub's own -
    the tunnel hostname, the only sign that Cloudflare serves the record.
    ``capabilities.public_base_url`` is always the hub's own address and
    confirms nothing."""
    parsed = urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return False
    return f"{parsed.scheme}://{parsed.netloc}" != hub_api_origin()


def plural(kind: str) -> str:
    return "shares" if kind == "share" else "requests"


# --------------------------------------------------------------------------- #
# The Cloudflare confirmation wait
# --------------------------------------------------------------------------- #


class CloudWait:
    """The lab's one wait for the hub to confirm a Cloudflare switch-on.

    A cloud-on record's url carries the tunnel hostname only once the hub's
    tunnel is registered, and the hub rings its change stream when a record
    changes, not when the tunnel registers. So after a switch-on the lab
    reads the hub's items itself until every record switched on during the
    wait carries a url off the hub's origin, then rings every open panel
    once so each fetches the tunnel links. Unconfirmed at the bound, it
    switches those records and the default back off, keeps ``reason`` for
    the panel, and rings; a switch back off that fails leaves the default as
    it is, with reason ``hub_unavailable`` when the hub did not answer and
    ``cloud_not_switched_off`` when it answered other than 204 or 404. A
    record switched off or removed during the wait needs no confirmation.
    """

    def __init__(self):
        self._records: dict[str, str] = {}  # id -> kind, still unconfirmed
        self._task: asyncio.Task | None = None
        self.reason = ""

    @property
    def waiting(self) -> bool:
        return self._task is not None

    def start(self, records: dict[str, str], items: list[dict]) -> None:
        """Records just switched on (id -> kind), with the hub's items read
        after the switch: a record whose url already carries the tunnel
        hostname needs nothing; the rest start the wait, or join the one
        running. Every switch-on clears the previous timeout reason."""
        self.reason = ""
        confirmed = {i.get("id") for i in items if on_tunnel(i.get("url", ""))}
        self._records.update({k: v for k, v in records.items() if k not in confirmed})
        if self._records and self._task is None:
            self._task = asyncio.ensure_future(self._run())

    def drop(self, ids) -> None:
        """Records switched off: nothing to confirm for them; the last one
        ends the wait."""
        for id_ in ids:
            self._records.pop(id_, None)
        if not self._records and self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONFIRM_TIMEOUT_SECONDS
        try:
            while self._records:
                await asyncio.sleep(max(0.0, min(CONFIRM_POLL_SECONDS, deadline - loop.time())))
                self._settle(await self._items())
                if self._records and loop.time() >= deadline:
                    await self._switch_back()
        finally:
            if self._task is asyncio.current_task():
                self._task = None
        RELAY.ring(CHANGED)

    async def _items(self) -> list[dict] | None:
        try:
            code, data = await HubClient().request("GET", "items")
        except HubUnavailable:
            return None
        return (data.get("items") or []) if code == 200 and isinstance(data, dict) else None

    def _settle(self, items: list[dict] | None) -> None:
        if items is None:
            return
        rows = {i.get("id"): i for i in items}
        for id_ in list(self._records):
            row = rows.get(id_)
            if row is None or not row.get("cloud") or on_tunnel(row.get("url", "")):
                del self._records[id_]

    async def _switch_back(self) -> None:
        unanswered = refused = False
        while self._records:
            id_, kind = self._records.popitem()
            try:
                code, _ = await HubClient().request("PUT", f"{plural(kind)}/{id_}/cloud", {"cloud": False})
            except HubUnavailable:
                unanswered = True
            else:
                refused = refused or code not in (204, 404)
        # a record the hub kept on stays on - the default must not claim off
        if unanswered or refused:
            self.reason = HubUnavailable.reason if unanswered else NOT_OFF_REASON
            return
        set_cloud_default(False)
        self.reason = UNCONFIRMED_REASON


CLOUD_WAIT = CloudWait()


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


class _HubBase(_Base):
    """Hub client access, error relay and link rewriting shared by every route."""

    def _refuse(self, status: int, message: str, reason: str = "") -> None:
        body: dict[str, Any] = {"error": message}
        if reason:
            body["reason"] = reason
        self.set_status(status)
        self.finish(body)

    async def _hub(self, method: str, path: str, body: dict | None = None):
        """One hub call. Returns ``(status, data)``; a hub that cannot be
        reached answers the panel 502 and returns ``None``."""
        try:
            return await HubClient().request(method, path, body)
        except HubUnavailable as exc:
            self._refuse(502, str(exc), HubUnavailable.reason)
            return None

    def _relay(self, code: int, data: Any) -> None:
        status, body = relay_error(code, data)
        self.set_status(status)
        self.finish(body)

    async def _hub_quiet(self, method: str, path: str, body: dict | None = None):
        """One hub call after the main work is done: unavailability is
        reported as ``(0, {})`` instead of a 502 over an answer already earned."""
        try:
            return await HubClient().request(method, path, body)
        except HubUnavailable:
            return 0, {}

    def _link(self, url: str) -> str:
        return rewrite_link(url, _request_origin(self))

    async def _capabilities(self) -> dict | None:
        answer = await self._hub("GET", "capabilities")
        if answer is None:
            return None
        code, data = answer
        if code != 200:
            self._relay(code, data)
            return None
        return data if isinstance(data, dict) else {}

    async def _items(self, kind: str) -> list[dict] | None:
        answer = await self._hub("GET", "items")
        if answer is None:
            return None
        code, data = answer
        if code != 200:
            self._relay(code, data)
            return None
        items = data.get("items") if isinstance(data, dict) else None
        return [i for i in (items or []) if i.get("kind") == kind]

    async def _uploads(self, request_id: str) -> list[dict] | None:
        """None means the 502 was already written; a non-200 answer for one
        row (deleted between the items and uploads calls) is an empty list."""
        answer = await self._hub("GET", f"requests/{request_id}/uploads")
        if answer is None:
            return None
        code, data = answer
        if code != 200 or not isinstance(data, dict):
            return []
        return list(data.get("uploads") or [])

    def _tunnel_state(self, capabilities: dict) -> dict:
        """The cloud toggle as the panel reads it. The hub decides whether a
        record may be switched on (its group policy), so the toggle is always
        offered while the hub answers; a refused switch names its reason.
        ``tunnel_waiting`` is true while a switch-on waits for the hub's
        confirmation, ``tunnel_reason`` names why the last switch-on ended
        unconfirmed ('' otherwise)."""
        return {
            "tunnel_configured": True,
            "tunnel_active": cloud_default(),
            "tunnel_autostart": False,
            "tunnel_running": bool(capabilities.get("serving")),
            "tunnel_waiting": CLOUD_WAIT.waiting,
            "tunnel_reason": CLOUD_WAIT.reason,
        }

    async def _set_cloud(self, kind: str, id_: str, cloud: bool) -> tuple[int, Any]:
        return await self._hub_quiet("PUT", f"{plural(kind)}/{id_}/cloud", {"cloud": cloud})

    async def _items_quiet(self) -> list[dict]:
        """The hub's items after a switch, or [] when the read fails."""
        code, data = await self._hub_quiet("GET", "items")
        return (data.get("items") or []) if code == 200 and isinstance(data, dict) else []

    async def _apply_cloud_default(self, kind: str, row: dict) -> dict:
        """A record the hub just minted with its switch off: switch it on when
        the toggle says so. A refusal turns the toggle off and rides on the
        row as ``cloud_reason`` so the panel can say why the link stayed on
        the hub's network; the switched-on row's url is read back from the
        hub, which composes it, and starts the wait while it is the hub's own."""
        if not cloud_default():
            return row
        code, data = await self._set_cloud(kind, row["id"], True)
        if code == 204:
            items = await self._items_quiet()
            CLOUD_WAIT.start({row["id"]: kind}, items)
            for item in items:
                if item.get("id") == row["id"]:
                    return {**row, "cloud": True, "link": self._link(item.get("url", ""))}
            return {**row, "cloud": True}
        reason = data.get("reason") if isinstance(data, dict) else ""
        if reason == "cloud_not_configured":
            set_cloud_default(False)
        # code 0: the hub did not answer
        return {**row, "cloud_reason": reason or (NOT_ON_REASON if code else HubUnavailable.reason)}


class HubInfoHandler(_HubBase):
    """api/info - mode, link state and the hub's capabilities for this user."""

    @tornado.web.authenticated
    async def get(self):
        info: dict[str, Any] = {
            "mode": "hub",
            "storage_path": "",
            "shares_subdir": "",
            "requests_subdir": "",
            "public_base_url": "",
            "tunnel_configured": False,
            "tunnel_active": cloud_default(),
            "tunnel_autostart": False,
            "tunnel_running": False,
            "tunnel_waiting": CLOUD_WAIT.waiting,
            "tunnel_reason": CLOUD_WAIT.reason,
        }
        try:
            code, data = await HubClient().request("GET", "capabilities")
        except HubUnavailable as exc:
            info["hub"] = {
                "available": False,
                "reason": HubUnavailable.reason,
                "message": str(exc),
            }
            return self.write_json(info)
        if code != 200:
            _status, body = relay_error(code, data)
            info["hub"] = {
                "available": False,
                "reason": body.get("reason") or f"http_{code}",
                "message": body["error"],
            }
            return self.write_json(info)
        caps = data if isinstance(data, dict) else {}
        info.update(self._tunnel_state(caps))
        info["hub"] = {
            "available": True,
            "allow_share": bool(caps.get("allow_share")),
            "allow_request": bool(caps.get("allow_request")),
            "reason": caps.get("reason") or "",
            "serving": bool(caps.get("serving")),
            "password_required": bool(caps.get("password_required")),
            "max_share_bytes": caps.get("max_share_bytes"),
            "max_upload_bytes": caps.get("max_upload_bytes"),
            "max_shares": caps.get("max_shares"),
            "retention_days": caps.get("retention_days"),
        }
        self.write_json(info)


class HubTunnelHandler(_HubBase):
    """api/tunnel - the cloud toggle. No daemon: it flips the Cloudflare
    switch on every share and request this user has and sets the default
    for the next one. The hub refuses a switch on while the group policy has
    Cloudflare off; that refusal is relayed and the toggle stays off unless
    the switch on already switched a record. A switch on starts the wait for
    the hub's confirmation; a switch off ends it for the records it switched."""

    @tornado.web.authenticated
    async def get(self):
        caps = await self._capabilities()
        if caps is None:
            return
        self.write_json(self._tunnel_state(caps))

    @tornado.web.authenticated
    async def post(self):
        body = self.get_json_body() or {}
        caps = await self._capabilities()
        if caps is None:
            return
        if "active" in body:
            active = bool(body["active"])
            answer = await self._hub("GET", "items")
            if answer is None:
                return
            code, data = answer
            if code != 200:
                return self._relay(code, data)
            items = (data.get("items") if isinstance(data, dict) else None) or []
            switched: dict[str, str] = {}
            for item in items:
                if bool(item.get("cloud")) == active:
                    continue
                answer = await self._hub("PUT", f"{plural(item.get('kind', ''))}/{item.get('id', '')}/cloud", {"cloud": active})
                # 204 switched, 404 the record is already gone - the same
                # acceptance CloudWait._switch_back uses for this call
                if answer is None or answer[0] not in (204, 404):
                    # a switch on that fails partway: the records it switched
                    # wait for the confirmation like any switch on, so none is
                    # left on while the header reads off
                    if active and switched:
                        set_cloud_default(True)
                        CLOUD_WAIT.start(switched, await self._items_quiet())
                    return None if answer is None else self._relay(*answer)
                switched[item.get("id", "")] = item.get("kind", "")
            set_cloud_default(active)
            if active:
                # a record already on whose url is still the hub's own - a
                # switch back off that failed - waits with the ones switched
                on = {i.get("id", ""): i.get("kind", "") for i in items if i.get("cloud")}
                CLOUD_WAIT.start({**on, **switched}, await self._items_quiet() if switched else items)
            else:
                CLOUD_WAIT.drop(switched)
        self.write_json(self._tunnel_state(caps))


class HubCloudHandler(_HubBase):
    """api/<shares|requests>/<id>/cloud - one record's Cloudflare switch."""

    @tornado.web.authenticated
    async def post(self, kind, id_):
        body = self.get_json_body() or {}
        cloud = body.get("cloud")
        if not isinstance(cloud, bool):
            return self.write_error_json(400, "'cloud' must be a boolean")
        answer = await self._hub("PUT", f"{kind}/{id_}/cloud", {"cloud": cloud})
        if answer is None:
            return
        code, data = answer
        if code != 204:
            return self._relay(code, data)
        if cloud:
            CLOUD_WAIT.start({id_: "share" if kind == "shares" else "request"}, await self._items_quiet())
        else:
            CLOUD_WAIT.drop([id_])
        self.write_json({"id": id_, "cloud": cloud})


class HubStreamHandler(_HubBase):
    """api/stream - the panel's change stream, Server-Sent Events held open.

    One ``changed`` event per ring of the hub's own stream (the lab holds one
    hub stream for all its panels, ``hub_stream.RELAY``), a keepalive comment
    while idle, and one ``poll`` event when the hub has no stream route, in
    which case the panel falls back to its timer. No payload: the panel
    fetches its lists once per event and once per open.
    """

    _queue: asyncio.Queue | None = None

    @tornado.web.authenticated
    async def get(self):
        self.set_header("Content-Type", "text/event-stream")
        self.set_header("Cache-Control", "no-cache")
        self.set_header("X-Accel-Buffering", "no")
        self._queue = RELAY.subscribe()
        try:
            self.write(f"retry: {RETRY_SECONDS * 1000}\n\n")
            await self.flush()
            while True:
                try:
                    event = await asyncio.wait_for(self._queue.get(), KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    self.write(": keepalive\n\n")
                else:
                    if event is CLOSE:
                        break
                    self.write(f"event: {event}\ndata:\n\n")
                await self.flush()
        except StreamClosedError:
            pass
        finally:
            RELAY.unsubscribe(self._queue)

    def on_connection_close(self):
        if self._queue is None:
            return
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(CLOSE)


class HubLinkCheckHandler(_HubBase):
    """api/link-check - the lab server opens the record's link itself, the
    way the standalone check does, at the url the hub composed: the hub's
    own address on its internal origin, or the tunnel hostname through
    Cloudflare. ``capabilities.serving`` is a cached deployment-wide verdict
    and says nothing about one link.

    A GET of the hub's recipient page renders it and changes nothing - no
    view count, no event, no download, no password attempt (galaxahub
    fileshare ``app.share_page``); it charges one token of the page's
    lookup rate limit, as any recipient's GET does."""

    @tornado.web.authenticated
    async def get(self):
        kind = self.get_query_argument("kind", "")
        id_ = self.get_query_argument("id", "")
        if kind not in ("share", "request"):
            return self.write_error_json(400, "kind must be 'share' or 'request'")
        if not _HUB_ID_RE.match(id_):
            return self.write_error_json(400, "invalid id")
        items = await self._items(kind)
        if items is None:
            return
        for item in items:
            if item.get("id") == id_:
                return self.write_json(await probe_link(item.get("url", "")))
        self.write_error_json(404, "not found")


class HubSharesListHandler(_HubBase):
    @tornado.web.authenticated
    async def get(self):
        items = await self._items("share")
        if items is None:
            return
        self.write_json({
            "shares": [share_from_item(i, self._link(i.get("url", ""))) for i in items]
        })

    @tornado.web.authenticated
    async def post(self):
        body = self.get_json_body() or {}
        name = (body.get("name") or "").strip()
        paths = body.get("paths") or []
        password = str(body.get("password") or "")
        if not name:
            return self.write_error_json(400, "Missing 'name'")
        if not isinstance(paths, list) or not paths:
            return self.write_error_json(400, "'paths' must be a non-empty list")
        for rel in paths:
            if not isinstance(rel, str) or not _is_safe_relative(rel):
                return self.write_error_json(400, f"Unsafe path: {rel}")
        answer = await self._hub(
            "POST", "shares", {"title": name, "paths": paths, "password": password}
        )
        if answer is None:
            return
        code, data = answer
        if code not in (201, 202):
            return self._relay(code, data)
        if password:
            _PASSWORDS[data.get("id", "")] = password
        item = {
            "id": data.get("id", ""),
            "title": name,
            "state": data.get("state") or "staging",
            "url": data.get("url", ""),
            "files": [],
            "has_password": bool(password),
            "created_at": _now_iso(),
        }
        row = share_from_item(item, self._link(item["url"]))
        self.write_json(await self._apply_cloud_default("share", row))


class HubShareItemHandler(_HubBase):
    @tornado.web.authenticated
    async def get(self, id_):
        items = await self._items("share")
        if items is None:
            return
        for item in items:
            if item.get("id") == id_:
                return self.write_json(share_from_item(item, self._link(item.get("url", ""))))
        self.write_error_json(404, "not found")

    @tornado.web.authenticated
    async def delete(self, id_):
        answer = await self._hub("DELETE", f"shares/{id_}")
        if answer is None:
            return
        code, data = answer
        if code != 204:
            return self._relay(code, data)
        _PASSWORDS.pop(id_, None)
        self.write_json({"ok": True})


class HubRequestsListHandler(_HubBase):
    @tornado.web.authenticated
    async def get(self):
        items = await self._items("request")
        if items is None:
            return
        rows = []
        for item in items:
            uploads = await self._uploads(item.get("id", ""))
            if uploads is None:
                return
            rows.append(request_from_item(item, uploads, self._link(item.get("url", ""))))
        self.write_json({"requests": rows})

    @tornado.web.authenticated
    async def post(self):
        body = self.get_json_body() or {}
        name = (body.get("name") or "").strip()
        password = str(body.get("password") or "")
        if not name:
            return self.write_error_json(400, "Missing 'name'")
        answer = await self._hub("POST", "requests", {"title": name, "password": password})
        if answer is None:
            return
        code, data = answer
        if code not in (201, 202):
            return self._relay(code, data)
        if password:
            _PASSWORDS[data.get("id", "")] = password
        item = {
            "id": data.get("id", ""),
            "title": name,
            "state": data.get("state") or "ready",
            "url": data.get("url", ""),
            "has_password": bool(password),
            "created_at": _now_iso(),
        }
        row = request_from_item(item, [], self._link(item["url"]))
        self.write_json(await self._apply_cloud_default("request", row))


class HubRequestItemHandler(_HubBase):
    @tornado.web.authenticated
    async def get(self, id_):
        items = await self._items("request")
        if items is None:
            return
        for item in items:
            if item.get("id") == id_:
                uploads = await self._uploads(id_)
                if uploads is None:
                    return
                return self.write_json(
                    request_from_item(item, uploads, self._link(item.get("url", "")))
                )
        self.write_error_json(404, "not found")

    @tornado.web.authenticated
    async def delete(self, id_):
        answer = await self._hub("DELETE", f"requests/{id_}")
        if answer is None:
            return
        code, data = answer
        if code != 204:
            return self._relay(code, data)
        _PASSWORDS.pop(id_, None)
        self.write_json({"ok": True})


class HubPasswordHandler(_HubBase):
    """api/<shares|requests>/<id>/password - set or clear on the hub; the
    value read back is the one set through this server process."""

    @tornado.web.authenticated
    def get(self, kind, id_):
        self.write_json({"id": id_, "password": _PASSWORDS.get(id_, "")})

    @tornado.web.authenticated
    async def post(self, kind, id_):
        body = self.get_json_body() or {}
        password = str(body.get("password") or "")
        answer = await self._hub("PUT", f"{kind}/{id_}/password", {"password": password})
        if answer is None:
            return
        code, data = answer
        if code != 204:
            return self._relay(code, data)
        if password:
            _PASSWORDS[id_] = password
        else:
            _PASSWORDS.pop(id_, None)
        self.write_json({"id": id_, "password": password, "has_password": bool(password)})


class HubUploadFetchHandler(_HubBase):
    """api/requests/<id>/uploads/<upload_id>/fetch - copy one recipient
    upload into the workspace through the hub's transfer job.

    The hub creates ``dest`` and refuses one that exists, so the lab picks a
    fresh directory under the folder the panel names (the file browser's
    current folder) - the upload lands inside it under its own name.
    """

    @tornado.web.authenticated
    async def post(self, id_, upload_id):
        body = self.get_json_body() or {}
        target_dir = str(body.get("target_dir") or "").strip("/")
        base = _safe_name(str(body.get("name") or "upload"))
        if target_dir and not _is_safe_relative(target_dir):
            return self.write_error_json(400, f"Unsafe target_dir: {target_dir}")
        root = Path(self.workspace_root)
        parent = root / target_dir if target_dir else root
        if not parent.is_dir():
            return self.write_error_json(404, f"Not a folder: {target_dir or '.'}")
        dest = _resolve_unique_target(parent, base)
        rel = dest.relative_to(root).as_posix()
        answer = await self._hub(
            "POST", f"requests/{id_}/uploads/{upload_id}/fetch", {"dest": rel}
        )
        if answer is None:
            return
        code, data = answer
        if code != 200:
            return self._relay(code, data)
        path = data.get("path") if isinstance(data, dict) else ""
        self.write_json({"ok": True, "path": path or rel})


def hub_handlers(base_url: str, ns: str) -> list:
    """The complete hub-mode route table: authenticated ``api/*`` only."""
    api = lambda *parts: url_path_join(base_url, ns, "api", *parts)  # noqa: E731
    return [
        (api("info"), HubInfoHandler),
        (api("stream"), HubStreamHandler),
        (api("link-check"), HubLinkCheckHandler),
        (api("tunnel"), HubTunnelHandler),
        (api("generate-password"), GeneratePasswordHandler),
        (api(r"(shares|requests)", HUB_ID, "password"), HubPasswordHandler),
        (api(r"(shares|requests)", HUB_ID, "cloud"), HubCloudHandler),
        (api("shares"), HubSharesListHandler),
        (api("shares", HUB_ID), HubShareItemHandler),
        (api("requests"), HubRequestsListHandler),
        (api("requests", HUB_ID), HubRequestItemHandler),
        (api("requests", HUB_ID, "uploads", UPLOAD_ID, "fetch"), HubUploadFetchHandler),
    ]
