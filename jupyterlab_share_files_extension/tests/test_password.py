"""Tests for optional password protection of shares and requests.

Covers the storage layer (password persisted owner-only, stripped from every
client-facing manifest), passphrase generation (xkcdpass), unlock tokens,
the per-resource rate limiter / cooldown, the public password gate, and the
connect-to-protected-share flow (probe + verify + stored peer password).
Follows the stub-handler style of test_public_origin.py - no Tornado server.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from jupyterlab_share_files_extension import routes
from jupyterlab_share_files_extension.config import ShareFilesConfig
from jupyterlab_share_files_extension.storage import (
    ConnectionStore,
    RequestStore,
    ShareStore,
    generate_password,
    verify_password,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "hello.txt").write_text("hi")
    return tmp_path


def _share_store(workspace) -> ShareStore:
    return ShareStore(str(workspace))


def _request_store(workspace) -> RequestStore:
    return RequestStore(str(workspace))


class _FakeRequest:
    def __init__(self, headers=None, body=b"{}"):
        self.headers = headers or {}
        self.body = body
        self.protocol = "http"
        self.host = "hub.local:8000"
        self.method = "POST"


class _GateHandler:
    """Just enough handler surface for _password_gate / _rate_limit_ok."""

    def __init__(self, cfg=None, headers=None, query=None):
        self.request = _FakeRequest(headers=headers)
        self.settings = {
            "share_files_config": cfg or ShareFilesConfig(),
            "base_url": "/",
        }
        self._query = query or {}
        self.status = None
        self.finished = None

    def get_query_argument(self, name, default=""):
        return self._query.get(name, default)

    def set_status(self, code):
        self.status = code

    def set_header(self, *_args):
        pass

    def finish(self, payload=""):
        self.finished = payload


@pytest.fixture(autouse=True)
def fresh_rate_limiter():
    """Each test gets clean rate-limit counters."""
    routes._RATE_STORAGE.reset()
    yield
    routes._RATE_STORAGE.reset()


# --------------------------------------------------------------------------- #
# Storage: password persisted owner-only
# --------------------------------------------------------------------------- #


def test_share_created_with_password_strips_it_from_manifest(workspace):
    store = _share_store(workspace)
    created = store.create("Secret", ["hello.txt"], password="hunter2")
    assert created["has_password"] is True
    assert "password" not in created
    # raw manifest on disk keeps the value (owner-only retrieval)
    assert store.get_password(created["id"]) == "hunter2"
    # list() never leaks it either
    listed = store.list()[0]
    assert listed["has_password"] is True
    assert "password" not in listed


def test_share_without_password_reports_has_password_false(workspace):
    store = _share_store(workspace)
    created = store.create("Open", ["hello.txt"])
    assert created["has_password"] is False
    assert store.get_password(created["id"]) == ""


def test_set_password_changes_and_clears(workspace):
    store = _share_store(workspace)
    share = store.create("Mutable", ["hello.txt"])
    out = store.set_password(share["id"], "first")
    assert out["has_password"] is True
    assert store.get_password(share["id"]) == "first"
    out = store.set_password(share["id"], "second")
    assert store.get_password(share["id"]) == "second"
    out = store.set_password(share["id"], "")
    assert out["has_password"] is False
    assert store.get_password(share["id"]) == ""


def test_request_password_round_trip(workspace):
    store = _request_store(workspace)
    req = store.create("Inbox", password="drop-zone")
    assert req["has_password"] is True
    assert "password" not in req
    assert store.get_password(req["id"]) == "drop-zone"


def test_connection_stores_and_updates_peer_password(workspace):
    store = ConnectionStore(str(workspace))
    entry = store.add(
        "share", "ABC234", "https://peer", link="https://peer/x", password="pw1"
    )
    assert entry["password"] == "pw1"
    # re-connect with a new password updates the stored one
    entry = store.add(
        "share", "ABC234", "https://peer", link="https://peer/x", password="pw2"
    )
    assert entry["password"] == "pw2"
    assert store.get(entry["key"])["password"] == "pw2"


# --------------------------------------------------------------------------- #
# Passphrase generation + verification
# --------------------------------------------------------------------------- #


def test_generate_password_is_xkcd_style():
    pw = generate_password()
    words = pw.split("-")
    assert len(words) == 4
    assert all(w.isalpha() and w.islower() for w in words)
    assert generate_password() != pw  # vanishingly unlikely to collide


def test_verify_password_semantics():
    assert verify_password("secret", "secret") is True
    assert verify_password("secret", "wrong") is False
    # no password stored never matches - not even an empty attempt
    assert verify_password("", "") is False
    assert verify_password("secret", "") is False


# --------------------------------------------------------------------------- #
# Unlock tokens
# --------------------------------------------------------------------------- #


def test_unlock_token_round_trip():
    token = routes._make_unlock_token("ABC234", "pw")
    assert routes._check_unlock_token("ABC234", "pw", token) is True
    assert routes._check_unlock_token("ABC234", "other", token) is False
    assert routes._check_unlock_token("XYZ234", "pw", token) is False
    assert routes._check_unlock_token("ABC234", "pw", "garbage") is False
    assert routes._check_unlock_token("ABC234", "pw", "") is False


def test_unlock_token_expires(monkeypatch):
    token = routes._make_unlock_token("ABC234", "pw")
    future = time.time() + routes._TOKEN_TTL_SECONDS + 60
    monkeypatch.setattr(routes.time, "time", lambda: future)
    assert routes._check_unlock_token("ABC234", "pw", token) is False


def test_password_change_invalidates_outstanding_tokens():
    token = routes._make_unlock_token("ABC234", "old-password")
    # owner changes the password - tokens are HMAC-keyed on it
    assert routes._check_unlock_token("ABC234", "new-password", token) is False


# --------------------------------------------------------------------------- #
# Rate limiting (generous defaults, tunable via config)
# --------------------------------------------------------------------------- #


def test_rate_limit_enforces_per_minute_cap():
    cfg = ShareFilesConfig(
        password_max_attempts_per_minute=3, password_attempt_cooldown_seconds=0
    )
    handler = _GateHandler(cfg=cfg)
    assert routes._rate_limit_ok(handler, "share", "AAAA22") is True
    assert routes._rate_limit_ok(handler, "share", "AAAA22") is True
    assert routes._rate_limit_ok(handler, "share", "AAAA22") is True
    assert routes._rate_limit_ok(handler, "share", "AAAA22") is False


def test_rate_limit_is_per_resource():
    cfg = ShareFilesConfig(
        password_max_attempts_per_minute=1, password_attempt_cooldown_seconds=0
    )
    handler = _GateHandler(cfg=cfg)
    assert routes._rate_limit_ok(handler, "share", "AAAA22") is True
    assert routes._rate_limit_ok(handler, "share", "AAAA22") is False
    # a different share is unaffected
    assert routes._rate_limit_ok(handler, "share", "BBBB22") is True


def test_cooldown_blocks_back_to_back_attempts():
    cfg = ShareFilesConfig(
        password_max_attempts_per_minute=30, password_attempt_cooldown_seconds=5
    )
    handler = _GateHandler(cfg=cfg)
    assert routes._rate_limit_ok(handler, "share", "CCCC22") is True
    assert routes._rate_limit_ok(handler, "share", "CCCC22") is False


def test_default_limits_are_generous():
    cfg = ShareFilesConfig()
    assert cfg.password_max_attempts_per_minute == 30
    assert cfg.password_attempt_cooldown_seconds == 1


# --------------------------------------------------------------------------- #
# Public password gate
# --------------------------------------------------------------------------- #


class _StoreStub:
    def __init__(self, password=""):
        self._password = password

    def get_password(self, _id):
        return self._password

    def exists(self, _id):
        return True


def test_gate_open_when_no_password_set():
    handler = _GateHandler()
    assert routes._password_gate(handler, _StoreStub(""), "AAAA22") is True
    assert handler.status is None


def test_gate_blocks_without_token():
    handler = _GateHandler()
    assert routes._password_gate(handler, _StoreStub("pw"), "AAAA22") is False
    assert handler.status == 401
    assert json.loads(handler.finished)["password_required"] is True


def test_gate_accepts_valid_header_token():
    token = routes._make_unlock_token("AAAA22", "pw")
    handler = _GateHandler(headers={"X-Share-Token": token})
    assert routes._password_gate(handler, _StoreStub("pw"), "AAAA22") is True


def test_gate_accepts_query_token():
    token = routes._make_unlock_token("AAAA22", "pw")
    handler = _GateHandler(query={"t": token})
    assert routes._password_gate(handler, _StoreStub("pw"), "AAAA22") is True


def test_gate_rejects_token_after_password_change():
    token = routes._make_unlock_token("AAAA22", "old")
    handler = _GateHandler(headers={"X-Share-Token": token})
    assert routes._password_gate(handler, _StoreStub("new"), "AAAA22") is False
    assert handler.status == 401


# --------------------------------------------------------------------------- #
# Connecting to a password-protected share (probe + verify + store)
# --------------------------------------------------------------------------- #


class _PeerResponse:
    def __init__(self, code, body=b"{}"):
        self.code = code
        self.body = body


def _connections_handler(workspace, body, peer_responses):
    """ConnectionsHandler with the network and Tornado plumbing stubbed."""
    handler = object.__new__(routes.ConnectionsHandler)
    handler.request = _FakeRequest()
    # RequestHandler.settings is a read-only view of application.settings
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
    handler._json_body = body
    handler.get_json_body = lambda: handler._json_body
    handler.status = 200
    handler.payload = None
    handler.set_status = lambda code: setattr(handler, "status", code)
    handler.write_json = lambda p: setattr(handler, "payload", p)

    def _write_error(code, message):
        handler.status = code
        handler.payload = {"error": message}

    handler.write_error_json = _write_error
    handler.fetched = []

    async def _peer_fetch(url, **kwargs):
        handler.fetched.append((url, kwargs))
        suffix = url.rsplit("/", 1)[-1]
        return peer_responses[suffix]

    handler._peer_fetch = _peer_fetch
    return handler


PEER_LINK = (
    "https://peer.example.com/user/bob/"
    "jupyterlab-share-files-extension/public/share/QQQQ22"
)


def test_connect_to_protected_share_without_password_asks_for_one(workspace):
    handler = _connections_handler(
        workspace,
        {"link": PEER_LINK},
        {"manifest": _PeerResponse(401)},
    )
    asyncio.run(handler.post())
    assert handler.status == 401
    assert handler.payload["password_required"] is True
    # nothing persisted
    assert ConnectionStore(str(workspace)).list() == []


def test_connect_to_protected_share_with_wrong_password_rejected(workspace):
    handler = _connections_handler(
        workspace,
        {"link": PEER_LINK, "password": "nope"},
        {"manifest": _PeerResponse(401), "unlock": _PeerResponse(401)},
    )
    asyncio.run(handler.post())
    assert handler.status == 401
    assert handler.payload["password_required"] is True
    assert ConnectionStore(str(workspace)).list() == []


def test_connect_to_protected_share_with_password_verifies_and_stores(workspace):
    handler = _connections_handler(
        workspace,
        {"link": PEER_LINK, "password": "open-sesame"},
        {
            "manifest": _PeerResponse(401),
            "unlock": _PeerResponse(200, json.dumps({"token": "tok"}).encode()),
        },
    )
    asyncio.run(handler.post())
    assert handler.status == 200
    assert handler.payload["kind"] == "share"
    # the password was verified against the peer's unlock endpoint...
    assert any(url.endswith("/unlock") for url, _ in handler.fetched)
    # ...and persisted with the connection for later save/upload unlocks
    stored = ConnectionStore(str(workspace)).list()[0]
    assert stored["password"] == "open-sesame"


def test_connect_to_open_share_skips_unlock(workspace):
    handler = _connections_handler(
        workspace,
        {"link": PEER_LINK},
        {"manifest": _PeerResponse(200)},
    )
    asyncio.run(handler.post())
    assert handler.status == 200
    assert not any(url.endswith("/unlock") for url, _ in handler.fetched)


def test_connect_rate_limited_peer_maps_to_429(workspace):
    handler = _connections_handler(
        workspace,
        {"link": PEER_LINK, "password": "x"},
        {"manifest": _PeerResponse(401), "unlock": _PeerResponse(429)},
    )
    asyncio.run(handler.post())
    assert handler.status == 429


# --------------------------------------------------------------------------- #
# Peer auth headers for save/upload from protected connections
# --------------------------------------------------------------------------- #


def _base_handler_with_conn(peer_responses):
    handler = object.__new__(routes._Base)
    handler.fetched = []

    async def _peer_fetch(url, **kwargs):
        handler.fetched.append((url, kwargs))
        suffix = url.rsplit("/", 1)[-1]
        return peer_responses[suffix]

    handler._peer_fetch = _peer_fetch
    return handler


def test_peer_auth_headers_unlocks_with_stored_password():
    handler = _base_handler_with_conn(
        {"unlock": _PeerResponse(200, json.dumps({"token": "tok123"}).encode())}
    )
    conn = {"password": "pw", "link": PEER_LINK}
    headers = asyncio.run(handler._peer_auth_headers(conn))
    assert headers == {"X-Share-Token": "tok123"}


def test_peer_auth_headers_skips_unprotected_connection():
    handler = _base_handler_with_conn({})
    headers = asyncio.run(handler._peer_auth_headers({"link": PEER_LINK}))
    assert headers == {}
    assert handler.fetched == []


def test_peer_auth_headers_surfaces_changed_password():
    handler = _base_handler_with_conn({"unlock": _PeerResponse(401)})
    conn = {"password": "stale", "link": PEER_LINK}
    with pytest.raises(routes.PeerUnavailable):
        asyncio.run(handler._peer_auth_headers(conn))


# --------------------------------------------------------------------------- #
# CLI password flags
# --------------------------------------------------------------------------- #


def test_cli_create_share_passes_password(monkeypatch, capsys):
    from jupyterlab_share_files_extension import cli

    sent = {}
    monkeypatch.setattr(
        cli, "_request", lambda m, ep, body=None: sent.update(body or {}) or {}
    )
    assert (
        cli.main(["create-share", "demo", "a.txt", "--password", "hunter2"]) == 0
    )
    assert sent["password"] == "hunter2"


def test_cli_create_share_generate_password(monkeypatch, capsys):
    from jupyterlab_share_files_extension import cli

    calls = []

    def fake_request(method, endpoint, body=None):
        calls.append(endpoint)
        if endpoint == "api/generate-password":
            return {"password": "horse-battery-staple-ok"}
        return {"id": "X", "name": "demo", "link": "L"}

    monkeypatch.setattr(cli, "_request", fake_request)
    assert cli.main(["create-share", "demo", "--generate-password"]) == 0
    assert "api/generate-password" in calls
    out = capsys.readouterr().out
    assert "horse-battery-staple-ok" in out


def test_cli_set_password_and_clear(monkeypatch, capsys):
    from jupyterlab_share_files_extension import cli

    sent = []
    monkeypatch.setattr(
        cli,
        "_request",
        lambda m, ep, body=None: sent.append((ep, body)) or {"id": "AAAA22"},
    )
    assert cli.main(["set-password", "share", "AAAA22", "newpw"]) == 0
    assert sent[-1] == ("api/shares/AAAA22/password", {"password": "newpw"})
    assert cli.main(["set-password", "request", "AAAA22", "--clear"]) == 0
    assert sent[-1] == ("api/requests/AAAA22/password", {"password": ""})
