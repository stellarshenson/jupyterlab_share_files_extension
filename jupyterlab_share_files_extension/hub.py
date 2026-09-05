"""Hub mode - the lab is spawned by galaxahub and shares only through its API.

galaxahub injects two variables into every lab it spawns (their names are
reserved, so a user or a group cannot set them):

    SHARE_FILES_PUBLIC_ZONE=hub
    SHARE_FILES_HUB_API=<hub base_url>hub/api/fileshare

The first selects the mode; the second is the path of the hub's fileshare
API, joined here with the scheme and host of ``JUPYTERHUB_API_URL``. The lab
authenticates with its own ``JUPYTERHUB_API_TOKEN``.

The mode decision depends on ``SHARE_FILES_PUBLIC_ZONE`` alone. A missing
API path or token does not fall back to standalone - that would remount the
unauthenticated recipient routes on a hub-managed lab - it makes every hub
call fail with ``HubUnavailable`` instead.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

import tornado.httpclient

PUBLIC_ZONE_VAR = "SHARE_FILES_PUBLIC_ZONE"
HUB_API_VAR = "SHARE_FILES_HUB_API"
TOKEN_VAR = "JUPYTERHUB_API_TOKEN"
API_URL_VAR = "JUPYTERHUB_API_URL"
HUB_BASE_URL_VAR = "JUPYTERHUB_BASE_URL"

REQUEST_TIMEOUT_SECONDS = 30


def hub_mode() -> bool:
    """True when the hub spawned this lab with ``SHARE_FILES_PUBLIC_ZONE=hub``."""
    return os.environ.get(PUBLIC_ZONE_VAR, "").strip().lower() == "hub"


def hub_api_base() -> str:
    """Absolute base of the hub fileshare API, or '' when the contract is incomplete.

    ``SHARE_FILES_HUB_API`` is a path (``/hub/api/fileshare``); the scheme and
    host come from ``JUPYTERHUB_API_URL``. An absolute value is used verbatim.
    """
    raw = os.environ.get(HUB_API_VAR, "").strip()
    if not raw:
        return ""
    if "://" in raw:
        return raw.rstrip("/")
    parsed = urlparse(os.environ.get(API_URL_VAR, "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/{raw.lstrip('/')}".rstrip("/")


def hub_api_origin() -> str:
    """``scheme://host`` of the hub API - the address the hub sees the lab call from."""
    parsed = urlparse(hub_api_base())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def hub_base_url() -> str:
    """The hub's own base url (``/`` unless the hub is mounted under a prefix)."""
    value = os.environ.get(HUB_BASE_URL_VAR, "").strip() or "/"
    if not value.startswith("/"):
        value = "/" + value
    return value


class HubUnavailable(Exception):
    """The hub could not be reached, or the spawn contract is incomplete."""

    reason = "hub_unavailable"


class HubClient:
    """One authenticated HTTP path to the hub fileshare API.

    Every hub request goes through :meth:`request`, so the token header is
    set in exactly one place. The client raises ``HubUnavailable`` when the
    contract is incomplete or the hub does not answer; an HTTP error status
    is returned to the caller as data, never raised.
    """

    def __init__(self, base: str | None = None, token: str | None = None):
        self.base = (base if base is not None else hub_api_base()).rstrip("/")
        self.token = token if token is not None else os.environ.get(TOKEN_VAR, "")
        if not self.base:
            raise HubUnavailable(
                f"hub contract incomplete: {HUB_API_VAR} or {API_URL_VAR} is not set"
            )
        if not self.token:
            raise HubUnavailable(f"hub contract incomplete: {TOKEN_VAR} is not set")

    def headers(self, has_body: bool) -> dict[str, str]:
        """The token header, plus the JSON content type when a body rides along."""
        headers = {"Authorization": f"token {self.token}"}
        if has_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def request(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, Any]:
        """Send one request; return ``(status, parsed JSON body or {})``."""
        url = self.base + "/" + path.lstrip("/")
        payload = json.dumps(body) if body is not None else None
        client = tornado.httpclient.AsyncHTTPClient()
        try:
            resp = await client.fetch(
                url,
                method=method,
                headers=self.headers(payload is not None),
                body=payload,
                raise_error=False,
                request_timeout=REQUEST_TIMEOUT_SECONDS,
                allow_nonstandard_methods=True,
            )
        except (OSError, ConnectionError) as exc:
            raise HubUnavailable(f"could not reach the hub: {exc}") from None
        if resp.code == 599:
            raise HubUnavailable(f"the hub did not answer: {resp.error}")
        raw = resp.body or b""
        if not raw:
            return resp.code, {}
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"message": raw.decode("utf-8", "replace")[:200]}
        return resp.code, data
