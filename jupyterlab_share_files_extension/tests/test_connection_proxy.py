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
import types

import pytest
import tornado.httputil

from jupyterlab_share_files_extension import routes
from jupyterlab_share_files_extension.storage import ConnectionStore
from jupyterlab_share_files_extension.tests._stubs import stub_handler

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
    def __init__(self, code, body=b"{}", content_type="", announces_length=True):
        self.code = code
        self.body = body
        self.headers = tornado.httputil.HTTPHeaders()
        if content_type:
            self.headers["Content-Type"] = content_type
        if announces_length:
            self.headers["Content-Length"] = str(len(body))


def _handler(cls, workspace, *, password="", link=PEER_LINK, name="", max_gb="", answers=None, fails=""):
    """A handler over one stored share connection; ``answers`` maps the path
    after the link to the peer's response, ``fails`` makes every peer fetch
    raise PeerUnavailable with that message."""
    entry = ConnectionStore(str(workspace)).add(
        "share", "QQQQ22", PEER_HOST, link=link, password=password
    )
    handler = stub_handler(cls, workspace, request=types.SimpleNamespace(method="GET", headers={}))
    handler.body = None
    handler.headers = {}
    handler.calls = []
    handler.chunks = []  # what the download handler streamed before finish
    handler.get_argument = lambda key, default="": {"name": name, "max_gb": max_gb}.get(key, default)
    handler.set_header = lambda k, v: handler.headers.__setitem__(k, v)
    handler.write = handler.chunks.append

    async def _flush():
        pass

    handler.flush = _flush

    def _finish(b=None, **kwargs):
        # APIHandler.finish takes the content type as a keyword
        handler.body = b"".join(handler.chunks) if handler.chunks else b
        if "set_content_type" in kwargs:
            handler.headers["Content-Type"] = kwargs["set_content_type"]

    handler.finish = _finish

    async def _peer_fetch(url, **kwargs):
        handler.calls.append((url, kwargs))
        if fails:
            raise routes.PeerUnavailable(fails)
        resp = answers[url.split(PEER_LINK + "/", 1)[1]]
        if "spool" in kwargs:
            # a download lands in the spool, flushed as the real fetch
            # streams it, once the peer's 200 headers have started the relay
            if resp.code == 200:
                kwargs["started"].set_result(resp.headers)
            _land(kwargs["spool"], resp.body)
        return resp

    handler._peer_fetch = _peer_fetch
    return handler, entry["key"]


def _land(spool, chunk):
    """What the real fetch's streaming callback does with a chunk."""
    spool.write(chunk)
    spool.flush()


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
    assert kwargs["headers"] == {} and "spool" not in kwargs


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


def test_a_removed_protected_share_is_named_at_the_unlock(tmp_path):
    # the peer answers 404 before it reads the password: the removal, not
    # a password change, is what the owner reads
    handler, key = _manifest(
        tmp_path, password="pw", answers={"unlock": _PeerResponse(404, b"")}
    )
    asyncio.run(handler.get(key))
    assert handler.status == 502
    assert handler.payload == {"error": "The owner has removed this share or request."}


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
    assert "disconnect it and connect again" in handler.payload["error"]
    assert handler.calls == []


def test_the_download_is_relayed_as_an_attachment(tmp_path, monkeypatch):
    """The entry is spooled to disk under the store and relayed to the
    browser in chunks with the length the peer announced; the spool is
    removed afterwards."""
    monkeypatch.setattr(routes, "_EXTRACT_CHUNK_BYTES", 2, raising=False)
    handler, key = _download(tmp_path)
    asyncio.run(handler.get(key))
    assert handler.status == 200
    assert handler.chunks == [b"ab", b"c"]
    assert handler.body == b"abc"
    assert handler.headers["Content-Type"] == "text/plain"
    assert handler.headers["Content-Length"] == "3"
    assert handler.headers["Content-Disposition"] == "attachment; filename*=UTF-8''a%20b.txt"
    assert handler.headers["Cache-Control"] == "no-store"
    (url, kwargs), = handler.calls
    assert url == PEER_LINK + "/download/a%20b.txt"
    assert kwargs["spool"].name.startswith(str(tmp_path / "uploads" / "tmp" / "download-"))
    assert kwargs["max_bytes"] == 10 * routes.GB
    assert list((tmp_path / "uploads" / "tmp").iterdir()) == []


def test_a_peer_announcing_no_length_is_relayed_without_one(tmp_path, monkeypatch):
    """A folder arrives as a zip the peer streams without a length: the
    browser gets the same chunks and no Content-Length."""
    monkeypatch.setattr(routes, "_EXTRACT_CHUNK_BYTES", 2, raising=False)
    handler, key = _download(
        tmp_path, answers={"download/a%20b.txt": _PeerResponse(200, b"abc", "application/zip", announces_length=False)}
    )
    asyncio.run(handler.get(key))
    assert handler.status == 200
    assert handler.chunks == [b"ab", b"c"]
    assert handler.headers["Content-Type"] == "application/zip"
    # the browser takes this name over the anchor's download attribute
    assert handler.headers["Content-Disposition"] == "attachment; filename*=UTF-8''a%20b.txt.zip"
    assert "Content-Length" not in handler.headers
    assert list((tmp_path / "uploads" / "tmp").iterdir()) == []


def test_bytes_reach_the_browser_while_the_peer_is_still_sending(tmp_path, monkeypatch):
    """The relay tails the spool: the first chunk is written to the browser
    before the peer fetch has returned. A relay that waits for the whole
    body deadlocks against this peer, so the test is bounded."""
    monkeypatch.setattr(routes, "_EXTRACT_CHUNK_BYTES", 2, raising=False)
    handler, key = _download(tmp_path)

    async def _peer_fetch(url, **kwargs):
        kwargs["started"].set_result(tornado.httputil.HTTPHeaders({"Content-Length": "3"}))
        _land(kwargs["spool"], b"ab")
        while not handler.chunks:
            await asyncio.sleep(0.01)
        _land(kwargs["spool"], b"c")
        return _PeerResponse(200, b"abc")

    handler._peer_fetch = _peer_fetch
    asyncio.run(asyncio.wait_for(handler.get(key), 5))
    assert handler.status == 200
    assert handler.chunks == [b"ab", b"c"]
    assert handler.headers["Content-Length"] == "3"


def test_a_peer_that_fails_mid_body_closes_the_browser_connection(tmp_path, monkeypatch):
    """Bytes are out, so no JSON answer can follow: the connection is closed
    without finish() and the browser marks the download failed."""
    monkeypatch.setattr(routes, "_EXTRACT_CHUNK_BYTES", 2, raising=False)
    handler, key = _download(tmp_path)
    closed = []
    handler.request.connection = type("_Conn", (), {"close": lambda self: closed.append(True)})()

    async def _peer_fetch(url, **kwargs):
        kwargs["started"].set_result(tornado.httputil.HTTPHeaders({"Content-Length": "3"}))
        _land(kwargs["spool"], b"ab")
        raise routes.PeerUnavailable("The peer closed the connection before the download finished")

    handler._peer_fetch = _peer_fetch
    asyncio.run(handler.get(key))
    assert handler.chunks == [b"ab"]
    assert closed == [True]
    assert handler.payload is None and handler.body is None
    assert list((tmp_path / "uploads" / "tmp").iterdir()) == []


async def _serve_slow_peer(pieces):
    """A loopback peer whose download announces the pieces' total and sends
    each piece after its delay. Returns the server, the link and a future
    that resolves when the lab closes the connection."""
    dropped = asyncio.get_running_loop().create_future()

    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        eof = asyncio.ensure_future(reader.read())
        eof.add_done_callback(lambda t: dropped.set_result(t.result() == b""))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % sum(len(p) for _, p in pieces))
        for delay, piece in pieces:
            await asyncio.sleep(delay)
            writer.write(piece)
            try:
                await writer.drain()
            except ConnectionError:
                break

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/public/share/QQQQ22", dropped


def test_a_browser_that_gives_up_ends_the_peer_fetch(tmp_path, monkeypatch):
    """The browser cancels mid-relay: the flush fails, the spool closes and
    the fetch stops at the peer's next chunk instead of downloading the rest
    into a file nothing reads. Bounded well under the peer's last piece."""
    monkeypatch.setattr(routes, "_EXTRACT_CHUNK_BYTES", 2, raising=False)
    flushes = []

    async def run():
        server, link, dropped = await _serve_slow_peer([(0, b"ab"), (0.1, b"cd"), (0.1, b"ef"), (3, b"gh")])
        handler, key = _download(tmp_path, link=link)
        del handler._peer_fetch  # the real one, against the loopback peer
        closed = []
        handler.request.connection = type("_Conn", (), {"close": lambda self: closed.append(True)})()

        async def flush():
            flushes.append(True)
            if len(flushes) > 1:
                raise routes.StreamClosedError()

        handler.flush = flush
        try:
            await asyncio.wait_for(handler.get(key), 2)
            assert await asyncio.wait_for(dropped, 2)
        finally:
            server.close()
        return handler, closed

    handler, closed = asyncio.run(run())
    assert handler.chunks == [b"ab", b"cd"]
    assert closed == [True]
    assert handler.payload is None
    assert list((tmp_path / "uploads" / "tmp").iterdir()) == []


async def _serve_announcing_peer(size):
    """A loopback peer whose download announces ``size`` bytes and sends
    none, until the lab closes the connection. Returns the server and the
    link."""

    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % size)
        await writer.drain()
        await reader.read()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/public/share/QQQQ22"


def test_a_download_over_the_limit_is_refused_before_anything_is_streamed(tmp_path):
    """A 200 announcing more than the limit never starts the relay: the
    existing 502 answers and the browser sees no headers or bytes."""

    async def run():
        server, link = await _serve_announcing_peer(2 * routes.GB)
        handler, key = _download(tmp_path, link=link, max_gb="1")
        del handler._peer_fetch  # the real one, where the limit check lives
        try:
            await handler.get(key)
        finally:
            server.close()
        return handler

    handler = asyncio.run(run())
    assert handler.status == 502
    assert handler.payload == {"error": "The peer's download is larger than the 1 GB limit"}
    assert handler.chunks == [] and handler.headers == {}
    assert list((tmp_path / "uploads" / "tmp").iterdir()) == []


def test_the_download_limit_comes_from_the_query(tmp_path):
    handler, key = _download(tmp_path, max_gb="2")
    asyncio.run(handler.get(key))
    assert handler.status == 200
    assert handler.calls[0][1]["max_bytes"] == 2 * routes.GB


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


async def _serve_protected_peer(token):
    """A loopback peer behind a password: the unlock answers ``token``, a
    download carrying it answers ``abc``, one without it answers 401 with a
    body. Returns the server and the link."""

    async def serve(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        line, _, headers = head.partition(b"\r\n")
        if line.split(b" ")[1].endswith(b"/unlock"):
            status, body = b"200 OK", json.dumps({"token": token}).encode()
        elif b"X-Share-Token: " + token.encode() in headers:
            status, body = b"200 OK", b"abc"
        else:
            status, body = b"401 Unauthorized", b'{"error": "unlock"}'
        writer.write(b"HTTP/1.1 %s\r\nContent-Length: %d\r\n\r\n%s" % (status, len(body), body))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/public/share/QQQQ22"


def test_a_download_retried_with_a_fresh_unlock_starts_the_spool_empty(tmp_path):
    """Every answer lands in the spool, the 401's body too: the download the
    fresh token unlocks must not carry it in front of the file."""
    fresh = _live_token("fresh")

    async def run():
        server, link = await _serve_protected_peer(fresh)
        handler, key = _download(tmp_path, password="pw", link=link)
        del handler._peer_fetch  # the real one, against the loopback peer
        routes._PEER_TOKENS[(link, "pw")] = _live_token("stale")
        try:
            await handler.get(key)
        finally:
            server.close()
        return handler

    handler = asyncio.run(run())
    assert handler.status == 200, handler.payload
    assert handler.body == b"abc"
    assert handler.headers["Content-Length"] == "3"


def test_a_download_the_peer_refuses_keeps_the_peer_status(tmp_path):
    handler, key = _download(tmp_path, answers={"download/a%20b.txt": _PeerResponse(404, b"")})
    asyncio.run(handler.get(key))
    assert handler.status == 404
    assert handler.body is None and handler.chunks == []
    assert list((tmp_path / "uploads" / "tmp").iterdir()) == []
