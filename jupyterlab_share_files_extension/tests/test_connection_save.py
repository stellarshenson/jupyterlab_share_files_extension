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
import re
import lzma
import zipfile
import zlib
from pathlib import Path

import pytest
import tornado.httpclient

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


def _save_handler(workspace, body, manifest, files=None, peer=None, link=PEER_LINK):
    """ConnectionSaveHandler for a stored share connection, peer stubbed.

    ``files`` is the Save All zip, ``peer`` adds answers by the last path
    component of the fetched url."""
    entry = ConnectionStore(str(workspace)).add("share", "QQQQ22", PEER_HOST, link=link)
    handler = object.__new__(routes.ConnectionSaveHandler)
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

    def _write_error(code, message):
        handler.status = code
        handler.payload = {"error": message}

    handler.write_error_json = _write_error
    responses = {
        "manifest": _PeerResponse(200, json.dumps(manifest).encode()),
        "download-all": _PeerResponse(200, _zip_of(files or {"hello.txt": b"hi"})),
        **(peer or {}),
    }

    async def _peer_fetch(url, **kwargs):
        return responses[url.rsplit("/", 1)[-1]]

    handler._peer_fetch = _peer_fetch
    return handler, entry["key"]


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


async def _announces_2_gib(reader, writer):
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % (2 * 1024**3))
    await writer.drain()
    await reader.read()  # until the lab server closes the connection


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
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/public/share/QQQQ22"


async def _save_all_from_peer(workspace, answer):
    """Save All through the real peer fetch from a loopback peer."""
    server, link = await _serve_peer(answer)
    handler, key = _save_handler(workspace, {"target_dir": ""}, {}, link=link)
    del handler._peer_fetch
    await handler.post(key)
    server.close()
    return handler


def test_the_limits_are_the_chosen_ones():
    assert routes.PEER_TRANSFER_TIMEOUT_SECONDS == 300
    assert routes.PEER_SAVE_MAX_BYTES == 1024**3


def test_save_answers_502_when_the_download_passes_its_time_limit(tmp_path, monkeypatch):
    """DEF-PEER-36: past PEER_TRANSFER_TIMEOUT_SECONDS the save answers 502
    with the reason, not 500."""
    monkeypatch.setattr(routes, "PEER_TRANSFER_TIMEOUT_SECONDS", 0.3, raising=False)
    handler = asyncio.run(_save_all_from_peer(tmp_path, _silent))
    assert (handler.status, handler.payload) == (502, {"error": "The peer did not answer within 0.3 s"})
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
    "answer, error",
    [
        (_closes_mid_download, "The peer closed the connection before the download finished"),
        (_announces_2_gib, "The peer's download is larger than the 1 GiB limit for one save"),
    ],
)
def test_save_answers_502_when_the_download_breaks_off(tmp_path, answer, error):
    handler = asyncio.run(_save_all_from_peer(tmp_path, answer))
    assert (handler.status, handler.payload) == (502, {"error": error})
    assert not (tmp_path / "Report-2026").exists()


def _saved_files(root):
    # connections.json is the store's own write, not the save
    return {p for p in _files_under(root) if p.name != "connections.json"}


def test_save_all_stops_past_the_unpacked_size_limit_and_leaves_nothing(tmp_path, monkeypatch):
    """DEF-PEER-37: past PEER_SAVE_MAX_BYTES the save stops, says why and
    removes what it wrote."""
    monkeypatch.setattr(routes, "PEER_SAVE_MAX_BYTES", 10, raising=False)
    files = {"a.txt": b"12345678", "sub/b.txt": b"12345678"}
    handler, key = _save_handler(tmp_path, {"target_dir": ""}, {"slug": "Report-2026", "entries": []}, files=files)
    asyncio.run(handler.post(key))
    assert handler.status == 502
    assert re.fullmatch(r"The share is larger than \S+ GiB when unpacked", handler.payload["error"])
    assert not (tmp_path / "Report-2026").exists()
    assert _saved_files(tmp_path) == set()


def test_a_save_of_selected_items_counts_them_all_against_the_limit(tmp_path, monkeypatch):
    """The limit is for the whole save: a file under it and a folder that
    passes it together are both removed."""
    monkeypatch.setattr(routes, "PEER_SAVE_MAX_BYTES", 10, raising=False)
    manifest = {"slug": "Report-2026", "entries": [{"name": "notes.txt", "type": "file"}, {"name": "one", "type": "directory"}]}
    peer = {
        "notes.txt": _PeerResponse(200, b"12345678"),
        "one": _PeerResponse(200, _zip_of({"one/c.txt": b"12345678"})),
    }
    handler, key = _save_handler(tmp_path, {"target_dir": "", "names": ["notes.txt", "one"]}, manifest, peer=peer)
    asyncio.run(handler.post(key))
    assert handler.status == 502
    assert re.fullmatch(r"The share is larger than \S+ GiB when unpacked", handler.payload["error"])
    assert not (tmp_path / "notes.txt").exists()
    assert not (tmp_path / "one").exists()
    assert _saved_files(tmp_path) == set()


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
    monkeypatch.setattr(routes, "PEER_TRANSFER_TIMEOUT_SECONDS", 0.3, raising=False)

    async def run():
        server, link = await _serve_peer(answer)
        client = tornado.httpclient.AsyncHTTPClient()
        try:
            await routes._post_file(client, link + "/upload", "a.txt", b"hi")
        finally:
            server.close()

    with pytest.raises(routes.PeerUnavailable, match="^" + re.escape(error) + "$"):
        asyncio.run(run())


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


def test_a_failed_write_of_a_plain_file_leaves_nothing(tmp_path, monkeypatch):
    """A disk error during the write of a selected plain file removes the
    partial file - the target is registered before the write, as a folder is."""
    real_open = open

    class _Full:
        def __init__(self, f):
            self._f = f

        def write(self, data):
            raise OSError("No space left on device")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._f.close()

    def opening(path, mode="r", *args, **kwargs):
        if mode == "wb":
            return _Full(real_open(path, mode, *args, **kwargs))
        return real_open(path, mode, *args, **kwargs)

    manifest = {"slug": "Report-2026", "entries": [{"name": "notes.txt", "type": "file"}]}
    peer = {"notes.txt": _PeerResponse(200, b"hi")}
    handler, key = _save_handler(tmp_path, {"target_dir": "", "names": ["notes.txt"]}, manifest, peer=peer)
    monkeypatch.setattr("builtins.open", opening)
    asyncio.run(handler.post(key))
    assert (handler.status, handler.payload) == (502, {"error": "Could not write the save to the workspace"})
    assert _saved_files(tmp_path) == set()


def test_a_central_directory_pointing_nowhere_answers_502(tmp_path):
    """A live archive whose end record points the central directory at the
    wrong place raises ValueError from zipfile's seek - it answers 502 like
    every other unreadable zip, with nothing left."""
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
