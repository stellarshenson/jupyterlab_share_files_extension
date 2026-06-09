"""Replay tests against RECORDED Cloudflare API responses.

`fixtures/cloudflare_responses.json` holds 17 real request/response exchanges
captured from a live `cloudflare_verify` + `cloudflare_setup` run against the
Cloudflare v4 API (secrets redacted). Replaying them exercises the CLI against
the API's actual envelope shapes - field names, nesting, error format - rather
than hand-written approximations.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from jupyterlab_share_files_extension import cli

FIXTURE = Path(__file__).parent / "fixtures" / "cloudflare_responses.json"
ACCOUNT_ID = "d3786894d5db55e6074c57ab92e09888"
TUNNEL_ID = "5ac0f754-53b0-43db-a0ca-ec038c432960"
ZONE_ID = "69235b408f1d4840fe8202843fbbb28a"
HOSTNAME = "share.duoptimum.com"


@pytest.fixture
def recorded():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def replay_cf(recorded, monkeypatch):
    """Replay recorded responses keyed by (method, endpoint shape)."""

    def normalize(endpoint: str) -> str:
        # probe-tunnel DELETEs carry a random id per run - normalize it
        return re.sub(r"cfd_tunnel/[0-9a-f-]{36}$", "cfd_tunnel/PROBE", endpoint)

    table: dict[tuple, list] = {}
    for ex in recorded:
        key = (ex["method"], normalize(ex["endpoint"]))
        table.setdefault(key, []).append(ex["response"])

    calls = []

    def fake_cf(method, endpoint, token, body=None):
        calls.append((method, endpoint, body))
        key = (method, normalize(endpoint))
        if key not in table:
            raise AssertionError(f"no recorded response for {method} {endpoint}")
        responses = table[key]
        return responses.pop(0) if len(responses) > 1 else responses[0]

    monkeypatch.setattr(cli, "_cf_request", fake_cf)
    return calls


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_recorded_verify_full_capabilities(replay_cf):
    """The real API envelopes drive verify to all-green."""
    out = cli.cloudflare_verify("REDACTED_API_TOKEN", ACCOUNT_ID)
    assert out["token_valid"] is True
    assert out["account_id"] == ACCOUNT_ID
    assert out["accounts"][0]["name"] == "stellars"
    assert out["can_bind_existing"] is True
    assert out["can_create_tunnel"] is True
    assert "create_error" not in out
    assert "probe_cleanup_warning" not in out


def test_recorded_cfat_token_rejected_by_user_endpoint(recorded):
    """The live account-owned token really fails /user/tokens/verify with
    code 1000 and verifies at /accounts/{id}/tokens/verify - the recorded
    envelopes lock the fallback the flow depends on."""
    user = next(
        ex["response"] for ex in recorded if ex["endpoint"] == "user/tokens/verify"
    )
    assert user["success"] is False
    assert user["errors"][0]["code"] == 1000
    account = next(
        ex["response"]
        for ex in recorded
        if ex["endpoint"] == f"accounts/{ACCOUNT_ID}/tokens/verify"
    )
    assert account["success"] is True
    assert account["result"]["status"] == "active"


ORIGIN = "https://jupyterhub.lab.stellars-tech.eu"


def test_recorded_setup_provisions_and_saves(replay_cf, config_home):
    out = cli.cloudflare_setup("REDACTED_API_TOKEN", ACCOUNT_ID, HOSTNAME, ORIGIN)
    assert out["tunnel_id"] == TUNNEL_ID
    assert out["hostname"] == HOSTNAME
    assert out["public_base_url"] == "https://" + HOSTNAME
    assert out["origin"] == ORIGIN

    # the ingress PUT carried the recorded shape: public path only, origin
    # Host/SNI override, catch-all 404 keeps the rest of the network dark
    put = next(
        b
        for m, e, b in replay_cf
        if m == "PUT" and e.endswith(f"{TUNNEL_ID}/configurations")
    )
    assert put["config"]["ingress"] == [
        {
            "hostname": HOSTNAME,
            "path": r"^(/user/[^/]+)?/jupyterlab-share-files-extension/public/.*",
            "service": ORIGIN,
            "originRequest": {
                "httpHostHeader": "jupyterhub.lab.stellars-tech.eu",
                "originServerName": "jupyterhub.lab.stellars-tech.eu",
                "noTLSVerify": True,
            },
        },
        {"service": "http_status:404"},
    ]
    # DNS record already existed in the recording - upsert went via PUT
    assert any(
        m == "PUT" and e.startswith(f"zones/{ZONE_ID}/dns_records/")
        for m, e, b in replay_cf
    )
    cfg = json.loads(cli.config_path().read_text())
    assert cfg["cloudflare_tunnel_id"] == TUNNEL_ID
    assert cfg["cloudflare_hostname"] == HOSTNAME
    assert cfg["cloudflare_tunnel_token"] == "REDACTED_TUNNEL_TOKEN"
    assert cfg["public_base_url"] == "https://" + HOSTNAME


def test_recorded_setup_reuses_existing_tunnel(replay_cf, config_home):
    """The recording lists the live `share-files` tunnel - setup must reuse it
    and never POST a second one (only the verify probe creates)."""
    cli.cloudflare_setup("REDACTED_API_TOKEN", ACCOUNT_ID, HOSTNAME, ORIGIN)
    creates = [
        b
        for m, e, b in replay_cf
        if m == "POST" and e == f"accounts/{ACCOUNT_ID}/cfd_tunnel"
    ]
    assert all(b["name"].startswith("share-files-verify-") for b in creates)


def test_recorded_zone_enforces_https(recorded):
    """The live zone reports always_use_https on - plain http is redirected
    at the edge, so only secure connections reach a share."""
    setting = next(
        ex["response"]
        for ex in recorded
        if ex["endpoint"].endswith("settings/always_use_https")
    )
    assert setting["result"]["value"] == "on"


def test_recorded_dns_record_points_at_tunnel(recorded):
    """The recorded zone listing carries the proxied CNAME to the tunnel."""
    listing = next(
        ex["response"]
        for ex in recorded
        if ex["endpoint"].startswith(f"zones/{ZONE_ID}/dns_records?")
    )
    rec = listing["result"][0]
    assert rec["name"] == HOSTNAME
    assert rec["content"] == f"{TUNNEL_ID}.cfargotunnel.com"
    assert rec["proxied"] is True
