"""Unit tests for the Share Files MCP server.

No network: `urllib` is monkeypatched for the `_request` layer, and `_request`
itself is monkeypatched when exercising the individual tool functions. The tool
bodies are plain functions, so they are called directly.
"""

import io
import json

import pytest

from jupyterlab_share_files_extension import mcp_server as m


# --------------------------------------------------------------------------- #
# _request: URL building, auth header, body, error mapping
# --------------------------------------------------------------------------- #


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SHARE_FILES_BASE_URL", "https://hub.test/user/alice/")
    monkeypatch.setenv("SHARE_FILES_TOKEN", "secret-token")


def test_request_builds_namespaced_url_with_auth(env, monkeypatch):
    captured = {}

    def fake_urlopen(req, context=None):  # noqa: ANN001
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = req.data
        return _FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)

    out = m._request("POST", "api/shares", {"name": "Demo", "paths": ["f.txt"]})

    assert out == {"ok": True}
    assert (
        captured["url"]
        == "https://hub.test/user/alice/jupyterlab-share-files-extension/api/shares"
    )
    assert captured["method"] == "POST"
    assert captured["auth"] == "token secret-token"
    assert json.loads(captured["body"]) == {"name": "Demo", "paths": ["f.txt"]}


def test_request_get_has_no_body(env, monkeypatch):
    captured = {}

    def fake_urlopen(req, context=None):  # noqa: ANN001
        captured["method"] = req.get_method()
        captured["body"] = req.data
        return _FakeResponse(json.dumps({"shares": []}).encode())

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    m._request("GET", "api/shares")
    assert captured["method"] == "GET"
    assert captured["body"] is None


def test_request_maps_http_error_to_server_message(env, monkeypatch):
    import urllib.error

    def fake_urlopen(req, context=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            req.full_url,
            400,
            "Bad Request",
            {},
            io.BytesIO(json.dumps({"error": "That link points to your own server"}).encode()),
        )

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as exc:
        m._request("POST", "api/connections", {"link": "x"})
    assert "400" in str(exc.value)
    assert "your own server" in str(exc.value)


def test_request_missing_config_raises():
    # no env set -> clear guidance
    import os

    saved = {k: os.environ.pop(k, None) for k in ("SHARE_FILES_BASE_URL", "JUPYTER_SERVER_URL")}
    try:
        with pytest.raises(RuntimeError) as exc:
            m._request("GET", "api/shares")
        assert "SHARE_FILES_BASE_URL" in str(exc.value)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# --------------------------------------------------------------------------- #
# Tools: each hits the right method + endpoint + body
# --------------------------------------------------------------------------- #


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    def fake_request(method, endpoint, body=None):
        recorded.append((method, endpoint, body))
        # Return shapes the tools post-process
        if endpoint == "api/shares" and method == "GET":
            return {"shares": [{"id": "AAA", "name": "S", "link": "L", "entries": [{"name": "f"}]}]}
        if endpoint == "api/requests" and method == "GET":
            return {"requests": [{"id": "BBB", "name": "R", "link": "L2", "upload_count": 2}]}
        if endpoint == "api/connections" and method == "GET":
            return {"connections": [{"key": "k", "kind": "share", "name": "C", "link": "L3"}]}
        if method == "POST" and endpoint == "api/shares":
            return {"id": "AAA", "name": body["name"], "link": "https://hub/share"}
        if method == "POST" and endpoint == "api/requests":
            return {"id": "BBB", "name": body["name"], "link": "https://hub/request"}
        if method == "POST" and endpoint == "api/connections":
            return {"key": "share:host:ID", "kind": "share", "name": "Peer", "link": "https://peer/share/ID"}
        return {"ok": True}

    monkeypatch.setattr(m, "_request", fake_request)
    return recorded


def test_list_items(calls):
    out = m.list_items()
    assert out["shares"][0] == {"id": "AAA", "name": "S", "link": "L", "entries": ["f"]}
    assert out["requests"][0]["upload_count"] == 2
    assert out["connections"][0]["key"] == "k"
    assert ("GET", "api/shares", None) in calls


def test_create_share(calls):
    out = m.create_share("Demo", ["a.txt", "b/"])
    assert out == {"id": "AAA", "name": "Demo", "link": "https://hub/share"}
    assert ("POST", "api/shares", {"name": "Demo", "paths": ["a.txt", "b/"]}) in calls


def test_create_request(calls):
    out = m.create_request("Inbox")
    assert out["link"] == "https://hub/request"
    assert ("POST", "api/requests", {"name": "Inbox"}) in calls


def test_connect_includes_entries(calls, monkeypatch):
    monkeypatch.setattr(m, "_fetch_public_json", lambda link: {"entries": [{"name": "data.csv"}]})
    out = m.connect("https://peer/share/ID")
    assert out["key"] == "share:host:ID"
    assert out["kind"] == "share"
    assert out["entries"] == ["data.csv"]
    assert ("POST", "api/connections", {"link": "https://peer/share/ID"}) in calls


def test_pick_up_all_vs_named(calls):
    m.pick_up("k")
    m.pick_up("k", names=["a.txt"], target_dir="sub")
    assert ("POST", "api/connections/k/save", {"target_dir": ""}) in calls
    assert (
        "POST",
        "api/connections/k/save",
        {"target_dir": "sub", "names": ["a.txt"]},
    ) in calls


def test_send_to_request(calls):
    m.send_to_request("k", ["x.txt"], uploader="bob")
    assert (
        "POST",
        "api/connections/k/upload",
        {"paths": ["x.txt"], "uploader": "bob"},
    ) in calls


def test_close_and_disconnect(calls):
    m.close_share("AAA")
    m.close_request("BBB")
    m.disconnect("k")
    assert ("DELETE", "api/shares/AAA", None) in calls
    assert ("DELETE", "api/requests/BBB", None) in calls
    assert ("DELETE", "api/connections/k", None) in calls


def test_list_request_uploads(calls, monkeypatch):
    def fake_request(method, endpoint, body=None):
        return {
            "id": "BBB",
            "name": "R",
            "upload_count": 1,
            "path": "@uploads/requests/R-BBB",
            "uploaders": [
                {"name": "carol", "entries": [{"name": "f.txt", "path": "@uploads/requests/R-BBB/carol/f.txt", "type": "file"}]}
            ],
        }

    monkeypatch.setattr(m, "_request", fake_request)
    out = m.list_request_uploads("BBB")
    assert out["upload_count"] == 1
    assert out["uploaders"][0]["files"][0]["path"].endswith("carol/f.txt")


def test_build_server_requires_mcp():
    # mcp is an optional import resolved lazily; if absent, build_server raises
    # ImportError (not at module import time).
    pytest.importorskip("mcp", reason="mcp not installed in this environment")
    server = m.build_server()
    assert server is not None
