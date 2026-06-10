"""Unit tests for the Share Files CLI.

No network: the API request helpers are monkeypatched for both the Cloudflare
layer (`_cf_request`) and the share-files subcommands - the CLI is a thin
dispatcher over both.
"""

import io
import json

import pytest

from jupyterlab_share_files_extension import cli


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- #
# Subcommand dispatch to the API client functions
# --------------------------------------------------------------------------- #


def test_list_items_dispatches(monkeypatch, capsys):
    monkeypatch.setattr(cli, "list_items", lambda: {"shares": []})
    assert cli.main(["--json", "list-items"]) == 0
    assert json.loads(capsys.readouterr().out) == {"shares": []}


def test_create_share_passes_name_and_paths(monkeypatch, capsys):
    captured = {}

    def fake(name, paths):
        captured["args"] = (name, paths)
        return {"id": "X"}

    monkeypatch.setattr(cli, "create_share", fake)
    assert cli.main(["create-share", "demo", "a.txt", "b/c.txt"]) == 0
    assert captured["args"] == ("demo", ["a.txt", "b/c.txt"])


def test_pick_up_names_default_to_all(monkeypatch, capsys):
    captured = {}

    def fake(key, names, target_dir):
        captured["args"] = (key, names, target_dir)
        return {"ok": True}

    monkeypatch.setattr(cli, "pick_up", fake)
    assert cli.main(["pick-up", "K1", "--target-dir", "inbox"]) == 0
    assert captured["args"] == ("K1", None, "inbox")


def test_runtime_error_prints_to_stderr_and_exits_1(monkeypatch, capsys):
    def boom():
        raise RuntimeError("no server")

    monkeypatch.setattr(cli, "list_items", boom)
    assert cli.main(["list-items"]) == 1
    assert "no server" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# cloudflare: token save + verify
# --------------------------------------------------------------------------- #


def test_cloudflare_token_and_account_saved_with_0600(config_home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "cloudflare_setup", lambda *a: {"tunnel_id": "t"})
    monkeypatch.setattr(cli, "ensure_connector", lambda *a, **kw: True)
    assert (
        cli.main(
            ["cloudflare", "setup", "--token", "tok123", "--account-id", "acc9",
             "--private-base-url", "https://hub.example.com/user/a/"]
        )
        == 0
    )
    path = cli.config_path()
    cfg = json.loads(path.read_text())
    assert cfg["cloudflare_token"] == "tok123"
    assert cfg["cloudflare_account_id"] == "acc9"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_cloudflare_validate_without_token_fails(config_home, capsys):
    assert cli.main(["cloudflare", "validate"]) == 1
    assert "no token" in capsys.readouterr().err


def test_cloudflare_verify_happy_path(config_home, monkeypatch, capsys):
    calls = []

    def fake_cf(method, endpoint, token, body=None):
        calls.append((method, endpoint.split("?")[0], body))
        if endpoint == "user/tokens/verify":
            return {"success": True, "result": {"status": "active"}}
        if endpoint == "accounts":
            return {"success": True, "result": [{"id": "acc1", "name": "Acme"}]}
        if endpoint.startswith("accounts/acc1/cfd_tunnel?"):
            return {
                "success": True,
                "result": [{"id": "t1", "name": "existing", "status": "healthy"}],
            }
        if method == "POST":
            return {"success": True, "result": {"id": "probe-id"}}
        if method == "DELETE":
            return {"success": True, "result": {}}
        raise AssertionError(f"unexpected call {method} {endpoint}")

    monkeypatch.setattr(cli, "_cf_request", fake_cf)
    out = cli.cloudflare_verify("tok")

    assert out["token_valid"] is True
    assert out["accounts"] == [{"id": "acc1", "name": "Acme"}]
    assert out["can_bind_existing"] is True
    assert out["existing_tunnels"][0]["name"] == "existing"
    assert out["can_create_tunnel"] is True
    # the create probe must be cleaned up
    assert ("DELETE", "accounts/acc1/cfd_tunnel/probe-id", None) in calls


def test_cloudflare_verify_invalid_token_short_circuits(monkeypatch):
    def fake_cf(method, endpoint, token, body=None):
        return {"success": False, "errors": [{"code": 6003, "message": "bad header"}]}

    monkeypatch.setattr(cli, "_cf_request", fake_cf)
    out = cli.cloudflare_verify("nope", "acc1")
    assert out["token_valid"] is False
    assert "6003" in out["error"]
    assert out["can_create_tunnel"] is False


def test_cloudflare_verify_account_owned_token_falls_back(monkeypatch):
    """A cfat_ account-owned token fails /user/tokens/verify but verifies at
    /accounts/{id}/tokens/verify - the flow must try both."""

    def fake_cf(method, endpoint, token, body=None):
        if endpoint == "accounts":
            return {"success": True, "result": [{"id": "acc1", "name": "Acme"}]}
        if endpoint == "user/tokens/verify":
            return {"success": False, "errors": [{"code": 1000, "message": "Invalid API Token"}]}
        if endpoint == "accounts/acc1/tokens/verify":
            return {"success": True, "result": {"status": "active"}}
        if method == "GET":
            return {"success": True, "result": []}
        if method == "POST":
            return {"success": True, "result": {"id": "probe-id"}}
        return {"success": True, "result": {}}

    monkeypatch.setattr(cli, "_cf_request", fake_cf)
    out = cli.cloudflare_verify("cfat_x")
    assert out["token_valid"] is True
    assert out["account_id"] == "acc1"


def _setup_fake_cf(calls):
    """Cloudflare API fake for --setup: valid token, no existing tunnels."""

    def fake_cf(method, endpoint, token, body=None):
        calls.append((method, endpoint, body))
        if endpoint == "user/tokens/verify":
            return {"success": True, "result": {"status": "active"}}
        if endpoint == "accounts":
            return {"success": True, "result": [{"id": "acc1", "name": "Acme"}]}
        if endpoint.startswith("accounts/acc1/cfd_tunnel?"):
            return {"success": True, "result": []}
        if method == "POST" and endpoint == "accounts/acc1/cfd_tunnel":
            name = body["name"]
            tid = "probe-id" if name.startswith("share-files-verify-") else "tun-1"
            return {"success": True, "result": {"id": tid, "name": name}}
        if method == "DELETE":
            return {"success": True, "result": {}}
        if method == "PUT" and endpoint.endswith("/configurations"):
            return {"success": True, "result": body}
        if endpoint.startswith("zones?name="):
            return {"success": True, "result": [{"id": "zone-1"}]}
        if endpoint.startswith("zones/zone-1/dns_records?"):
            return {"success": True, "result": []}
        if method == "POST" and endpoint == "zones/zone-1/dns_records":
            return {"success": True, "result": {"id": "rec-1"}}
        if method == "GET" and endpoint == "zones/zone-1/settings/always_use_https":
            return {"success": True, "result": {"id": "always_use_https", "value": "off"}}
        if method == "PATCH" and endpoint == "zones/zone-1/settings/always_use_https":
            return {"success": True, "result": {"id": "always_use_https", "value": "on"}}
        if endpoint == "accounts/acc1/cfd_tunnel/tun-1/token":
            return {"success": True, "result": "conn-token"}
        raise AssertionError(f"unexpected call {method} {endpoint}")

    return fake_cf


def test_cloudflare_setup_provisions_and_saves(config_home, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_cf_request", _setup_fake_cf(calls))
    out = cli.cloudflare_setup(
        "tok", "", "share.example.com", "https://hub.example.com/user/alice/"
    )

    assert out["tunnel_id"] == "tun-1"
    assert out["public_base_url"] == "https://share.example.com"
    assert out["run_command"] == "cloudflared tunnel run --token conn-token"
    # the origin is the PUBLIC base URL's scheme+host, never internal/localhost
    assert out["origin"] == "https://hub.example.com"

    # ingress: hostname+public-path -> origin plus the 404 catch-all, so the
    # rest of the hub / private network stays unreachable through the tunnel
    put = next(b for m, e, b in calls if m == "PUT" and e.endswith("/configurations"))
    assert put["config"]["ingress"] == [
        {
            "hostname": "share.example.com",
            "path": r"^(/user/[^/]+)?/jupyterlab-share-files-extension/public/.*",
            "service": "https://hub.example.com",
            "originRequest": {
                "httpHostHeader": "hub.example.com",
                "originServerName": "hub.example.com",
                "noTLSVerify": True,
            },
        },
        {"service": "http_status:404"},
    ]
    # HTTPS is enforced at the edge: the zone setting is switched on
    assert any(
        m == "PATCH" and e == "zones/zone-1/settings/always_use_https" and b == {"value": "on"}
        for m, e, b in calls
    )
    # proxied CNAME to <tunnel>.cfargotunnel.com on the apex zone
    dns = next(b for m, e, b in calls if e == "zones/zone-1/dns_records")
    assert dns == {
        "type": "CNAME",
        "name": "share.example.com",
        "content": "tun-1.cfargotunnel.com",
        "proxied": True,
    }
    cfg = json.loads(cli.config_path().read_text())
    assert cfg["cloudflare_tunnel_id"] == "tun-1"
    assert cfg["cloudflare_hostname"] == "share.example.com"
    assert cfg["cloudflare_tunnel_token"] == "conn-token"
    assert cfg["public_base_url"] == "https://share.example.com"


def test_cloudflare_setup_reuses_existing_tunnel(config_home, monkeypatch):
    calls = []
    base = _setup_fake_cf(calls)

    def fake_cf(method, endpoint, token, body=None):
        if endpoint.startswith("accounts/acc1/cfd_tunnel?"):
            calls.append((method, endpoint, body))
            return {
                "success": True,
                "result": [{"id": "tun-1", "name": "share-files-hub-example-com", "status": "inactive"}],
            }
        return base(method, endpoint, token, body)

    monkeypatch.setattr(cli, "_cf_request", fake_cf)
    out = cli.cloudflare_setup("tok", "", "share.example.com", "https://hub.example.com")
    assert out["tunnel_id"] == "tun-1"
    # no second share-files tunnel was created (only the verify probe POSTs)
    creates = [b for m, e, b in calls if m == "POST" and e == "accounts/acc1/cfd_tunnel"]
    assert all(b["name"].startswith("share-files-verify-") for b in creates)


def test_cloudflare_setup_refuses_without_create_rights(monkeypatch):
    monkeypatch.setattr(
        cli,
        "cloudflare_verify",
        lambda token, account_id="": {
            "can_create_tunnel": False,
            "create_error": "10000: Authentication error",
            "account_id": "acc1",
            "existing_tunnels": [],
        },
    )
    with pytest.raises(RuntimeError, match="cannot create tunnels"):
        cli.cloudflare_setup("tok", "acc1", "share.example.com", "https://hub.example.com")


def test_cloudflare_setup_requires_private_base_url(config_home, capsys):
    """Setup runs only when --private-base-url is given - the public base is
    explicit, never inferred from the environment or defaulted to
    localhost/internal addresses. --run without it is refused."""
    cli._save_config({"cloudflare_token": "tok"})
    with pytest.raises(SystemExit):
        cli.main(["cloudflare", "setup"])
    assert "--private-base-url" in capsys.readouterr().err


def test_cloudflare_one_command_saves_and_sets_up(config_home, monkeypatch, capsys):
    """One `cloudflare` invocation: --token/--account_id are saved and
    --private-base-url triggers the tunnel setup - no separate mode flag."""
    captured = {}

    def fake_setup(token, account_id, hostname, private_base_url):
        captured["args"] = (token, account_id, hostname, private_base_url)
        return {"tunnel_id": "tun-1"}

    monkeypatch.setattr(cli, "cloudflare_setup", fake_setup)
    monkeypatch.setattr(cli, "ensure_connector", lambda *a, **kw: True)
    assert (
        cli.main(
            [
                "--json",
                "cloudflare",
                "setup",
                "--token", "tok",
                "--account-id", "acc1",
                "--hostname", "share.example.com",
                "--private-base-url", "https://hub.example.com/user/alice/",
            ]
        )
        == 0
    )
    assert captured["args"] == (
        "tok", "acc1", "share.example.com", "https://hub.example.com/user/alice/"
    )
    cfg = json.loads(cli.config_path().read_text())
    assert cfg["cloudflare_token"] == "tok"
    out = json.loads(capsys.readouterr().out)
    assert out["setup"]["tunnel_id"] == "tun-1"
    # the extension guarantees the connector after setup
    assert out["setup"]["daemon_running"] is True


def test_cloudflare_info_masks_tokens(config_home, monkeypatch, capsys):
    """--info shows the current config with secrets masked to the last 4
    characters; the account id is shown in full."""
    cli._save_config(
        {
            "cloudflare_token": "cfat_secretsecretb676",
            "cloudflare_account_id": "acc-full-id",
            "cloudflare_tunnel_id": "tun-1",
            "cloudflare_hostname": "share.example.com",
            "cloudflare_tunnel_token": "eyJsecretsecret9zzz",
            "public_base_url": "https://share.example.com",
        }
    )
    monkeypatch.setattr(cli, "_connector_running", lambda: True)
    monkeypatch.setattr(
        cli,
        "_cf_request",
        lambda *a, **kw: {"success": True, "result": {"status": "healthy"}},
    )
    assert cli.main(["--json", "cloudflare", "info"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cloudflare_token"] == "...b676"
    assert out["cloudflare_tunnel_token"] == "...9zzz"
    assert out["cloudflare_account_id"] == "acc-full-id"
    assert out["public_base_url"] == "https://share.example.com"
    assert out["daemon_running"] is True
    assert out["tunnel_status"] == "healthy"


def test_human_output_is_default_json_optional(monkeypatch, capsys):
    """Without --json the result renders as key: value lines; with --json it
    is machine-readable JSON."""
    monkeypatch.setattr(cli, "list_items", lambda: {"shares": [], "requests": []})
    assert cli.main(["list-items"]) == 0
    human = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        json.loads(human)
    assert "shares" in human


def test_ensure_connector_retries_and_fails(config_home, monkeypatch, caplog):
    """ensure_connector tries the configured number of times and logs the
    failure when the connector never stays up."""
    cli._save_config({"cloudflare_tunnel_token": "tt"})
    monkeypatch.setattr(cli, "_connector_running", lambda: False)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    attempts = []

    class _DeadProc:
        pid = 123
        returncode = 1

        def poll(self):
            return 1

    def fake_popen(*a, **kw):
        attempts.append(a)
        return _DeadProc()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    import logging as _logging

    log = _logging.getLogger("test-connector")
    with caplog.at_level("ERROR", logger="test-connector"):
        assert cli.ensure_connector(retries=3, logger=log) is False
    assert len(attempts) == 3
    assert any("failed after 3 attempts" in r.message for r in caplog.records)


def test_ensure_connector_succeeds_when_process_stays_up(config_home, monkeypatch):
    cli._save_config({"cloudflare_tunnel_token": "tt"})
    monkeypatch.setattr(cli, "_connector_running", lambda: False)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    class _LiveProc:
        pid = 123

        def poll(self):
            return None

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **kw: _LiveProc())
    assert cli.ensure_connector(retries=3) is True


def test_cloudflare_setup_rejects_relative_base_url():
    with pytest.raises(RuntimeError, match="not an\\s+absolute URL"):
        cli._origin_from_base_url("/user/alice/")


def test_cloudflare_setup_rejects_http_base_url():
    """Only secure connections are permitted - an http base URL is refused
    with guidance; localhost is fine as long as it is https."""
    with pytest.raises(RuntimeError, match="must use\\s+https"):
        cli._origin_from_base_url("http://localhost:8888/")
    assert cli._origin_from_base_url("https://localhost:8888/") == "https://localhost:8888"


# --------------------------------------------------------------------------- #
# cloudflare: --reset
# --------------------------------------------------------------------------- #


def test_cloudflare_reset_clears_token_and_setup_state(config_home, capsys):
    cli._save_config(
        {
            "cloudflare_token": "tok",
            "cloudflare_account_id": "acc1",
            "cloudflare_tunnel_id": "tun-1",
            "cloudflare_hostname": "share.example.com",
            "cloudflare_tunnel_token": "conn-token",
            "private_base_url": "https://hub.example.com/user/a/",
            "public_base_url": "https://share.example.com",
            "tunnel_active": True,
            "tunnel_autostart": True,
            "unrelated": "kept",
        }
    )
    assert cli.main(["--json", "cloudflare", "reset"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["reset"]) == set(cli._RESET_KEYS)
    cfg = json.loads(cli.config_path().read_text())
    assert cfg == {"unrelated": "kept"}
    # back to the unconfigured state: validate now demands a token
    assert cli.main(["cloudflare", "validate"]) == 1


def test_cloudflare_reset_rejects_other_flags(config_home, capsys):
    """The subcommands are orthogonal - reset takes no flags at all."""
    with pytest.raises(SystemExit):
        cli.main(["cloudflare", "reset", "--token", "tok"])


def test_cloudflare_verify_create_denied(monkeypatch):
    def fake_cf(method, endpoint, token, body=None):
        if endpoint == "user/tokens/verify":
            return {"success": True, "result": {"status": "active"}}
        if endpoint == "accounts":
            return {"success": True, "result": [{"id": "acc1", "name": "Acme"}]}
        if method == "GET":
            return {"success": True, "result": []}
        return {"success": False, "errors": [{"code": 10000, "message": "denied"}]}

    monkeypatch.setattr(cli, "_cf_request", fake_cf)
    out = cli.cloudflare_verify("tok")
    assert out["can_bind_existing"] is True
    assert out["can_create_tunnel"] is False
    assert "denied" in out["create_error"]


def test_cloudflare_start_requires_setup(config_home, capsys):
    assert cli.main(["cloudflare", "start"]) == 1
    assert "run cloudflare setup first" in capsys.readouterr().err


def test_cloudflare_start_and_stop_toggle_active_state(
    config_home, monkeypatch, capsys
):
    """start/stop switch between public links (daemon up) and private links
    (daemon stopped) without touching credentials or Cloudflare resources."""
    cli._save_config(
        {
            "cloudflare_tunnel_token": "conn-token",
            "public_base_url": "https://share.example.com",
        }
    )
    monkeypatch.setattr(cli, "ensure_connector", lambda *a, **kw: True)
    stopped = []
    monkeypatch.setattr(cli, "stop_connector", lambda: stopped.append(True) or True)

    assert cli.main(["--json", "cloudflare", "start"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tunnel_active"] is True
    assert out["daemon_running"] is True
    assert json.loads(cli.config_path().read_text())["tunnel_active"] is True

    assert cli.main(["--json", "cloudflare", "stop"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tunnel_active"] is False
    assert out["daemon_stopped"] is True
    cfg = json.loads(cli.config_path().read_text())
    assert cfg["tunnel_active"] is False
    # credentials and setup state are kept - stop is not reset
    assert cfg["cloudflare_tunnel_token"] == "conn-token"
    assert cfg["public_base_url"] == "https://share.example.com"
    assert stopped


def test_bare_invocation_prints_full_help(capsys):
    """No command -> the full help (not a terse usage error), exit 0."""
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "commands:" in out
    assert "cloudflare" in out


def test_cloudflare_validate_reports_cloudflared_binary(
    config_home, monkeypatch, capsys
):
    """validate checks the system can reach the cloudflared binary - the
    extension launches the connector itself, so a missing binary means the
    tunnel can never come up."""
    cli._save_config({"cloudflare_token": "tok"})
    monkeypatch.setattr(cli, "cloudflare_verify", lambda *a: {"token_valid": True})
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/cloudflared")
    assert cli.main(["--json", "cloudflare", "validate"]) == 0
    out = json.loads(capsys.readouterr().out)["validate"]
    assert out["cloudflared_available"] is True
    assert out["cloudflared_path"] == "/usr/bin/cloudflared"

    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["--json", "cloudflare", "validate"]) == 0
    out = json.loads(capsys.readouterr().out)["validate"]
    assert out["cloudflared_available"] is False
    assert "NOT FOUND" in out["cloudflared_path"]


def test_tunnel_name_is_deterministic_slug_of_private_url():
    """Tunnel name = extension prefix + sluggified private base URL - the
    same URL always yields the same name (reuse), different users/servers
    never collide on a shared account."""
    assert (
        cli.tunnel_name("https://hub.example.com/user/alice/")
        == "share-files-hub-example-com-user-alice"
    )
    assert (
        cli.tunnel_name("https://hub.example.com/user/alice/")
        == cli.tunnel_name("https://hub.example.com/user/alice")
    )
    assert cli.tunnel_name("https://hub.example.com/user/alice/") != cli.tunnel_name(
        "https://hub.example.com/user/bob/"
    )
    assert cli.tunnel_name("https://myhost.example.com") == "share-files-myhost-example-com"
