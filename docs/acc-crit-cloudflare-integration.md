# Acceptance Criteria - Cloudflare Integration

Criteria for the Cloudflare tunnel integration that will expose share and
request links beyond the hub (direction in
[CLOUDFLARE_SHARING.md](CLOUDFLARE_SHARING.md)). AC-CF1-AC-CF7 cover the CLI
foundation - credential handling and tunnel-rights verification. AC-CF8
(tunnel provisioning + `public_base_url` link rewriting), AC-CF11
(self-connect over the Cloudflare host), AC-CF12 (reset) and AC-CF13
(HTTPS only) are implemented; AC-CF9/AC-CF10 (panel connection behaviour
over the Cloudflare origin) need a second peer to exercise end to end.

## Preconditions

- The package is installed so the `jupyterlab_share_files` console script is on
  PATH (or run as `python -m jupyterlab_share_files_extension.cli`).
- A Cloudflare account and an API token. Account-owned tokens (`cfat_...`)
  and user-owned tokens are both acceptable.

## Criteria

**AC-CF1 - Token is accepted and saved.**
Given `cloudflare --token <T>`, the token is written to
`~/.config/jupyterlab-share-files/config.json` with file mode `600`. A later
run with a new `--token` replaces it.

**AC-CF2 - Account id is accepted and saved.**
Given `cloudflare --account_id <A>`, the account id is saved alongside the
token in the same file. `--token` and `--account_id` may be passed together or
separately; each save preserves the other value.

**AC-CF3 - Verify reports token validity for both token types.**
Given `cloudflare --verify`, the token is checked against
`/user/tokens/verify`; when that rejects it (account-owned `cfat_` tokens are
not user tokens), against `/accounts/{id}/tokens/verify`. The result carries
`token_valid: true` for an active token of either kind.

**AC-CF4 - Verify resolves the account.**
When `--account_id` is given (or saved), it is used directly. Otherwise the
account is discovered via `GET /accounts` and the first account is used. With
no account id and no listable account, verify fails with a message naming
`--account_id` as the fix.

**AC-CF5 - Verify reports bind capability.**
`can_bind_existing: true` when the token can list `cfd_tunnel` in the account
(read access - enough to bind `cloudflared` to an existing tunnel), and the
existing tunnels are returned with id, name and status.

**AC-CF6 - Verify reports create capability truthfully.**
`can_create_tunnel` is proven, not inferred: a throwaway tunnel
(`share-files-verify-<hex>`) is created and immediately deleted. Creation
denied by Cloudflare yields `can_create_tunnel: false` with the API's own
error under `create_error`; a probe that cannot be cleaned up is reported
under `probe_cleanup_warning`.

**AC-CF7 - Verify works from saved credentials.**
`cloudflare --verify` with no flags uses the saved token and account id.
Without a saved or passed token it exits 1 with guidance to pass `--token`.

**AC-CF8 - A verified token yields a working Cloudflare URL to the service.**
Given a saved token that verify confirms can set up a tunnel
(`can_create_tunnel: true`), a Cloudflare-based URL is created (a tunnel with
a public hostname routed to the local Jupyter server) through which the
extension's public share/request endpoints are reachable from outside the
hub - opening `<cloudflare-url>/.../public/share/<id>` serves the share
exactly as the hub-local link does. Generated links then carry the Cloudflare
hostname so recipients without hub access can use them. Implemented as
`cloudflare --setup --hostname H --local-base-url URL [--run]`: creates or
reuses a tunnel named `share-files`, routes the hostname to the server
address given by the MANDATORY `--local-base-url` (explicit, never inferred
from the environment - so an internal address cannot sneak in; it must be
`https`, with an error and guidance otherwise, and `localhost` is acceptable
only as an explicit https value), upserts a proxied CNAME to
`<tunnel>.cfargotunnel.com`, and saves
`public_base_url = https://<hostname>` to the CLI config. The tunnel ingress
is path-restricted to the extension's unauthenticated `/public/...` capability
endpoints with a catch-all 404, so the hub login, the authenticated `/api/*`
endpoints, and the rest of the private network are NOT reachable through the
tunnel; the origin's own hostname is sent as Host header and TLS SNI so a
reverse proxy in front of the hub routes the forwarded requests. The server
reads `public_base_url` per request (mtime-cached) and rewrites only the
scheme+host of generated links - the base path stays auto-detected from the
server's `base_url`; with no value configured, links keep the old behaviour
(the host the browser is on).

**AC-CF9 - A Cloudflare link connects in the share panel.**
Given a Cloudflare-based share or request link, pasting it into the panel's
connect input adds a working connection: the manifest loads, a connected
share's entries can be picked up (double-click, drag, copy/paste, save) and a
connected request accepts uploads - identically to a hub-local link. The
connection persists across refreshes using the stored link verbatim (the
existing no-reconstruct rule), so the Cloudflare hostname is never rewritten
to a hub path. _(Depends on AC-CF8.)_

**AC-CF10 - Connection traffic is HTTPS end to end, also behind the hub proxy.**
When the connecting user's own server runs behind the JupyterHub proxy, all
traffic to a Cloudflare-linked peer - browser-side manifest/download fetches
(`credentials: 'omit'`) and server-side saves/uploads (`_peer_fetch` /
`_post_file`) - goes to the link's `https://` Cloudflare origin as stored,
not via the hub's base URL. Cloudflare's certificate is publicly trusted, so
this works with `verify_peer_tls = True` (no self-signed exception needed).
_(Depends on AC-CF8.)_

**AC-CF11 - Own Cloudflare links are recognised as self.**
Pasting the Cloudflare form of one's OWN share/request link does not create a
loop connection: the self-connect detection (today an own-prefix compare that
a different hostname would bypass) also recognises the configured Cloudflare
base as self and shows the existing "your own link" dialog. Implemented in
`_own_link_prefixes`: the configured public origin + own base path counts as
self alongside the request host.

**AC-CF12 - Reset returns the CLI to the unconfigured state.**
Given `cloudflare --reset`, the saved token is reset to none: the token,
account id, tunnel state (`cloudflare_tunnel_id`, `cloudflare_hostname`,
`cloudflare_tunnel_token`) and `public_base_url` are removed from the config
file while unrelated keys are preserved. Generated links revert to the old
behaviour (local/hub address) on the next request, without a restart.
Local-only: Cloudflare-side resources (tunnel, DNS record) are kept.
`--reset` cannot be combined with other flags.

**AC-CF13 - Cloudflare allows only secure connections.**
Plain `http://` to the share hostname is not served: `--setup` switches the
zone's `always_use_https` setting on, so the Cloudflare edge 301-redirects
http to https before anything reaches the tunnel. The `--local-base-url`
the tunnel forwards to must itself be `https` (enforced at setup with an
error otherwise).

## Verification log

Verified live against the `stellars` Cloudflare account
(`d3786894d5db55e6074c57ab92e09888`) with an account-owned `cfat_` token.

- **AC-CF1/AC-CF2:** token and account id saved to
  `~/.config/jupyterlab-share-files/config.json`, mode `600`; covered by
  `test_cloudflare_token_and_account_saved_with_0600`.
- **AC-CF3:** the `cfat_` token returned `1000: Invalid API Token` from
  `/user/tokens/verify` and `status: active` from
  `/accounts/{id}/tokens/verify`; the fallback is covered by
  `test_cloudflare_verify_account_owned_token_falls_back`.
- **AC-CF4-AC-CF6 (live):** verify returned `token_valid: true`, the
  `stellars` account, `can_bind_existing: true` with the existing tunnel
  `jupyterhub.lab.stellars-tech.eu` (`e9675a18-...`, inactive), and
  `can_create_tunnel: false` with `create_error: "10000: Authentication
error"` - the token has tunnel read but not create rights, reported
  truthfully rather than assumed.
- **Unit coverage:** 10 tests in
  `jupyterlab_share_files_extension/tests/test_cli.py` (dispatch, config
  save, happy path with probe cleanup, invalid token, account-owned fallback,
  create denied).
- **AC-CF6 update (live):** after the token gained `Cloudflare Tunnel Edit`
  plus zone-scoped `DNS Edit` on `duoptimum.com`, verify returned
  `can_create_tunnel: true` (probe created and deleted).
- **AC-CF8 (live):** `cloudflare --setup --run` created tunnel `share-files`
  (`5ac0f754-53b0-43db-a0ca-ec038c432960`), routed `share.duoptimum.com` to
  the public origin `https://jupyterhub.lab.stellars-tech.eu` (path-restricted
  ingress, Host/SNI override, noTLSVerify for the hub's self-signed cert)
  with a proxied CNAME, and launched the connector (4 edge connections).
  Over the Cloudflare host a real share's
  `GET .../public/share/<id>/manifest` returned 200,
  `GET .../public/share/<id>/download/<file>` returned the exact file
  content, and the standalone share page returned 200 - all over the
  publicly trusted Cloudflare certificate. Repeated `--setup` runs reused
  the same tunnel. The ingress Host-header lesson: cloudflared forwards the
  original `Host: share.duoptimum.com` by default, which the reverse proxy
  in front of the hub does not route (404 "page not found") - fixed with
  `httpHostHeader`/`originServerName` set to the origin's own hostname.
- **Exposure scope (live):** through `share.duoptimum.com`, `/` , `/hub/login`
  and the authenticated `.../api/info` all return 404 at the edge - only the
  `/public/...` capability endpoints pass; the rest of the hub and the
  private network are unreachable.
- **AC-CF13 (live):** `GET http://share.duoptimum.com/...` returns
  `301 → https://share.duoptimum.com/...` after `--setup` switched
  `always_use_https` on (verified off→on via the API, then redirect
  observed).
- **AC-CF8/AC-CF11-AC-CF13 unit coverage:** setup/reset tests in
  `test_cli.py` (provision incl. HTTPS-enforcement PATCH and ingress shape,
  tunnel reuse, refusal without create rights, mandatory `--local-base-url`,
  http base URL rejected / https-localhost accepted, reset clears state and
  preserves unrelated keys, reset rejects combined flags), 8 tests in
  `test_public_origin.py` (trait > config-file > request-host precedence,
  scheme+host-only extraction, post-reset fallback without restart, link
  path auto-detection, own Cloudflare link counted as self), and 6 replay
  tests in `test_cloudflare_recorded.py` driven by real API envelopes
  recorded into `tests/fixtures/cloudflare_responses.json` (secrets
  redacted): verify all-green, the live `cfat_` user-endpoint rejection,
  setup provision + reuse, the recorded `always_use_https: on`, and the
  proxied CNAME pointing at the tunnel.

## Notes / limitations

- The create probe really creates (and deletes) a tunnel when the token has
  create rights; the throwaway name is `share-files-verify-<8 hex>`.
- Values rejected at the format level are not tokens: Cloudflare API tokens
  are ~40 characters, account-owned tokens start with `cfat_`, and a bare
  32-hex value is an account id.
- The running server picks up `public_base_url` changes (setup/reset) on the
  next request via an mtime check - but loading the NEW code itself requires
  reinstalling the package and restarting the server; the link-rewrite was
  proven by unit test and by the live manifest over the tunnel.
- `--run` launches the connector for verification only (logs to
  `/tmp/cloudflared-share-files.log`); production needs a managed service.
- AC-CF9/AC-CF10 (panel connection behaviour over the Cloudflare origin,
  HTTPS end to end behind the hub proxy) are specified but need a second
  peer to exercise; the building blocks are live-verified under AC-CF8.
