"""Reading a connected peer through this server (api/connections/<key>/manifest
and /download).

The panel used to read these from the browser, straight from the peer's
origin; a lab page under a Content-Security-Policy of default-src 'self'
refused that before it left the page (DEF-PEER-72). Stub-handler style of
test_connection_save.py - no Tornado server, the peer fetch is replaced.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from jupyterlab_share_files_extension import routes
from jupyterlab_share_files_extension.config import ShareFilesConfig
from jupyterlab_share_files_extension.storage import ConnectionStore

PEER_HOST = "https://peer.example.com"
PEER_LINK = PEER_HOST + "/user/bob/jupyterlab-share-files-extension/public/share/QQQQ22"
MANIFEST = {
    "id": "QQQQ22",
    "name": "Bob's share",
    "kind": "share",
    "entries": [{"name": "a b.txt", "type": "file", "size": 3}],
}


@pytest.fixture(autouse=True)
def _no_kept_tokens():
    """Each test starts with no unlock token kept from an earlier one."""
    routes._PEER_TOKENS.clear()
    yield
    routes._PEER_TOKENS.clear()


def _live_token(sig="sig"):
    """A token in the peer's ``<expiry>.<sig>`` shape, valid for an hour."""
    return f"{int(time.time()) + 3600}.{sig}"


class _PeerResponse:
    def __init__(self, code, body=b"{}", content_type=""):
        self.code = code
        self.body = body
        self.headers = {"Content-Type": content_type} if content_type else {}


def _handler(cls, workspace, *, password="", link=PEER_LINK, name="", answers=None, fails=""):
    """A handler over one stored share connection; ``answers`` maps the path
    after the link to the peer's response, ``fails`` makes every peer fetch
    raise PeerUnavailable with that message."""
    entry = ConnectionStore(str(workspace)).add(
        "share", "QQQQ22", PEER_HOST, link=link, password=password
    )
    handler = object.__new__(cls)
    handler.request = type("_Req", (), {"method": "GET", "headers": {}})()
    handler.application = type(
        "_App",
        (),
        {
            "settings": {
                "share_files_config": ShareFilesConfig(),
                "base_url": "/",
                "server_root_dir": str(workspace),
            }
        },
    )()
    handler._current_user = "tester"
    handler.status = 200
    handler.payload = None
    handler.body = None
    handler.headers = {}
    handler.calls = []
    handler.get_argument = lambda key, default="": {"name": name}.get(key, default)
    handler.set_header = lambda k, v: handler.headers.__setitem__(k, v)
    handler.write_json = lambda p: setattr(handler, "payload", p)

    def _finish(b=None, **kwargs):
        # APIHandler.finish takes the content type as a keyword
        handler.body = b
        if "set_content_type" in kwargs:
            handler.headers["Content-Type"] = kwargs["set_content_type"]

    handler.finish = _finish

    def _write_error(code, message):
        handler.status = code
        handler.payload = {"error": message}

    handler.write_error_json = _write_error

    async def _peer_fetch(url, **kwargs):
        handler.calls.append((url, kwargs))
        if fails:
            raise routes.PeerUnavailable(fails)
        return answers[url.split(PEER_LINK + "/", 1)[1]]

    handler._peer_fetch = _peer_fetch
    return handler, entry["key"]


def _manifest(workspace, **kw):
    kw.setdefault("answers", {"manifest": _PeerResponse(200, json.dumps(MANIFEST).encode())})
    return _handler(routes.ConnectionManifestHandler, workspace, **kw)


def _download(workspace, **kw):
    kw.setdefault("name", "a b.txt")
    kw.setdefault("answers", {"download/a%20b.txt": _PeerResponse(200, b"abc", "text/plain")})
    return _handler(routes.ConnectionDownloadHandler, workspace, **kw)


def test_the_manifest_is_relayed_and_never_cached(tmp_path):
    handler, key = _manifest(tmp_path)
    asyncio.run(handler.get(key))
    assert handler.status == 200
    assert handler.payload == MANIFEST
    assert handler.headers["Cache-Control"] == "no-store"
    (url, kwargs), = handler.calls
    assert url == PEER_LINK + "/manifest"
    assert kwargs["headers"] == {} and kwargs["download"] is False


def test_a_protected_peer_is_unlocked_with_the_stored_password(tmp_path):
    handler, key = _manifest(
        tmp_path,
        password="pw",
        answers={
            "unlock": _PeerResponse(200, b'{"token": "tok"}'),
            "manifest": _PeerResponse(200, json.dumps(MANIFEST).encode()),
        },
    )
    asyncio.run(handler.get(key))
    assert handler.status == 200
    assert [u.rsplit("/", 1)[-1] for u, _ in handler.calls] == ["unlock", "manifest"]
    assert handler.calls[1][1]["headers"] == {"X-Share-Token": "tok"}


def test_a_kept_token_is_reused_until_it_expires(tmp_path):
    # the peer charges every unlock against its password limiter; the
    # panel's polls must not spend an attempt each
    token = _live_token()
    handler, key = _manifest(
        tmp_path,
        password="pw",
        answers={
            "unlock": _PeerResponse(200, json.dumps({"token": token}).encode()),
            "manifest": _PeerResponse(200, json.dumps(MANIFEST).encode()),
        },
    )
    asyncio.run(handler.get(key))
    asyncio.run(handler.get(key))
    assert [u.rsplit("/", 1)[-1] for u, _ in handler.calls] == ["unlock", "manifest", "manifest"]
    assert handler.calls[2][1]["headers"] == {"X-Share-Token": token}
    # a token about to expire is not sent again
    routes._PEER_TOKENS[(PEER_LINK, "pw")] = f"{int(time.time()) + 30}.sig"
    asyncio.run(handler.get(key))
    assert [u.rsplit("/", 1)[-1] for u, _ in handler.calls][-2:] == ["unlock", "manifest"]


def test_a_token_the_peer_refuses_is_replaced_once(tmp_path):
    # the owner changed the password on the peer and the connection was
    # reconnected: the kept token is stale, the new password unlocks again
    handler, key = _manifest(tmp_path, password="new")
    stale, fresh = _live_token("stale"), _live_token("fresh")
    routes._PEER_TOKENS[(PEER_LINK, "new")] = stale
    manifests = iter(
        [_PeerResponse(401, b'{"error": "unlock"}'), _PeerResponse(200, json.dumps(MANIFEST).encode())]
    )

    async def _peer_fetch(url, **kwargs):
        handler.calls.append((url, kwargs))
        if url.endswith("/unlock"):
            return _PeerResponse(200, json.dumps({"token": fresh}).encode())
        return next(manifests)

    handler._peer_fetch = _peer_fetch
    asyncio.run(handler.get(key))
    assert handler.status == 200 and handler.payload == MANIFEST
    assert [u.rsplit("/", 1)[-1] for u, _ in handler.calls] == ["manifest", "unlock", "manifest"]
    assert handler.calls[0][1]["headers"] == {"X-Share-Token": stale}
    assert handler.calls[2][1]["headers"] == {"X-Share-Token": fresh}


def test_a_rate_limited_unlock_says_to_wait(tmp_path):
    handler, key = _manifest(
        tmp_path, password="pw", answers={"unlock": _PeerResponse(429, b"{}")}
    )
    asyncio.run(handler.get(key))
    assert handler.status == 502
    assert handler.payload == {"error": "too many password attempts - wait before retrying"}


@pytest.mark.parametrize(
    "code, status, words",
    [
        (401, 401, "no longer accepts this connection's password"),
        (404, 404, "removed this share or request"),
        (500, 502, "Remote unavailable (500)"),
    ],
)
def test_a_peer_answer_other_than_200_keeps_its_meaning(tmp_path, code, status, words):
    handler, key = _manifest(tmp_path, answers={"manifest": _PeerResponse(code, b"")})
    asyncio.run(handler.get(key))
    assert handler.status == status
    assert words in handler.payload["error"]


def test_an_unreachable_peer_is_a_502_with_the_cause(tmp_path):
    handler, key = _manifest(tmp_path, fails="Could not reach the peer")
    asyncio.run(handler.get(key))
    assert handler.status == 502
    assert handler.payload == {"error": "Could not reach the peer"}


def test_an_unreadable_manifest_is_a_502(tmp_path):
    handler, key = _manifest(tmp_path, answers={"manifest": _PeerResponse(200, b"<html>")})
    asyncio.run(handler.get(key))
    assert handler.status == 502
    assert "unreadable manifest" in handler.payload["error"]


def test_an_unknown_connection_is_a_404(tmp_path):
    handler, _ = _manifest(tmp_path)
    asyncio.run(handler.get("share:https://nowhere:XXXX"))
    assert handler.status == 404
    assert handler.calls == []


def test_a_connection_without_a_link_is_refused_before_any_fetch(tmp_path):
    handler, key = _manifest(tmp_path, link="")
    asyncio.run(handler.get(key))
    assert handler.status == 400
    assert "reconnect" in handler.payload["error"]
    assert handler.calls == []


def test_the_download_is_relayed_as_an_attachment(tmp_path):
    handler, key = _download(tmp_path)
    asyncio.run(handler.get(key))
    assert handler.status == 200
    assert handler.body == b"abc"
    assert handler.headers["Content-Type"] == "text/plain"
    assert handler.headers["Content-Disposition"] == "attachment; filename*=UTF-8''a%20b.txt"
    assert handler.headers["Cache-Control"] == "no-store"
    (url, kwargs), = handler.calls
    assert url == PEER_LINK + "/download/a%20b.txt"
    assert kwargs["download"] is True


def test_a_nested_entry_keeps_its_path_and_takes_its_own_name(tmp_path):
    handler, key = _download(
        tmp_path, name="dir/x.bin", answers={"download/dir/x.bin": _PeerResponse(200, b"\x00")}
    )
    asyncio.run(handler.get(key))
    assert handler.status == 200
    assert handler.headers["Content-Type"] == "application/octet-stream"
    assert handler.headers["Content-Disposition"] == "attachment; filename*=UTF-8''x.bin"


def test_a_download_without_a_name_is_refused(tmp_path):
    handler, key = _download(tmp_path, name="")
    asyncio.run(handler.get(key))
    assert handler.status == 400
    assert handler.calls == []


def test_a_download_the_peer_refuses_keeps_the_peer_status(tmp_path):
    handler, key = _download(tmp_path, answers={"download/a%20b.txt": _PeerResponse(404, b"")})
    asyncio.run(handler.get(key))
    assert handler.status == 404
    assert handler.body is None
