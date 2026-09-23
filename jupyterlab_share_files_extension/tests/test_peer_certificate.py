"""A connected peer whose certificate the system does not trust.

The connect answers the certificate's details until the user trusts it in
the panel's dialog; the connection then keeps that certificate and every
fetch for it - read, save, upload - accepts that one only. Stub-handler
style of test_connection_save.py, against a loopback peer over https that
presents one of two self-signed certificates in ``fixtures/``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
import time
import types
from pathlib import Path

import tornado.httpserver
import tornado.netutil
import tornado.web

from jupyterlab_share_files_extension import routes
from jupyterlab_share_files_extension.config import ShareFilesConfig
from jupyterlab_share_files_extension.storage import ConnectionStore

FIXTURES = Path(__file__).parent / "fixtures"
END = "-----END CERTIFICATE-----"


def _certificate(name: str) -> tuple[str, str]:
    """The fixture's certificate as the lab stores it, and its fingerprint."""
    text = (FIXTURES / f"{name}.pem").read_text()
    der = ssl.PEM_cert_to_DER_cert(text[: text.index(END) + len(END)])
    return ssl.DER_cert_to_PEM_cert(der), hashlib.sha256(der).digest().hex(":").upper()


PEM_A, PRINT_A = _certificate("peer-a")
PEM_B, PRINT_B = _certificate("peer-b")
# a leaf a private authority signed, sent with that authority, and carrying
# no Authority Key Identifier
PEM_C, PRINT_C = _certificate("peer-c")
# a self-signed certificate that expired in 2001
PEM_D, PRINT_D = _certificate("peer-d")


class _Peer:
    """A peer lab over https: a share holding ``a.txt`` behind the password
    ``pw`` and a request that takes uploads, presenting peer-a's certificate
    until ``present`` swaps it. Started inside the test's loop."""

    def __init__(self):
        self.context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.context.load_cert_chain(FIXTURES / "peer-a.pem")
        self.uploads: list[tuple[str, bytes]] = []
        peer = self

        class Share(tornado.web.RequestHandler):
            def get(self, rest):
                if not self.request.headers.get("X-Share-Token", "").endswith(".sig"):
                    return self.send_error(401)
                if rest == "manifest":
                    return self.finish({"id": "QQQQ22", "name": "Theirs", "slug": "theirs", "kind": "share",
                                        "entries": [{"name": "a.txt", "type": "file", "size": 3}]})
                if rest == "download/a.txt":
                    return self.finish(b"abc")
                self.send_error(404)

            def post(self, rest):
                if rest != "unlock" or json.loads(self.request.body).get("password") != "pw":
                    return self.send_error(401)
                self.finish({"token": f"{int(time.time()) + 3600}.sig"})

        class Request(tornado.web.RequestHandler):
            def get(self, rest):
                self.finish({"id": "RRRR22", "name": "Inbox", "kind": "request", "uploads": []})

            def post(self, rest):
                peer.uploads.append((self.request.headers["X-Filename"], self.request.body))
                self.finish({"ok": True, "me": {"hash": "ABCDEFGH"}})

        prefix = "/user/bob/jupyterlab-share-files-extension/public"
        self.server = tornado.httpserver.HTTPServer(
            tornado.web.Application([(prefix + r"/share/QQQQ22/(.*)", Share), (prefix + r"/request/RRRR22/(.*)", Request)]),
            ssl_options=self.context,
        )
        socks = tornado.netutil.bind_sockets(0, "127.0.0.1")
        self.server.add_sockets(socks)
        self.host = f"127.0.0.1:{socks[0].getsockname()[1]}"
        self.share = f"https://{self.host}{prefix}/share/QQQQ22"
        self.request = f"https://{self.host}{prefix}/request/RRRR22"

    def present(self, name: str) -> None:
        self.context.load_cert_chain(FIXTURES / f"{name}.pem")


def _handler(cls, workspace, body=None, verify=True):
    """A handler of ``cls`` with no Tornado server: the JSON body and the
    answer are plain attributes, the peer fetches are real."""
    handler = object.__new__(cls)
    handler.request = types.SimpleNamespace(method="POST", headers={}, protocol="http", host="lab.local:8888")
    handler.application = types.SimpleNamespace(settings={
        "share_files_config": ShareFilesConfig(verify_peer_tls=verify),
        "base_url": "/",
        "server_root_dir": str(workspace),
    })
    handler._current_user = "tester"  # satisfies @tornado.web.authenticated
    handler.get_json_body = lambda: body or {}
    handler.status = 200
    handler.payload = None
    handler.set_status = lambda code: setattr(handler, "status", code)
    handler.write_json = lambda p: setattr(handler, "payload", p)
    handler.set_header = lambda name, value: None

    def _write_error(code, message, reason=""):
        handler.status = code
        handler.payload = {"error": message, "reason": reason} if reason else {"error": message}

    handler.write_error_json = _write_error
    return handler


async def _connect(workspace, link, trust="", verify=True, password=""):
    handler = _handler(routes.ConnectionsHandler, workspace, {"link": link, "trust": trust, "password": password}, verify)
    await handler.post()
    return handler


def _stored(workspace):
    return ConnectionStore(str(workspace)).list()


def test_a_certificate_the_system_does_not_trust_is_shown_before_anything_is_stored(tmp_path):
    async def run():
        peer = _Peer()
        try:
            asked = await _connect(tmp_path, peer.share)
            # a trust that names another certificate trusts nothing
            other = await _connect(tmp_path, peer.share, trust=PRINT_B)
            return peer, asked, other
        finally:
            peer.server.stop()

    peer, asked, other = asyncio.run(run())
    assert asked.status == 502
    assert asked.payload["certificate"] == {
        "host": peer.host, "fingerprint": PRINT_A, "reason": "self-signed certificate"}
    assert (other.status, other.payload["certificate"]["fingerprint"]) == (502, PRINT_A)
    assert _stored(tmp_path) == []


def test_a_trusted_certificate_stays_with_the_connection_for_its_reads_saves_and_uploads(tmp_path):
    (tmp_path / "up.txt").write_text("up")

    async def run():
        peer = _Peer()
        try:
            share = await _connect(tmp_path, peer.share, trust=PRINT_A, password="pw")
            request = await _connect(tmp_path, peer.request, trust=PRINT_A)
            # the read unlocks again, with the certificate too
            routes._PEER_TOKENS.clear()
            read = _handler(routes.ConnectionManifestHandler, tmp_path)
            await read.get(share.payload["key"])
            save = _handler(routes.ConnectionSaveHandler, tmp_path, {"target_dir": "", "names": ["a.txt"]})
            await save.post(share.payload["key"])
            upload = _handler(routes.ConnectionUploadHandler, tmp_path, {"paths": ["up.txt"]})
            await upload.post(request.payload["key"])
            # Connect Again keeps the certificate without asking
            again = await _connect(tmp_path, peer.share, password="pw")
            return peer, share, read, save, upload, again
        finally:
            peer.server.stop()

    peer, share, read, save, upload, again = asyncio.run(run())
    assert share.status == 200 and share.payload["certificate"] == PEM_A
    assert {c["certificate"] for c in _stored(tmp_path)} == {PEM_A}
    assert (read.status, read.payload["name"]) == (200, "Theirs")
    assert save.status == 200 and (tmp_path / "a.txt").read_bytes() == b"abc"
    assert upload.status == 200 and peer.uploads == [("up.txt", b"up")]
    assert (again.status, again.payload["certificate"]) == (200, PEM_A)


def test_a_changed_certificate_stops_the_connection_and_the_next_connect_asks_again(tmp_path):
    async def run():
        peer = _Peer()
        try:
            share = await _connect(tmp_path, peer.share, trust=PRINT_A, password="pw")
            peer.present("peer-b")
            read = _handler(routes.ConnectionManifestHandler, tmp_path)
            await read.get(share.payload["key"])
            # Connect Again sends no password: the connection's own is kept
            asked = await _connect(tmp_path, peer.share)
            trusted = await _connect(tmp_path, peer.share, trust=PRINT_B)
            return read, asked, trusted
        finally:
            peer.server.stop()

    read, asked, trusted = asyncio.run(run())
    assert (read.status, read.payload["reason"]) == (502, "certificate_untrusted")
    assert "Connect Again" in read.payload["error"]
    assert (asked.status, asked.payload["certificate"]["fingerprint"]) == (502, PRINT_B)
    assert trusted.status == 200
    assert [(c["certificate"], c["password"]) for c in _stored(tmp_path)] == [(PEM_B, "pw")]


def test_a_trusted_certificate_a_private_authority_signed_serves_the_connection(tmp_path):
    async def run():
        peer = _Peer()
        peer.present("peer-c")
        try:
            asked = await _connect(tmp_path, peer.share, password="pw")
            share = await _connect(tmp_path, peer.share, trust=PRINT_C, password="pw")
            routes._PEER_TOKENS.clear()
            read = _handler(routes.ConnectionManifestHandler, tmp_path)
            await read.get(share.payload["key"])
            return asked, share, read
        finally:
            peer.server.stop()

    asked, share, read = asyncio.run(run())
    assert (asked.status, asked.payload["certificate"]["fingerprint"]) == (502, PRINT_C)
    assert (share.status, share.payload["certificate"]) == (200, PEM_C)
    assert (read.status, read.payload["name"]) == (200, "Theirs")


def test_an_expired_certificate_is_named_at_connect_and_never_offered_for_trust(tmp_path):
    async def run():
        peer = _Peer()
        peer.present("peer-d")
        try:
            asked = await _connect(tmp_path, peer.share, password="pw")
            trusted = await _connect(tmp_path, peer.share, trust=PRINT_D, password="pw")
            return peer, asked, trusted
        finally:
            peer.server.stop()

    peer, asked, trusted = asyncio.run(run())
    # the dialog would offer a trust the lab could not honour
    assert (asked.status, asked.payload) == (
        502, {"error": f"{peer.host} presents a certificate that cannot be used (certificate has expired)"})
    # no row exists yet: the sentence names the certificate, not a row menu
    assert trusted.status == 502
    assert "cannot be used (certificate has expired)" in trusted.payload["error"]
    assert "Connect Again" not in trusted.payload["error"]
    assert _stored(tmp_path) == []


def test_with_verify_peer_tls_off_nothing_is_asked_or_kept(tmp_path):
    async def run():
        peer = _Peer()
        try:
            return await _connect(tmp_path, peer.share, verify=False, password="pw")
        finally:
            peer.server.stop()

    handler = asyncio.run(run())
    assert handler.status == 200
    assert "certificate" not in _stored(tmp_path)[0]


def test_a_host_that_does_not_answer_is_said_so_at_connect(tmp_path):
    socks = tornado.netutil.bind_sockets(0, "127.0.0.1")
    port = socks[0].getsockname()[1]
    socks[0].close()
    link = f"https://127.0.0.1:{port}/user/bob/jupyterlab-share-files-extension/public/share/QQQQ22"
    handler = asyncio.run(_connect(tmp_path, link))
    assert (handler.status, handler.payload) == (502, {"error": "Could not reach the peer"})
