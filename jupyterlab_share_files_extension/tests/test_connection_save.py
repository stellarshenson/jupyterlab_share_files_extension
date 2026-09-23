"""Saving from a connected share into the workspace (api/connections/<key>/save).

The peer controls its manifest, so the folder name Save All creates must
never leave the chosen target folder, and no save may land inside the
extension's own shares/requests store. Stub-handler style of
test_password.py - no Tornado server; the peer fetch is replaced, or a
loopback peer answers the real one.
"""

from __future__ import annotations

import asyncio
import io
import json
import lzma
import os
import re
import types
import zipfile
import zlib
from pathlib import Path
from urllib.parse import unquote

import pytest
import tornado.httpclient
import tornado.httpserver
import tornado.netutil
import tornado.web

from jupyterlab_share_files_extension import routes
from jupyterlab_share_files_extension.config import ShareFilesConfig
from jupyterlab_share_files_extension.storage import ConnectionStore, StorageError

PEER_HOST = "https://peer.example.com"
PEER_LINK = PEER_HOST + "/user/bob/jupyterlab-share-files-extension/public/share/QQQQ22"


class _PeerResponse:
    def __init__(self, code, body=b"{}"):
        self.code = code
        self.body = body


def _zip_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _stub_handler(cls, workspace, body, kind, id_, link):
    """A handler of ``cls`` over one stored connection, no Tornado server:
    the JSON body and the answer are plain attributes."""
    entry = ConnectionStore(str(workspace)).add(kind, id_, PEER_HOST, link=link)
    handler = object.__new__(cls)
    handler.request = type("_Req", (), {"method": "POST", "headers": {}})()
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
    handler._current_user = "tester"  # satisfies @tornado.web.authenticated
    handler.get_json_body = lambda: body
    handler.status = 200
    handler.payload = None
    handler.write_json = lambda p: setattr(handler, "payload", p)

    def _write_error(code, message, reason=""):
        handler.status = code
        handler.payload = {"error": message, "reason": reason} if reason else {"error": message}

    handler.write_error_json = _write_error
    return handler, entry["key"]


def _save_handler(workspace, body, manifest, files=None, peer=None, link=PEER_LINK):
    """ConnectionSaveHandler for a stored share connection, peer stubbed.

    ``files`` is the Save All zip, ``peer`` adds answers by the last path
    component of the fetched url."""
    handler, key = _stub_handler(routes.ConnectionSaveHandler, workspace, body, "share", "QQQQ22", link)
    responses = {
        "manifest": _PeerResponse(200, json.dumps(manifest).encode()),
        "download-all": _PeerResponse(200, _zip_of(files or {"hello.txt": b"hi"})),
        **(peer or {}),
    }

    async def _peer_fetch(url, **kwargs):
        resp = responses[url.rsplit("/", 1)[-1]]
        if "spool" in kwargs:
            # a download lands in the spool, as the real fetch streams it
            kwargs["spool"].write(resp.body)
        return resp

    handler._peer_fetch = _peer_fetch
    return handler, key


def _files_under(root):
    return {p for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    "target_dir, slug",
    [
        ("", "../../escape"),
        ("", "../outside"),
        ("", "/abs/escape"),
        ("sub", ".."),
        ("sub", "."),
        ("sub", "..\\..\\escape"),
    ],
)
def test_save_all_never_writes_outside_the_target_folder(tmp_path, target_dir, slug):
    workspace = tmp_path / "home" / "workspace"
    workspace.mkdir(parents=True)
    dest_root = workspace / target_dir if target_dir else workspace
    before = _files_under(tmp_path)
    handler, key = _save_handler(workspace, {"target_dir": target_dir}, {"slug": slug, "entries": []})
    asyncio.run(handler.post(key))
    written = _files_under(tmp_path) - before
    # connections.json is the store's own write, not the save
    written = {p for p in written if p.name != "connections.json"}
    for path in written:
        assert path.resolve().is_relative_to(dest_root.resolve()), path
        # the wrapping folder is a direct child of the target folder
        assert path.resolve().parent.parent == dest_root.resolve(), path
    if handler.status == 200:
        assert len(written) == 1
    else:
        assert handler.status == 502
        assert written == set()


def test_save_all_keeps_a_plain_slug_as_the_folder_name(tmp_path):
    handler, key = _save_handler(tmp_path, {"target_dir": ""}, {"slug": "Report-2026", "entries": []})
    asyncio.run(handler.post(key))
    assert handler.status == 200
    assert handler.payload["saved"] == ["Report-2026"]
    assert (tmp_path / "Report-2026" / "hello.txt").read_bytes() == b"hi"


def test_save_all_as_a_zip_keeps_the_peers_archive(tmp_path):
    """ACC-HUBM-178 standalone: the whole share as the one zip the peer sends,
    named after it, and not unpacked."""
    handler, key = _save_handler(tmp_path, {"target_dir": "", "archive": "zip"}, {"slug": "Report-2026", "entries": []})
    asyncio.run(handler.post(key))
    assert (handler.status, handler.payload["saved"]) == (200, ["Report-2026.zip"])
    with zipfile.ZipFile(tmp_path / "Report-2026.zip") as zf:
        assert zf.read("hello.txt") == b"hi"
    assert not (tmp_path / "Report-2026").exists()


def test_save_all_as_a_zip_refuses_what_is_not_a_zip(tmp_path):
    handler, key = _save_handler(
        tmp_path, {"target_dir": "", "archive": "zip"}, {"slug": "Report-2026", "entries": []},
        peer={"download-all": _PeerResponse(200, b"not a zip")},
    )
    asyncio.run(handler.post(key))
    assert (handler.status, handler.payload) == (502, {"error": "The peer did not send a readable zip archive"})
    assert _saved_files(tmp_path) == set()


def test_a_zip_of_selected_items_is_refused(tmp_path):
    handler, key = _save_handler(
        tmp_path, {"target_dir": "", "archive": "zip", "names": ["hello.txt"]}, {"slug": "x", "entries": []}
    )
    asyncio.run(handler.post(key))
    assert handler.status == 400


@pytest.mark.parametrize(
    "target_dir",
    ["uploads", "uploads/shares", "uploads/requests", "uploads/shares/x-ABCDEF22", "uploads/requests/inbox-ABCDEF22/HASH01"],
)
def test_target_inside_the_store_is_refused(tmp_path, target_dir):
    with pytest.raises(StorageError):
        routes._resolve_workspace_target_dir(str(tmp_path), target_dir)


def test_symlink_into_the_store_is_refused(tmp_path):
    (tmp_path / "uploads" / "shares").mkdir(parents=True)
    (tmp_path / "into-store").symlink_to(tmp_path / "uploads" / "shares")
    with pytest.raises(StorageError):
        routes._resolve_workspace_target_dir(str(tmp_path), "into-store")


def test_target_outside_the_store_is_accepted(tmp_path):
    assert routes._resolve_workspace_target_dir(str(tmp_path), "data") == tmp_path / "data"
    assert routes._resolve_workspace_target_dir(str(tmp_path), "") == tmp_path
    # a folder that only starts with the store's name is not inside it
    assert routes._resolve_workspace_target_dir(str(tmp_path), "uploads-old") == tmp_path / "uploads-old"


def test_save_into_the_store_writes_nothing(tmp_path):
    handler, key = _save_handler(tmp_path, {"target_dir": "uploads/shares"}, {"slug": "x-ABCDEF22", "entries": []})
    asyncio.run(handler.post(key))
    assert handler.status == 400
    assert not (tmp_path / "uploads" / "shares").exists()


def test_save_all_answers_200_when_the_root_is_a_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    handler, key = _save_handler(tmp_path / "link", {"target_dir": ""}, {"slug": "Report-2026", "entries": []})
    asyncio.run(handler.post(key))
    assert handler.status == 200
    assert handler.payload["saved"] == ["Report-2026"]
    assert (real / "Report-2026" / "hello.txt").read_bytes() == b"hi"


def test_save_all_answers_200_into_a_symlinked_folder_outside_the_root(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "linked").symlink_to(outside)
    handler, key = _save_handler(root, {"target_dir": "linked"}, {"slug": "Report-2026", "entries": []})
    asyncio.run(handler.post(key))
    assert handler.status == 200
    assert handler.payload["saved"] == ["linked/Report-2026"]
    assert (outside / "Report-2026" / "hello.txt").read_bytes() == b"hi"


# --------------------------------------------------------------------------- #
# A download that fails, and a share too large to unpack
# --------------------------------------------------------------------------- #


async def _silent(reader, writer):
    await asyncio.Event().wait()


async def _closes_mid_download(reader, writer):
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\nPK")


def _announces(size):
    async def answer(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % size)
        await writer.drain()
        await reader.read()  # until the lab server closes the connection

    return answer


def _streams_chunked_past(limit):
    """A peer that announces no length - a folder or Save All zip - and
    streams past ``limit`` bytes; the lab closes the connection."""

    async def answer(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        piece = b"x" * 65536
        for _ in range(limit // len(piece) + 2):
            writer.write(b"%x\r\n%s\r\n" % (len(piece), piece))

    return answer


def _sends(body):
    async def answer(reader, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body))
        writer.write(body)

    return answer


async def _serve_peer(answer):
    """A loopback peer: its manifest answers at once, any other path does
    what ``answer(reader, writer)`` does. Returns the server and the link."""

    async def serve(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        if head.split(b" ")[1].endswith(b"/manifest"):
            body = json.dumps({"slug": "Report-2026", "entries": []}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        else:
            await answer(reader, writer)
        try:
            await writer.drain()
        except ConnectionResetError:
            pass  # the lab closed first: a download it refused
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/public/share/QQQQ22"


async def _save_all_from_peer(workspace, answer, body=None):
    """Save All through the real peer fetch from a loopback peer."""
    server, link = await _serve_peer(answer)
    handler, key = _save_handler(workspace, {"target_dir": "", **(body or {})}, {}, link=link)
    del handler._peer_fetch
    await handler.post(key)
    server.close()
    return handler


def test_the_limits_are_the_chosen_ones():
    assert routes.PEER_UPLOAD_SECONDS_PER_GB == 300
    assert routes.PEER_DOWNLOAD_SECONDS_PER_GB == 300
    assert routes.PEER_DOWNLOAD_DEFAULT_GB == 10


@pytest.mark.parametrize(
    "body, seconds_per_gb, waited",
    [
        ({}, 0.03, "0.3"),  # the 10 GB default
        ({"max_gb": 2}, 0.2, "0.4"),  # the panel's setting
        ({"max_gb": 0.5}, 0.3, "0.3"),  # below one GB the floor holds, as for what a landed item leaves
    ],
)
def test_save_answers_502_when_the_download_passes_its_time_limit(
    tmp_path, monkeypatch, body, seconds_per_gb, waited
):
    """DEF-PEER-36: past PEER_DOWNLOAD_SECONDS_PER_GB for every GB of the
    limit the save answers 502 with the reason, not 500."""
    monkeypatch.setattr(routes, "PEER_DOWNLOAD_SECONDS_PER_GB", seconds_per_gb, raising=False)
    handler = asyncio.run(_save_all_from_peer(tmp_path, _silent, body))
    assert (handler.status, handler.payload) == (502, {"error": f"The peer did not answer within {waited} s"})
    assert not (tmp_path / "Report-2026").exists()


def test_a_peer_request_other_than_a_download_keeps_the_short_time_limit(tmp_path, monkeypatch):
    """The manifest, the unlock and the connect probe wait PEER_TIMEOUT_SECONDS."""
    monkeypatch.setattr(routes, "PEER_TIMEOUT_SECONDS", 0.3, raising=False)
    handler, _key = _save_handler(tmp_path, {}, {})
    del handler._peer_fetch

    async def run():
        server, link = await _serve_peer(_silent)
        try:
            await handler._peer_fetch(link + "/unlock", method="POST", body="{}")
        finally:
            server.close()

    with pytest.raises(routes.PeerUnavailable, match=r"^The peer did not answer within 0\.3 s$"):
        asyncio.run(run())


@pytest.mark.parametrize(
    "answer, body, error",
    [
        (_closes_mid_download, {}, "The peer closed the connection before the download finished"),
        # the 10 GB default, and the panel's setting carried by the request
        (_announces(20 * routes.GB), {}, "The peer's download is larger than the 10 GB limit"),
        (_announces(2 * routes.GB), {"max_gb": 1}, "The peer's download is larger than the 1 GB limit"),
        # no length announced: the limit and a dropped connection end the same way
        (
            _streams_chunked_past(1_000_000),
            {"max_gb": 0.001},
            "The peer's download passed the 0.001 GB limit, or the peer closed the connection before it finished",
        ),
    ],
)
def test_save_answers_502_when_the_download_breaks_off(tmp_path, answer, body, error):
    handler = asyncio.run(_save_all_from_peer(tmp_path, answer, body))
    assert (handler.status, handler.payload) == (502, {"error": error})
    assert not (tmp_path / "Report-2026").exists()


def _saved_files(root):
    # connections.json is the store's own write, not the save
    return {p for p in _files_under(root) if p.name != "connections.json"}


def _spool_dir(root):
    return root / "uploads" / "tmp"


def test_a_download_is_written_to_disk_as_it_arrives(tmp_path, monkeypatch):
    """The peer's body goes to a spool file under the store in the chunks
    tornado reads, never held whole in memory; the spool is removed once the
    save is done."""
    sizes = []
    real = routes.tempfile.NamedTemporaryFile

    def spy(*args, **kwargs):
        spool = real(*args, **kwargs)
        write = spool.write
        spool.write = lambda chunk: sizes.append(len(chunk)) or write(chunk)
        return spool

    monkeypatch.setattr(routes.tempfile, "NamedTemporaryFile", spy)
    data = os.urandom(300 * 1024)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("big.bin", data)
    handler = asyncio.run(_save_all_from_peer(tmp_path, _sends(buf.getvalue())))
    assert handler.status == 200, handler.payload
    assert (tmp_path / "Report-2026" / "big.bin").read_bytes() == data
    assert len(sizes) > 1 and max(sizes) <= 64 * 1024, sizes
    assert sum(sizes) == len(buf.getvalue())
    assert list(_spool_dir(tmp_path).iterdir()) == []


def test_a_single_file_moves_from_the_spool_into_the_workspace(tmp_path):
    manifest = {"slug": "Report-2026", "entries": [{"name": "notes.txt", "type": "file"}]}
    handler, key = _save_handler(
        tmp_path, {"target_dir": "", "names": ["notes.txt"]}, manifest, peer={"notes.txt": _PeerResponse(200, b"12345678")}
    )
    asyncio.run(handler.post(key))
    assert (handler.status, handler.payload) == (200, {"ok": True, "saved": ["notes.txt"]})
    assert (tmp_path / "notes.txt").read_bytes() == b"12345678"
    assert list(_spool_dir(tmp_path).iterdir()) == []
    # the mode of a file the lab writes, not the spool's private 0600
    (tmp_path / "plain.txt").write_bytes(b"")
    assert (tmp_path / "notes.txt").stat().st_mode == (tmp_path / "plain.txt").stat().st_mode


def test_save_all_stops_past_the_unpacked_size_limit_and_leaves_nothing(tmp_path):
    """DEF-PEER-37: past the request's limit, unpacked, the save stops, says
    why and removes what it wrote - the spool too."""
    files = {"a.txt": b"12345678", "sub/b.txt": b"12345678"}
    handler, key = _save_handler(
        tmp_path, {"target_dir": "", "max_gb": 10 / routes.GB}, {"slug": "Report-2026", "entries": []}, files=files
    )
    asyncio.run(handler.post(key))
    assert handler.status == 502
    assert re.fullmatch(r"The share is larger than \S+ GB when unpacked", handler.payload["error"])
    assert not (tmp_path / "Report-2026").exists()
    assert _saved_files(tmp_path) == set()
    assert list(_spool_dir(tmp_path).iterdir()) == []


def test_a_save_of_selected_items_counts_them_all_against_the_limit(tmp_path):
    """The limit is for the whole save: a file under it and a folder that
    passes it together are both removed."""
    manifest = {"slug": "Report-2026", "entries": [{"name": "notes.txt", "type": "file"}, {"name": "one", "type": "directory"}]}
    peer = {
        "notes.txt": _PeerResponse(200, b"12345678"),
        "one": _PeerResponse(200, _zip_of({"one/c.txt": b"12345678"})),
    }
    handler, key = _save_handler(
        tmp_path, {"target_dir": "", "names": ["notes.txt", "one"], "max_gb": 10 / routes.GB}, manifest, peer=peer
    )
    asyncio.run(handler.post(key))
    assert handler.status == 502
    assert re.fullmatch(r"The share is larger than \S+ GB when unpacked", handler.payload["error"])
    assert not (tmp_path / "notes.txt").exists()
    assert not (tmp_path / "one").exists()
    assert _saved_files(tmp_path) == set()


@pytest.mark.parametrize("max_gb, limit", [(None, 10 * routes.GB), ("", 10 * routes.GB), (2, 2 * routes.GB), ("0.5", routes.GB // 2), (0, 10 * routes.GB)])
def test_the_download_limit_is_the_requests_or_the_default(max_gb, limit):
    assert routes._download_limit(max_gb) == limit


def test_save_writes_each_member_in_chunks(tmp_path, monkeypatch):
    """DEF-PEER-37: no member is read whole into memory."""
    monkeypatch.setattr(routes, "_EXTRACT_CHUNK_BYTES", 4, raising=False)
    sizes = []
    read = zipfile.ZipExtFile.read

    def spy(self, n=-1):
        sizes.append(n)
        return read(self, n)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", spy)
    data = bytes(range(100))
    handler, key = _save_handler(tmp_path, {"target_dir": ""}, {"slug": "Report-2026", "entries": []}, files={"big.bin": data})
    asyncio.run(handler.post(key))
    assert handler.status == 200
    assert (tmp_path / "Report-2026" / "big.bin").read_bytes() == data
    assert sizes and all(n is not None and 0 < n <= 4 for n in sizes), sizes


# --------------------------------------------------------------------------- #
# A save that fails leaves nothing, and an upload that fails says why
# --------------------------------------------------------------------------- #


def test_save_all_answers_502_when_the_peer_sends_something_that_is_not_a_zip(tmp_path):
    """DEF-PEER-52: an unreadable archive answers 502 with the reason, not
    500, and the folder the save created is removed."""
    handler, key = _save_handler(
        tmp_path,
        {"target_dir": ""},
        {"slug": "Report-2026", "entries": []},
        peer={"download-all": _PeerResponse(200, b"not a zip")},
    )
    asyncio.run(handler.post(key))
    assert (handler.status, handler.payload) == (
        502,
        {"error": "The peer did not send a readable zip archive"},
    )
    assert not (tmp_path / "Report-2026").exists()
    assert _saved_files(tmp_path) == set()


def test_a_save_that_fails_part_way_leaves_nothing(tmp_path):
    """DEF-PEER-53: the items already written are removed when a later item
    is not in the peer's manifest."""
    manifest = {"slug": "Report-2026", "entries": [{"name": "notes.txt", "type": "file"}]}
    peer = {"notes.txt": _PeerResponse(200, b"hi")}
    handler, key = _save_handler(
        tmp_path, {"target_dir": "", "names": ["notes.txt", "gone.txt"]}, manifest, peer=peer
    )
    asyncio.run(handler.post(key))
    assert (handler.status, handler.payload) == (404, {"error": "Not in share: gone.txt"})
    assert _saved_files(tmp_path) == set()


async def _closes_at_once(reader, writer):
    """A peer that closes the connection without answering."""
    return


@pytest.mark.parametrize(
    "answer, error",
    [
        (_silent, "The peer did not answer within 0.3 s"),
        (_closes_at_once, "The peer closed the connection before the upload finished"),
    ],
)
def test_an_upload_that_fails_says_why(tmp_path, monkeypatch, answer, error):
    """DEF-PEER-51: a timeout and a closed connection become a
    PeerUnavailable the handler maps to 502; both used to answer 500."""
    monkeypatch.setattr(routes, "PEER_UPLOAD_SECONDS_PER_GB", 0.3, raising=False)
    path = tmp_path / "a.txt"
    path.write_bytes(b"hi")

    async def run():
        server, link = await _serve_peer(answer)
        client = tornado.httpclient.AsyncHTTPClient()
        try:
            await routes._post_file(client, link + "/upload", path, "a.txt")
        finally:
            server.close()

    with pytest.raises(routes.PeerUnavailable, match="^" + re.escape(error) + "$"):
        asyncio.run(run())


# --------------------------------------------------------------------------- #
# An upload is sent from disk, as the raw body under X-Filename
# --------------------------------------------------------------------------- #


async def _serve_receiver(received):
    """A loopback peer taking the raw-body upload shape: every POST is
    appended to ``received`` as the decoded X-Filename, the headers, the
    query's uploader and the body chunks as they arrived; the answer mints
    an uploader hash. Returns the server and the request link."""

    @tornado.web.stream_request_body
    class Upload(tornado.web.RequestHandler):
        def prepare(self):
            self.chunks = []

        def data_received(self, chunk):
            self.chunks.append(bytes(chunk))

        def post(self, _id):
            received.append(
                {
                    "name": unquote(self.request.headers["X-Filename"]),
                    "headers": dict(self.request.headers),
                    "uploader": self.get_argument("uploader", ""),
                    "chunks": self.chunks,
                }
            )
            self.write({"ok": True, "me": {"hash": "ABCDEFGH"}})

    server = tornado.httpserver.HTTPServer(tornado.web.Application([(r"/public/request/(\w+)/upload", Upload)]))
    socks = tornado.netutil.bind_sockets(0, "127.0.0.1")
    server.add_sockets(socks)
    return server, f"http://127.0.0.1:{socks[0].getsockname()[1]}/public/request/RRRR22"


def _upload_handler(workspace, body, link):
    """ConnectionUploadHandler for a stored request connection, the real
    peer fetch against ``link``."""
    return _stub_handler(routes.ConnectionUploadHandler, workspace, body, "request", "RRRR22", link)


def test_an_upload_reaches_the_peer_as_the_raw_body_under_its_name(tmp_path, monkeypatch):
    """A 3 MiB file arrives as exactly its bytes with its length announced
    and its name decoded from X-Filename, in more than one chunk; the unlock
    header rides along."""
    monkeypatch.setattr(routes, "_EXTRACT_CHUNK_BYTES", 1024 * 1024, raising=False)
    data = os.urandom(3 * 1024 * 1024)
    path = tmp_path / "big.bin"
    path.write_bytes(data)
    received = []

    async def run():
        server, link = await _serve_receiver(received)
        client = tornado.httpclient.AsyncHTTPClient()
        try:
            return await routes._post_file(
                client, link + "/upload?uploader=Ann", path, "Ordner/big.bin", headers={"X-Share-Token": "tok"}
            )
        finally:
            server.stop()

    body = asyncio.run(run())
    assert json.loads(body) == {"ok": True, "me": {"hash": "ABCDEFGH"}}
    (upload,) = received
    assert upload["name"] == "Ordner/big.bin" and upload["uploader"] == "Ann"
    assert upload["headers"]["Content-Length"] == str(len(data))
    assert upload["headers"]["Content-Type"] == "application/octet-stream"
    assert upload["headers"]["X-Share-Token"] == "tok"
    assert len(upload["chunks"]) > 1 and b"".join(upload["chunks"]) == data


def test_a_folder_upload_sends_each_file_from_disk_under_its_path(tmp_path):
    """The handler hands the walk's paths to the sender; a non-ASCII folder
    path round-trips through X-Filename, and the hash the peer mints on the
    first upload rides the next as the Cookie."""
    workspace = tmp_path / "ws"
    (workspace / "Ordner ü" / "sub").mkdir(parents=True)
    (workspace / "Ordner ü" / "a.bin").write_bytes(b"\x00\x01")
    (workspace / "Ordner ü" / "sub" / "café.txt").write_bytes("café".encode())
    received = []

    async def run():
        server, link = await _serve_receiver(received)
        handler, key = _upload_handler(workspace, {"paths": ["Ordner ü"], "uploader": "Ann"}, link)
        try:
            await handler.post(key)
        finally:
            server.stop()
        return handler

    handler = asyncio.run(run())
    assert handler.status == 200, handler.payload
    assert sorted(handler.payload["uploaded"]) == ["Ordner ü/a.bin", "Ordner ü/sub/café.txt"]
    assert {(u["name"], b"".join(u["chunks"])) for u in received} == {
        ("Ordner ü/a.bin", b"\x00\x01"),
        ("Ordner ü/sub/café.txt", "café".encode()),
    }
    assert [u["headers"].get("Cookie") for u in received] == [None, "sf_uploader_RRRR22=ABCDEFGH"]


class _RecordingClient:
    """A client that records the fetch and drives the body producer."""

    def __init__(self):
        self.kwargs = None
        self.writes = []

    async def fetch(self, url, **kwargs):
        self.kwargs = kwargs

        async def write(chunk):
            self.writes.append(len(chunk))

        await kwargs["body_producer"](write)
        return _PeerResponse(200, b"{}")


@pytest.mark.parametrize("size, seconds", [(1024, 300), (2 * routes.GB, 600)])
def test_the_upload_timeout_scales_with_the_file(tmp_path, monkeypatch, size, seconds):
    """PEER_UPLOAD_SECONDS_PER_GB for every GB the file's stat reports, and
    at least for one."""
    path = tmp_path / "a.bin"
    path.write_bytes(b"x" * 1024)
    stat = Path.stat
    monkeypatch.setattr(
        Path, "stat", lambda self, **kw: types.SimpleNamespace(st_size=size) if self == path else stat(self, **kw)
    )
    client = _RecordingClient()
    asyncio.run(routes._post_file(client, "http://peer/upload", path, "a.bin"))
    assert client.kwargs["request_timeout"] == seconds
    assert client.kwargs["connect_timeout"] == routes.PEER_TIMEOUT_SECONDS
    assert client.kwargs["headers"]["Content-Length"] == str(size)


def test_the_body_is_read_from_disk_in_chunks_as_it_is_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_EXTRACT_CHUNK_BYTES", 4, raising=False)
    path = tmp_path / "a.bin"
    path.write_bytes(b"0123456789")
    client = _RecordingClient()
    asyncio.run(routes._post_file(client, "http://peer/upload", path, "a.bin"))
    assert client.writes == [4, 4, 2]
    assert client.kwargs["headers"]["X-Filename"] == "a.bin"


# --------------------------------------------------------------------------- #
# The save boundary: a peer answer the lab cannot use, and a workspace that
# refuses the write
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[1, 2]", b'{"slug": "x", "entries": "nope"}', b'{"entries": [1]}', b'{"entries": [{}]}'],
)
def test_a_manifest_the_lab_cannot_read_answers_502(tmp_path, body):
    """A manifest that is not an object with a list of entry objects answers
    502 with the reason, not 500."""
    handler, key = _save_handler(tmp_path, {"target_dir": ""}, {}, peer={"manifest": _PeerResponse(200, body)})
    asyncio.run(handler.post(key))
    assert (handler.status, handler.payload) == (502, {"error": "The peer did not send a readable manifest"})
    assert _saved_files(tmp_path) == set()


@pytest.mark.parametrize(
    "raised",
    [
        zipfile.BadZipFile("File is not a zip file"),
        RuntimeError("File a.txt is encrypted, password required"),
        NotImplementedError("That compression method is not supported"),
        zlib.error("Error -3 while decompressing data"),
        lzma.LZMAError("Input format not supported by decoder"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ValueError("negative seek value -37"),
    ],
)
def test_an_unreadable_member_answers_502_and_leaves_nothing(tmp_path, monkeypatch, raised):
    """An archive whose bytes the extractor rejects - unreadable, encrypted,
    unknown compression, corrupt deflate - answers 502 through the same
    cleanup as a non-zip, not 500 with the folder left behind."""
    opened = zipfile.ZipFile.open

    def broken(self, name, *args, **kwargs):
        if isinstance(name, zipfile.ZipInfo) and not name.is_dir():
            raise raised
        return opened(self, name, *args, **kwargs)

    # patch after the handler is built: _zip_of writes through ZipFile.open
    handler, key = _save_handler(tmp_path, {"target_dir": ""}, {"slug": "Report-2026", "entries": []})
    monkeypatch.setattr(zipfile.ZipFile, "open", broken)
    asyncio.run(handler.post(key))
    assert (handler.status, handler.payload) == (502, {"error": "The peer did not send a readable zip archive"})
    assert not (tmp_path / "Report-2026").exists()
    assert _saved_files(tmp_path) == set()


def test_a_workspace_that_refuses_the_write_answers_502_and_leaves_nothing(tmp_path, monkeypatch):
    """A disk error mid-save goes through the same cleanup as every other
    failure, not 500."""
    made = Path.mkdir

    def full(self, *args, **kwargs):
        if self.name == "Report-2026":
            raise OSError("No space left on device")
        return made(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", full)
    handler, key = _save_handler(tmp_path, {"target_dir": ""}, {"slug": "Report-2026", "entries": []})
    asyncio.run(handler.post(key))
    assert (handler.status, handler.payload) == (502, {"error": "Could not write the save to the workspace"})
    assert not (tmp_path / "Report-2026").exists()
    assert _saved_files(tmp_path) == set()


def test_a_failed_write_of_the_spool_names_the_workspace_and_leaves_nothing(tmp_path, monkeypatch):
    """A disk error while the download lands on the spool answers the
    workspace reason, not 'could not reach the peer', and nothing is left."""
    real = routes.tempfile.NamedTemporaryFile

    def full(*args, **kwargs):
        spool = real(*args, **kwargs)

        def write(chunk):
            raise OSError(28, "No space left on device")

        spool.write = write
        return spool

    monkeypatch.setattr(routes.tempfile, "NamedTemporaryFile", full)
    handler = asyncio.run(_save_all_from_peer(tmp_path, _sends(_zip_of({"hello.txt": b"hi"}))))
    assert (handler.status, handler.payload) == (502, {"error": "Could not write the save to the workspace"})
    assert _saved_files(tmp_path) == set()
    assert list(_spool_dir(tmp_path).iterdir()) == []


def test_a_central_directory_pointing_nowhere_answers_502(tmp_path):
    """A live archive whose end record points the central directory at the
    wrong place makes zipfile seek before the spool's start (OSError EINVAL)
    - it answers 502 like every other unreadable zip, with nothing left."""
    data = bytearray(_zip_of({"hello.txt": b"hi"}))
    pos = data.rfind(b"PK\x05\x06")  # end of central directory record
    assert pos > 0
    data[pos + 16] ^= 0xFF  # offset of the start of the central directory
    handler, key = _save_handler(
        tmp_path,
        {"target_dir": ""},
        {"slug": "Report-2026", "entries": []},
        peer={"download-all": _PeerResponse(200, bytes(data))},
    )
    asyncio.run(handler.post(key))
    assert (handler.status, handler.payload) == (502, {"error": "The peer did not send a readable zip archive"})
    assert not (tmp_path / "Report-2026").exists()
    assert _saved_files(tmp_path) == set()


def test_a_null_byte_in_an_entry_name_answers_400(tmp_path):
    """A hostile entry name is refused by the name gate as what it is, not
    later as an unreadable zip."""
    manifest = {"slug": "Report-2026", "entries": [{"name": "a\x00b.txt", "type": "file"}]}
    peer = {"a\x00b.txt": _PeerResponse(200, b"hi")}
    handler, key = _save_handler(tmp_path, {"target_dir": "", "names": ["a\x00b.txt"]}, manifest, peer=peer)
    asyncio.run(handler.post(key))
    assert (handler.status, handler.payload) == (400, {"error": "Invalid name: a\x00b.txt"})
    assert _saved_files(tmp_path) == set()


def test_a_stored_link_the_lab_cannot_parse_answers_502_with_the_reach_reason(tmp_path):
    """A corrupt store whose link is not a URL fails the fetch as 'could not
    reach', not as an unreadable zip."""
    handler, key = _save_handler(tmp_path, {"target_dir": ""}, {}, link="not a url")
    del handler._peer_fetch  # the real one, so the fetch itself parses the link
    asyncio.run(handler.post(key))
    # DEF-PEER-57: the short literal phrase, no errno or URL text after it
    assert (handler.status, handler.payload) == (502, {"error": "Could not reach the peer"})
    assert _saved_files(tmp_path) == set()


def test_a_corrupt_bzip2_member_names_the_archive_not_the_workspace(tmp_path):
    """DEF-PEER-68: bz2 surfaces a corrupt member as OSError; the save answers
    the unreadable-zip reason, not 'Could not write the save'."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_BZIP2) as zf:
        zf.writestr("hello.txt", b"hi" * 100)
    data = bytearray(buf.getvalue())
    # the compressed data starts after the local header and the file name
    name_len = int.from_bytes(data[26:28], "little")
    start = 30 + name_len
    for i in range(start, start + 20):
        data[i] ^= 0xFF
    handler, key = _save_handler(
        tmp_path,
        {"target_dir": ""},
        {"slug": "Report-2026", "entries": []},
        peer={"download-all": _PeerResponse(200, bytes(data))},
    )
    asyncio.run(handler.post(key))
    assert (handler.status, handler.payload) == (502, {"error": "The peer did not send a readable zip archive"})
    assert not (tmp_path / "Report-2026").exists()
    assert _saved_files(tmp_path) == set()
