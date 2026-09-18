"""The public transfer endpoints move bytes through disk, never memory.

A download flushes chunk by chunk and a zip is streamed as it is built; a
recipient's upload is spooled under the store's ``tmp`` folder as it
arrives and moved into the request once complete. The real handlers on a
loopback tornado server over a temporary store.
"""

from __future__ import annotations

import asyncio
import errno
import io
import json
import os
import types
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse

import pytest
import tornado.httpclient
import tornado.httpserver
import tornado.web
from tornado.testing import bind_unused_port

from jupyterlab_share_files_extension import routes
from jupyterlab_share_files_extension.config import ShareFilesConfig
from jupyterlab_share_files_extension.storage import RequestStore, ShareStore

NS = "jupyterlab-share-files-extension"


@pytest.fixture(autouse=True)
def _standalone(monkeypatch):
    # the public routes are mounted in the standalone zone only
    monkeypatch.delenv("SHARE_FILES_PUBLIC_ZONE", raising=False)


def _serve(workspace):
    """The extension's routes on a loopback port over ``workspace``; the
    server reads bodies in 1 KiB pieces so a few KiB arrive in several."""
    app = tornado.web.Application(base_url="/", server_root_dir=str(workspace))
    routes.setup_route_handlers(app, ShareFilesConfig())
    sock, port = bind_unused_port()
    server = tornado.httpserver.HTTPServer(app, chunk_size=1024)
    server.add_socket(sock)
    return server, f"http://127.0.0.1:{port}/{NS}/public"


def _share(workspace, files: dict[str, bytes], sources: list[str]) -> str:
    for rel, data in files.items():
        (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
        (workspace / rel).write_bytes(data)
    return ShareStore(str(workspace)).create("Report", sources)["id"]


def _count_flushes(monkeypatch, cls):
    flushes = []
    real = cls.flush

    def flush(self, *args, **kwargs):
        flushes.append(1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(cls, "flush", flush)
    return flushes


async def _get(workspace, path, **fetch):
    server, base = _serve(workspace)
    try:
        return await tornado.httpclient.AsyncHTTPClient().fetch(base + path, raise_error=False, **fetch)
    finally:
        server.stop()


def _members(body: bytes) -> dict[str, bytes]:
    zf = zipfile.ZipFile(io.BytesIO(body))
    assert zf.testzip() is None
    return {name: zf.read(name) for name in zf.namelist()}


def test_a_file_download_is_flushed_after_every_chunk(tmp_path, monkeypatch):
    """Without the flush tornado keeps the whole file in its write buffer
    until finish()."""
    monkeypatch.setattr(routes, "_EXTRACT_CHUNK_BYTES", 1000)
    flushes = _count_flushes(monkeypatch, routes.PublicShareDownloadHandler)
    data = bytes(range(256)) * 20  # 5120 bytes: six chunks of 1000
    id_ = _share(tmp_path, {"blob.bin": data}, ["blob.bin"])
    resp = asyncio.run(_get(tmp_path, f"/share/{id_}/download/blob.bin"))
    assert resp.code == 200
    assert resp.body == data
    assert resp.headers["Content-Length"] == str(len(data))
    assert resp.headers["Content-Disposition"] == "attachment; filename*=UTF-8''blob.bin"
    # one per chunk, and finish()'s own
    assert len(flushes) == 6 + 1


def test_a_file_named_outside_latin_1_downloads_with_its_name_percent_encoded(tmp_path):
    """tornado refuses a header value with a character above U+00FF, so a
    plain filename= answered an HTML 500 for a Polish or CJK name."""
    id_ = _share(tmp_path, {"raport_ż.txt": b"ok"}, ["raport_ż.txt"])
    resp = asyncio.run(_get(tmp_path, f"/share/{id_}/download/raport_%C5%BC.txt"))
    assert resp.code == 200
    assert resp.body == b"ok"
    assert resp.headers["Content-Disposition"] == "attachment; filename*=UTF-8''raport_%C5%BC.txt"


def test_a_folder_entry_downloads_as_a_streamed_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_EXTRACT_CHUNK_BYTES", 8)
    flushes = _count_flushes(monkeypatch, routes.PublicShareDownloadHandler)
    files = {"docs/b.txt": b"b" * 40, "docs/sub/c.txt": b"c" * 40}
    id_ = _share(tmp_path, files, ["docs"])
    resp = asyncio.run(_get(tmp_path, f"/share/{id_}/download/docs"))
    assert resp.code == 200
    assert resp.headers["Content-Type"] == "application/zip"
    assert resp.headers["Content-Disposition"] == "attachment; filename*=UTF-8''docs.zip"
    assert "Content-Length" not in resp.headers
    assert _members(resp.body) == files
    assert len(flushes) > 1


def test_download_all_is_a_streamed_zip_of_bare_names(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "_EXTRACT_CHUNK_BYTES", 8)
    flushes = _count_flushes(monkeypatch, routes.PublicShareDownloadAllHandler)
    files = {"a.txt": b"a" * 40, "docs/b.txt": b"b" * 40}
    id_ = _share(tmp_path, files, ["a.txt", "docs"])
    resp = asyncio.run(_get(tmp_path, f"/share/{id_}/download-all"))
    assert resp.code == 200
    assert resp.headers["Content-Disposition"] == "attachment; filename*=UTF-8''Report.zip"
    assert "Content-Length" not in resp.headers
    assert _members(resp.body) == files
    assert len(flushes) > 1


def test_a_member_that_vanishes_under_the_zip_closes_the_connection(tmp_path, monkeypatch):
    """An item the owner removes while a recipient downloads the folder -
    here a link whose target is gone, walked after the first member - ends
    the download without the chunked terminator, so the browser marks it
    failed instead of saving the cut archive as complete."""
    monkeypatch.setattr(routes, "_EXTRACT_CHUNK_BYTES", 8)
    id_ = _share(tmp_path, {"docs/a.txt": b"a" * 40}, ["docs"])
    sub = ShareStore(str(tmp_path)).resolve_data_path(id_, "docs") / "sub"
    sub.mkdir()
    (sub / "b.txt").symlink_to(sub / "gone")
    lines, chunks = [], []
    with pytest.raises(routes.HTTPStreamClosedError):
        asyncio.run(
            _get(
                tmp_path,
                f"/share/{id_}/download/docs",
                header_callback=lines.append,
                streaming_callback=chunks.append,
            )
        )
    # the first member was out when the link failed
    assert lines[0].startswith("HTTP/1.1 200")
    assert chunks


def test_a_folder_whose_first_member_fails_is_answered_with_a_500(tmp_path):
    """No byte is out when the first walked member cannot be read - here the
    folder's only member is a link whose target is gone - so a closed
    connection would leave the browser an empty answer with no reason; the
    status line names the failure instead."""
    id_ = _share(tmp_path, {"docs/a.txt": b"a" * 40}, ["docs"])
    docs = ShareStore(str(tmp_path)).resolve_data_path(id_, "docs")
    (docs / "a.txt").unlink()
    (docs / "a.txt").symlink_to(docs / "gone")
    resp = asyncio.run(_get(tmp_path, f"/share/{id_}/download/docs"))
    assert (resp.code, json.loads(resp.body)) == (500, {"error": "Could not read the folder"})
    # the sentence, not an attachment named after the folder
    assert "Content-Disposition" not in resp.headers
    assert resp.headers["Content-Type"] == "application/json"


def test_a_member_dated_outside_the_zip_range_lands_with_the_date_clamped(tmp_path):
    """zip carries dates from 1980 to 2107: a member dated outside them (an
    epoch-zero restore, a clock set wrong) is zipped with its date clamped,
    as every archiver does, instead of failing the whole folder."""
    id_ = _share(tmp_path, {"docs/old.txt": b"o" * 40, "docs/far.txt": b"f" * 40}, ["docs"])
    docs = ShareStore(str(tmp_path)).resolve_data_path(id_, "docs")
    os.utime(docs / "old.txt", (0, 0))
    os.utime(docs / "far.txt", (7_258_118_400, 7_258_118_400))  # 2200-01-01
    resp = asyncio.run(_get(tmp_path, f"/share/{id_}/download/docs"))
    assert resp.code == 200
    assert _members(resp.body) == {"docs/old.txt": b"o" * 40, "docs/far.txt": b"f" * 40}
    dates = {i.filename: i.date_time for i in zipfile.ZipFile(io.BytesIO(resp.body)).infolist()}
    assert dates["docs/old.txt"] == (1980, 1, 1, 0, 0, 0)
    assert dates["docs/far.txt"][0] == 2107


def test_a_folder_whose_first_member_has_a_non_utf8_name_is_answered_with_a_500(tmp_path):
    """zipfile refuses a name it cannot encode with a ValueError, not an
    OSError; the answer is still the sentence, not tornado's HTML page."""
    id_ = _share(tmp_path, {"docs/keep": b""}, ["docs"])
    docs = ShareStore(str(tmp_path)).resolve_data_path(id_, "docs")
    (docs / "keep").unlink()
    with open(os.path.join(os.fsencode(docs), b"caf\xe9.txt"), "wb") as f:
        f.write(b"c" * 40)
    resp = asyncio.run(_get(tmp_path, f"/share/{id_}/download/docs"))
    assert (resp.code, json.loads(resp.body)) == (500, {"error": "Could not read the folder"})
    assert resp.headers["Content-Type"] == "application/json"


def test_a_share_holding_an_empty_file_still_zips(tmp_path):
    id_ = _share(tmp_path, {"empty.bin": b"", "docs/e.bin": b""}, ["empty.bin", "docs"])
    resp = asyncio.run(_get(tmp_path, f"/share/{id_}/download-all"))
    assert resp.code == 200
    assert _members(resp.body) == {"empty.bin": b"", "docs/e.bin": b""}


# --------------------------------------------------------------------------- #
# Uploads from a recipient
# --------------------------------------------------------------------------- #


def _request(workspace) -> str:
    return RequestStore(str(workspace)).create("Inbox")["id"]


def _uploads(workspace, id_) -> dict[str, bytes]:
    """Every file under the request's uploader pools, by path."""
    root = RequestStore(str(workspace))._path_for(id_)
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p.name != ".uploader.json"
    }


def _spool_dir(workspace):
    return workspace / "uploads" / "tmp"


async def _post(workspace, id_, headers, body=None, body_producer=None):
    server, base = _serve(workspace)
    try:
        return await tornado.httpclient.AsyncHTTPClient().fetch(
            f"{base}/request/{id_}/upload?uploader=alice",
            method="POST",
            headers=headers,
            body=body,
            body_producer=body_producer,
            raise_error=False,
        )
    finally:
        server.stop()


def test_a_chunked_upload_lands_with_its_bytes_and_name(tmp_path):
    """No Content-Length: three chunks through the client's body producer."""
    parts = [b"a" * 1500, b"b" * 1500, b"c" * 1500]

    async def produce(write):
        for part in parts:
            await write(part)

    id_ = _request(tmp_path)
    resp = asyncio.run(_post(tmp_path, id_, {"X-Filename": "answer.py"}, body_producer=produce))
    assert resp.code == 200
    payload = json.loads(resp.body)
    assert payload["ok"] is True and payload["count"] == 1
    assert payload["me"]["name"] == "alice"
    assert _uploads(tmp_path, id_) == {f"{payload['me']['hash']}/answer.py": b"".join(parts)}
    assert list(_spool_dir(tmp_path).iterdir()) == []


def test_a_folder_path_with_a_non_ascii_name_round_trips(tmp_path, monkeypatch):
    """X-Filename is percent-encoded UTF-8; the store receives it decoded."""
    names = []
    real = RequestStore.add_upload

    def spy(self, id_, uploader_hash, uploader_name, filename, data):
        names.append(filename)
        return real(self, id_, uploader_hash, uploader_name, filename, data)

    monkeypatch.setattr(RequestStore, "add_upload", spy)
    id_ = _request(tmp_path)
    resp = asyncio.run(_post(tmp_path, id_, {"X-Filename": quote("results/café.txt")}, body=b"x"))
    assert resp.code == 200
    assert names == ["results/café.txt"]
    hash_ = json.loads(resp.body)["me"]["hash"]
    # the store sanitises each component as it does today
    assert _uploads(tmp_path, id_) == {f"{hash_}/results/caf-.txt": b"x"}


@pytest.mark.parametrize("headers", [{}, {"X-Filename": "%FF"}])
def test_an_upload_without_a_usable_name_is_no_file(tmp_path, headers):
    id_ = _request(tmp_path)
    resp = asyncio.run(_post(tmp_path, id_, headers, body=b"x"))
    assert (resp.code, json.loads(resp.body)) == (400, {"error": "no file"})
    assert _uploads(tmp_path, id_) == {}


def test_a_body_larger_than_the_free_space_is_refused_before_it_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(routes.shutil, "disk_usage", lambda path: types.SimpleNamespace(free=10))
    writes = []
    real = routes.tempfile.NamedTemporaryFile

    def spy(*args, **kwargs):
        spool = real(*args, **kwargs)
        writes.append(spool.name)
        return spool

    monkeypatch.setattr(routes.tempfile, "NamedTemporaryFile", spy)
    id_ = _request(tmp_path)
    resp = asyncio.run(_post(tmp_path, id_, {"X-Filename": "big.bin"}, body=b"x" * 100))
    assert (resp.code, json.loads(resp.body)) == (413, {"error": "Not enough space"})
    assert writes == []
    assert list(_spool_dir(tmp_path).iterdir()) == []
    assert _uploads(tmp_path, id_) == {}


def test_a_disk_that_fills_mid_upload_is_answered_at_once(tmp_path, monkeypatch):
    """The 500 goes out on the chunk the spool refuses, not after the rest
    of the body was read and discarded: tornado closes the connection under
    it, and the recipient's upload stops."""
    delivered = []
    real_open = routes.tempfile.NamedTemporaryFile

    def spy(*args, **kwargs):
        spool = real_open(*args, **kwargs)
        write = spool.write

        def refuse_past_2k(chunk):
            if sum(delivered) >= 2048:
                raise OSError(errno.ENOSPC, "No space left on device")
            delivered.append(len(chunk))
            return write(chunk)

        spool.write = refuse_past_2k
        return spool

    monkeypatch.setattr(routes.tempfile, "NamedTemporaryFile", spy)
    handed = []
    real_received = routes.PublicRequestUploadHandler.data_received

    def data_received(self, chunk):
        handed.append(len(chunk))
        return real_received(self, chunk)

    monkeypatch.setattr(routes.PublicRequestUploadHandler, "data_received", data_received)
    id_ = _request(tmp_path)
    resp = asyncio.run(_post(tmp_path, id_, {"X-Filename": "big.bin"}, body=b"x" * 200_000))
    assert (resp.code, json.loads(resp.body)) == (500, {"error": "Could not store the upload"})
    # tornado stopped handing chunks over at the answer, not at the body's end
    assert 2048 <= sum(delivered) < sum(handed) < 10_000
    assert list(_spool_dir(tmp_path).iterdir()) == []
    assert _uploads(tmp_path, id_) == {}


def test_a_client_that_drops_mid_body_leaves_nothing_behind(tmp_path, monkeypatch):
    opened = []
    real_open = routes.tempfile.NamedTemporaryFile

    def spy(*args, **kwargs):
        spool = real_open(*args, **kwargs)
        opened.append(Path(spool.name))
        return spool

    monkeypatch.setattr(routes.tempfile, "NamedTemporaryFile", spy)
    closed = asyncio.Event()
    real_close = routes.PublicRequestUploadHandler.on_connection_close

    def on_connection_close(self):
        real_close(self)
        closed.set()

    monkeypatch.setattr(routes.PublicRequestUploadHandler, "on_connection_close", on_connection_close)
    id_ = _request(tmp_path)

    async def scenario():
        server, base = _serve(tmp_path)
        url = urlparse(base)
        reader, writer = await asyncio.open_connection(url.hostname, url.port)
        writer.write(
            f"POST {url.path}/request/{id_}/upload HTTP/1.1\r\nHost: lab\r\n"
            "X-Filename: big.bin\r\nContent-Length: 100000\r\n\r\n".encode() + b"x" * 3000
        )
        await writer.drain()
        writer.close()
        await asyncio.wait_for(closed.wait(), 5)
        server.stop()

    asyncio.run(scenario())
    assert len(opened) == 1 and not opened[0].exists()
    assert list(_spool_dir(tmp_path).iterdir()) == []
    assert _uploads(tmp_path, id_) == {}


def test_a_first_upload_sets_the_identity_cookie(tmp_path):
    id_ = _request(tmp_path)
    resp = asyncio.run(_post(tmp_path, id_, {"X-Filename": "a.txt"}, body=b"x"))
    assert resp.code == 200
    hash_ = json.loads(resp.body)["me"]["hash"]
    cookies = resp.headers.get_list("Set-Cookie")
    assert any(c.startswith(f"sf_uploader_{id_}={hash_};") for c in cookies), cookies


def test_the_body_reaches_disk_as_it_arrives(tmp_path, monkeypatch):
    """The spool sees several writes before post() runs - the body is not
    collected first."""
    writes = []
    real_open = routes.tempfile.NamedTemporaryFile

    def spy(*args, **kwargs):
        spool = real_open(*args, **kwargs)
        write = spool.write
        spool.write = lambda chunk: writes.append(len(chunk)) or write(chunk)
        return spool

    monkeypatch.setattr(routes.tempfile, "NamedTemporaryFile", spy)
    seen_at_post = []
    real_post = routes.PublicRequestUploadHandler.post

    def post(self, id_):
        seen_at_post.append(len(writes))
        return real_post(self, id_)

    monkeypatch.setattr(routes.PublicRequestUploadHandler, "post", post)
    id_ = _request(tmp_path)
    resp = asyncio.run(_post(tmp_path, id_, {"X-Filename": "a.bin"}, body=b"y" * 5000))
    assert resp.code == 200
    assert seen_at_post[0] > 1
    assert sum(writes) == 5000
