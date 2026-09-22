"""Hub-mode handlers: the panel's ``api/*`` surface backed by the hub API.

Registered instead of the standalone table when :func:`hub.hub_mode` is true.
Every handler here is an ``APIHandler`` guarded by ``tornado.web.authenticated``;
the module never defines a public or static route. The panel keeps calling
the same ``api/*`` paths with the same response shapes - only the backend
changes: paths are sent to the hub, which copies the bytes with its own
transfer job, and recipients are served by the hub's fileshare app.

What has no hub equivalent is simply not mounted: removing a single upload,
peer connections, and the per-user Cloudflare tunnel. The tunnel toggle survives with a new
meaning - every hub record carries its own tunnel switch, and the toggle
flips them all and sets the default for the next one.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tornado.web
from jupyter_server.utils import url_path_join
from tornado.iostream import StreamClosedError

from .hub import HubClient, HubUnavailable, hub_api_origin
from .hub_stream import CLOSE, KEEPALIVE_SECONDS, RELAY, RETRY_SECONDS
from .routes import (
    GeneratePasswordHandler,
    _Base,
    _request_origin,
    probe_link,
)
from .storage import (
    _is_safe_relative,
    _resolve_unique_target,
    _safe_name,
    all_excluded_message,
    is_excluded,
)
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

# Config-file key for the tunnel toggle in hub mode: True switches every new
# share and request on to the hub's Cloudflare front, False leaves it on the
# hub's own address (reachable on the hub's network only). The hub mints
# every record with the switch off; the lab applies the preference after.
TUNNEL_KEY = "hub_tunnel"

# the reason a created record carries when the hub answered its switch on
# with an error (the record stays off); a hub that does not answer is
# hub_unavailable
NOT_ON_REASON = "tunnel_not_switched_on"


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
    last_add = item.get("last_add") or {}
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
        "tunnel": bool(item.get("tunnel")),
        "state": item.get("state") or "ready",
        "reason": item.get("reason") or "",
        "expires_at": _ts(item.get("expires_at")),
        "bytes": int(item.get("bytes") or 0),
        # the hub's total for the share: the create and every add since
        "skipped": int(item.get("skipped") or 0),
        # the hub's own record of the last add: running while it copies, and
        # the reason when it refused
        "adding": last_add.get("state") == "running",
        "add_reason": str(last_add.get("reason") or "") if last_add.get("state") == "refused" else "",
        # bytes copied and to copy, present only while the hub carries bytes
        "progress": item.get("progress") or None,
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
        "tunnel": bool(item.get("tunnel")),
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


def tunnel_default() -> bool:
    """The tunnel toggle: switch every new share and request on to Cloudflare."""
    return bool(_load_config().get(TUNNEL_KEY, False))


def set_tunnel_default(value: bool) -> None:
    cfg = _load_config()
    cfg[TUNNEL_KEY] = bool(value)
    _save_config(cfg)


def rewrite_link(url: str, browser_origin: str) -> str:
    """The link the panel hands out for a hub ``url``.

    The hub composes a record's url from the Host header of the request it
    answers, so a lab-originated call yields the hub's internal address; it
    is replaced by the origin the browser reaches the lab on. Any other
    origin is the Cloudflare hostname the hub chose for a record with its
    tunnel switch on, and is kept as the hub composed it.
    """
    parsed = urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return url or ""
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin != hub_api_origin():
        return url
    return browser_origin + parsed.path + (f"?{parsed.query}" if parsed.query else "")


def plural(kind: str) -> str:
    return "shares" if kind == "share" else "requests"


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
        items = await self._reconcile_tunnel(list(items or []))
        return [i for i in items if i.get("kind") == kind]

    async def _reconcile_tunnel(self, items: list[dict]) -> list[dict]:
        """The tunnel is one state, not a property of a record: while the
        switch is on, every record is on it.

        A create whose switch-on never landed - the hub did not answer - left
        the record off with the switch still on, and nothing afterwards
        brought the two together, so that record kept a hub-network link
        while the panel said sharing was on (DEF-HUB-101). Switch the
        stragglers on and re-read, so the link the panel shows is the one the
        switch promises. A record the hub refuses stays off and is read again
        next time; the switch is the owner's and one refused record does not
        revoke it.
        """
        if not tunnel_default():
            return items
        behind = [i for i in items if not i.get("tunnel")]
        if not behind:
            return items
        switched = False
        for item in behind:
            kind, id_ = item.get("kind", ""), item.get("id", "")
            code, _ = await self._set_tunnel(kind, id_, True)
            if code == 204 and not tunnel_default():
                # The owner switched off while this write was in flight. Read
                # the switch after the write, not before it, so the write can
                # be taken back: a check before it leaves the record published
                # under a header that already reads off, and nothing switches
                # it off afterwards. Sound because a switch off writes its
                # intent before it reads the records, so a write that lands
                # after the press always reads the press.
                await self._set_tunnel(kind, id_, False)
                # read the records again, as the success path below does: the
                # switch off that ran under this loop took every record off
                # the tunnel, and the list read before the write still shows
                # them on it, with tunnel links the hub no longer serves
                return await self._items_quiet() or items
            switched = switched or code == 204
        if not switched:
            return items
        return await self._items_quiet() or items

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

    def _kept_paths(self, paths: Any) -> list[str] | None:
        """The paths the hub is sent, or ``None`` with the 400 already written.
        The hub copies what it is sent, so the excluded-names catalogue is
        applied here on the chosen items themselves; below a sent folder the
        hub applies the plain names of ``_hub_exclude`` beside its own skips."""
        if not isinstance(paths, list):
            self.write_error_json(400, "'paths' must be a list")
            return None
        for rel in paths:
            if not isinstance(rel, str) or not _is_safe_relative(rel):
                self.write_error_json(400, f"Unsafe path: {rel}")
                return None
        kept = [rel for rel in paths if not is_excluded(Path(rel).name, self.excluded_names)]
        if paths and not kept:
            self.write_error_json(400, all_excluded_message([Path(rel).name for rel in paths]))
            return None
        return kept

    def _hub_exclude(self) -> list[str]:
        """The catalogue entries the hub can apply below the sent paths: it
        takes plain names, no pattern, at most 64."""
        return [n for n in self.excluded_names if not any(c in n for c in "*?[/")][:64]

    def _tunnel_state(self, capabilities: dict) -> dict:
        """The tunnel toggle as the panel reads it. The hub decides whether a
        record may be switched on (its group policy), so the toggle is always
        offered while the hub answers; a refused switch names its reason.
        ``tunnel_available`` and ``tunnel_ready`` are the hub's own verdicts -
        its group policy has a tunnel at all, and its connector is up and
        serving; ``tunnel_default`` is the lab's stored default."""
        return {
            "tunnel_configured": True,
            "tunnel_active": tunnel_default(),
            "tunnel_autostart": False,
            "tunnel_running": bool(capabilities.get("serving")),
            "tunnel_available": bool(capabilities.get("tunnel_available")),
            "tunnel_ready": bool(capabilities.get("tunnel_ready")),
            "tunnel_default": tunnel_default(),
        }

    async def _set_tunnel(self, kind: str, id_: str, tunnel: bool) -> tuple[int, Any]:
        return await self._hub_quiet("PUT", f"{plural(kind)}/{id_}/tunnel", {"tunnel": tunnel})

    async def _items_quiet(self) -> list[dict]:
        """The hub's items after a switch, or [] when the read fails."""
        code, data = await self._hub_quiet("GET", "items")
        return (data.get("items") or []) if code == 200 and isinstance(data, dict) else []

    async def _apply_tunnel_default(self, kind: str, row: dict) -> dict:
        """A record the hub just minted with its switch off: switch it on when
        the toggle says so. A refusal turns the toggle off and rides on the
        row as ``tunnel_reason`` so the panel can say why the link stayed on
        the hub's network; the switched-on row's url is read back from the
        hub, which composes it."""
        if not tunnel_default():
            return row
        code, data = await self._set_tunnel(kind, row["id"], True)
        if code == 204:
            for item in await self._items_quiet():
                if item.get("id") == row["id"]:
                    return {**row, "tunnel": True, "link": self._link(item.get("url", ""))}
            return {**row, "tunnel": True}
        reason = data.get("reason") if isinstance(data, dict) else ""
        if reason == "tunnel_not_available":
            set_tunnel_default(False)
        # code 0: the hub did not answer
        return {**row, "tunnel_reason": reason or (NOT_ON_REASON if code else HubUnavailable.reason)}


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
            "tunnel_active": tunnel_default(),
            "tunnel_autostart": False,
            "tunnel_running": False,
            "tunnel_available": False,
            "tunnel_ready": False,
            "tunnel_default": tunnel_default(),
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
    """api/tunnel - the tunnel toggle. No daemon: it flips the Cloudflare
    switch on every share and request this user has and sets the default
    for the next one. The hub refuses a switch on while the group policy has
    Cloudflare off; that refusal is relayed and the toggle stays off unless
    the switch on already switched a record."""

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
            before = tunnel_default()  # what to put back if nothing lands
            # A switch off writes its intent before it reads the records. The
            # listing path reads this same switch, and every moment it still
            # reads on is a moment a listing can put the records this handler
            # is about to switch off straight back on the tunnel - public
            # links under a header that reads off. A switch on cannot do the
            # same, because the hub may refuse it and a refused switch on has
            # to leave the stored default off.
            if not active:
                set_tunnel_default(False)
            answer = await self._hub("GET", "items")
            code, data = answer or (0, {})
            if code != 200:
                # nothing was switched, so the switch goes back to what it
                # said before this request - never to a fixed value, which
                # would answer "turn sharing off" by turning it on
                set_tunnel_default(before)
                return None if answer is None else self._relay(code, data)
            items = (data.get("items") if isinstance(data, dict) else None) or []
            switched = 0  # records the hub really switched
            for item in items:
                if bool(item.get("tunnel")) == active:
                    continue
                answer = await self._hub("PUT", f"{plural(item.get('kind', ''))}/{item.get('id', '')}/tunnel", {"tunnel": active})
                # 204 switched, 404 the record is already gone (DEF-HUB-54)
                if answer is None or answer[0] not in (204, 404):
                    # a switch that got at least one record moved keeps the
                    # press: a switch off that put two of three records off
                    # the tunnel must not read on again, or the next listing
                    # reconciles the two it just closed straight back open.
                    # A switch that moved nothing goes back to what the
                    # switch said before the request - never to a fixed
                    # value, which would answer "stop sharing" with "on".
                    set_tunnel_default(active if switched else before)
                    return None if answer is None else self._relay(*answer)
                if answer[0] == 204:
                    switched += 1
            set_tunnel_default(active)
        self.write_json(self._tunnel_state(caps))


class HubItemTunnelHandler(_HubBase):
    """api/<shares|requests>/<id>/tunnel - one record's Cloudflare switch."""

    @tornado.web.authenticated
    async def post(self, kind, id_):
        body = self.get_json_body() or {}
        tunnel = body.get("tunnel")
        if not isinstance(tunnel, bool):
            return self.write_error_json(400, "'tunnel' must be a boolean")
        answer = await self._hub("PUT", f"{kind}/{id_}/tunnel", {"tunnel": tunnel})
        if answer is None:
            return
        code, data = answer
        if code != 204:
            return self._relay(code, data)
        self.write_json({"id": id_, "tunnel": tunnel})


class HubStreamHandler(_HubBase):
    """api/stream - the panel's change stream, Server-Sent Events held open.

    One ``changed`` event per ring of the hub's own stream (the lab holds one
    hub stream for all its panels, ``hub_stream.RELAY``) and a keepalive
    comment while idle. No payload: the panel
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
        paths = self._kept_paths(paths)
        if paths is None:
            return
        answer = await self._hub(
            "POST", "shares", {"title": name, "paths": paths, "password": password, "exclude": self._hub_exclude()}
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
        self.write_json(await self._apply_tunnel_default("share", row))


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


class HubShareItemsHandler(_HubBase):
    """api/shares/<id>/items - a share's contents on the hub, through its one
    ``content`` route: POST adds, DELETE removes, PUT renames. A name holds
    ``/`` for a nested entry, and a rename to a name under another folder
    moves it."""

    async def _content(self, id_: str, body: dict) -> int | None:
        """One content call; ``None`` when the refusal was already written."""
        answer = await self._hub("POST", f"shares/{id_}/content", body)
        if answer is None:
            return None
        code, data = answer
        if code not in (202, 204):
            self._relay(code, data)
            return None
        return code

    @tornado.web.authenticated
    async def post(self, id_):
        paths = self._kept_paths((self.get_json_body() or {}).get("paths") or [])
        if paths is None:
            return
        # The hub lands every path at the share root under its own name. It
        # refuses a taken name without saying which, and drops two paths of
        # one name after its 202, so both are answered here by name.
        names = [Path(rel).name for rel in paths]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            return self.write_error_json(
                400, f"Two of the chosen items are named {', '.join(twice)} - a share holds one entry per name"
            )
        items = await self._items("share")
        if items is None:
            return
        held = {
            str(f.get("name", "")).split("/")[0]
            for item in items if item.get("id") == id_
            for f in item.get("files") or []
        }
        taken = [n for n in names if n in held]
        if taken:
            return self.write_error_json(
                400, f"The share already holds {', '.join(taken)} - remove or rename it there first"
            )
        body = {
            "action": "add", "paths": paths, "password": _PASSWORDS.get(id_, ""),
            "exclude": self._hub_exclude(),
        }
        answer = await self._hub("POST", f"shares/{id_}/content", body)
        if answer is None:
            return
        code, data = answer
        if isinstance(data, dict) and data.get("reason") in ("password_required", "password_wrong"):
            # the hub keeps a hash only, and this process holds the password
            # from the moment it was set here until it restarts
            return self.write_error_json(
                400,
                "The hub asks for this share's password before it adds files, and the lab does not hold "
                "the current one - open Change Password in the share's menu, enter the same password "
                "again, then add the files",
            )
        if code not in (202, 204):
            return self._relay(code, data)
        self.write_json({"ok": True})

    @tornado.web.authenticated
    async def delete(self, id_):
        for name in [n for n in self.get_arguments("name") if n]:
            if await self._content(id_, {"action": "remove", "name": name}) is None:
                return
        self.write_json({"ok": True})

    @tornado.web.authenticated
    async def put(self, id_):
        body = self.get_json_body() or {}
        rename = {
            "action": "rename",
            "name": str(body.get("name") or ""),
            "new_name": str(body.get("new_name") or ""),
        }
        if await self._content(id_, rename) is None:
            return
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
        self.write_json(await self._apply_tunnel_default("request", row))


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
        (api(r"(shares|requests)", HUB_ID, "tunnel"), HubItemTunnelHandler),
        (api("shares"), HubSharesListHandler),
        (api("shares", HUB_ID), HubShareItemHandler),
        (api("shares", HUB_ID, "items"), HubShareItemsHandler),
        (api("requests"), HubRequestsListHandler),
        (api("requests", HUB_ID), HubRequestItemHandler),
        (api("requests", HUB_ID, "uploads", UPLOAD_ID, "fetch"), HubUploadFetchHandler),
    ]
