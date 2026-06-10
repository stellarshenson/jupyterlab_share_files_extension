# Acceptance Criteria - Cloudflare Integration

Criteria for the Cloudflare tunnel integration exposing share/request links beyond the hub (setup guide: [cloudflare_setup.md](cloudflare_setup.md)). All criteria implemented except AC-CF9/AC-CF10, which need a second peer to exercise end to end.

## Preconditions

- Package installed - `jupyterlab_share_files` console script on PATH (or `python -m jupyterlab_share_files_extension.cli`)
- Cloudflare account + API token - account-owned (`cfat_...`) and user-owned tokens both acceptable

## Criteria

### Credentials and validation (AC-CF1-AC-CF7)

- **AC-CF1 - token saved** - `cloudflare setup --token <T>` writes the token to `~/.config/jupyterlab-share-files/config.json`, file mode `600`; a later `--token` replaces it
- **AC-CF2 - account id saved** - `--account-id <A>` saved alongside the token; each save preserves the other value
- **AC-CF3 - both token types verify** - `validate` checks `/user/tokens/verify`, falls back to `/accounts/{id}/tokens/verify` for account-owned `cfat_` tokens; `token_valid: true` for an active token of either kind
- **AC-CF4 - account resolved** - saved account id used directly, else discovered via `GET /accounts` (first account); neither → exit with guidance naming the account id as the fix
- **AC-CF5 - bind capability reported** - `can_bind_existing: true` when the token can list `cfd_tunnel`; existing tunnels returned with id, name, status
- **AC-CF6 - create capability proven, not inferred** - throwaway tunnel `share-files-verify-<hex>` created and deleted; denial → `can_create_tunnel: false` with the API error under `create_error`; failed cleanup → `probe_cleanup_warning`
- **AC-CF7 - validate uses saved credentials only** - no flags; checks the SAVED config end to end (probe included); no saved token → exit 1 with guidance; also reports `cloudflared_available`/`cloudflared_path` (`shutil.which`) - the extension launches the connector itself, so a binary missing from the server's PATH means the tunnel can never come up

### Tunnel and links (AC-CF8-AC-CF13)

**AC-CF8 - a verified token yields a working public URL.** `cloudflare setup --hostname H --private-base-url URL` provisions everything; generated links then carry the Cloudflare hostname and the public share/request endpoints are reachable from outside the hub exactly as hub-local links are.

- **Tunnel** - creates or reuses tunnel `share-files-<sluggified private base URL>` (e.g. `share-files-hub-example-com-user-alice`); deterministic → repeated setup is idempotent, unique per user/server on a shared account
- **`--private-base-url` MANDATORY** - the server address the connector forwards to; explicit, never inferred; must be `https` (error with guidance otherwise; `localhost` acceptable only as an explicit https value)
- **Ingress path-restricted** - only `^(/user/[^/]+)?/jupyterlab-share-files-extension/public/.*` routes to the origin, catch-all 404; hub login, authenticated `/api/*` and the private network NOT reachable through the tunnel
- **Host/SNI override** - origin's own hostname sent as Host header and TLS SNI (`httpHostHeader`/`originServerName`) so a reverse proxy in front of the hub routes the requests; `noTLSVerify` for https origins
- **DNS** - proxied CNAME `<hostname> → <tunnel>.cfargotunnel.com` upserted on the apex zone
- **Link rewrite** - server reads `public_base_url` per request (mtime-cached, no restart); only scheme+host rewritten, base path auto-detected from the server's `base_url`; unconfigured → old behaviour (the host the browser is on)

Remaining criteria in this group:

- **AC-CF9 - Cloudflare link connects in the panel** - pasting a Cloudflare link adds a working connection (manifest, pick-up, uploads) identically to a hub-local link; link persisted verbatim, never reconstructed _(depends on AC-CF8; needs a second peer)_
- **AC-CF10 - HTTPS end to end behind the hub proxy** - browser fetches (`credentials: 'omit'`) and server-side saves/uploads go to the stored `https://` Cloudflare origin; publicly trusted certificate, works with `verify_peer_tls = True` _(depends on AC-CF8; needs a second peer)_
- **AC-CF11 - own Cloudflare links are self** - `_own_link_prefixes` counts the configured public origin + own base path as self; pasting one's own Cloudflare link shows the "your own link" dialog, no loop connection
- **AC-CF12 - reset returns to unconfigured** - removes token, account id, tunnel state and `public_base_url`; unrelated keys preserved; links revert on the next request, no restart; Cloudflare-side resources kept; takes no flags
- **AC-CF13 - HTTPS only** - setup switches the zone's `always_use_https` on → http 301-redirects to https at the edge; `--private-base-url` itself must be https (enforced at setup)

### CLI shape and daemon (AC-CF14-AC-CF17)

- **AC-CF14 - six orthogonal subcommands** - `setup`, `validate`, `info`, `start`, `stop`, `reset`; only `setup` carries flags (`--token`, `--account-id`, `--hostname`, `--private-base-url`); `cloudflare --help` gives a comprehensive reference with examples
- **AC-CF15 - info is safe and complete** - config path, account id in full, hostname, tunnel id, `private_base_url`/`public_base_url`, `tunnel_active`, `tunnel_autostart`; API and tunnel tokens masked to their LAST 4 characters; reports `daemon_running` (process) and `tunnel_status` (Cloudflare-side, e.g. `healthy`)
- **AC-CF16 - machine-readable on demand** - human `key: value` lines by default; global `--json` flag switches every subcommand to JSON
- **AC-CF17 - extension guarantees the daemon** - at startup and after setup, `cloudflared tunnel run` ensured, retrying `c.ShareFilesConfig.cloudflared_retries` times (default 3); all attempts failing logged as error, success as info; no manual `--run`; with `tunnel_autostart` off the server starts inactive (private links, no daemon)

### Toggle, UI and ergonomics (AC-CF18-AC-CF25)

- **AC-CF18 - public/private switch keeps the setup** - `start` marks active + starts the daemon (public links); `stop` stops the daemon (private links on the next request); credentials, tunnel and DNS kept (unlike `reset`); same switch as `POST api/tunnel {"active": bool}`; toggle read per request, no restart; self-connect detection toggle-independent
- **AC-CF19 - cloud icon reflects and controls the tunnel** - in the panel header, left of the filter (funnel) icon, ALWAYS visible; green filled cloud = active, dim dashed silhouette = inactive OR not configured, blinking blue = connecting; configured → click toggles via `api/tunnel` and refreshes the panel so visible links change host; not configured → click opens the setup popup (AC-CF23)
- **AC-CF20 - autostart is a user setting** - Settings Editor → Share Files → `tunnelAutostart` (default on); persisted server-side (`tunnel_autostart` in the CLI config) and honoured at startup: on → tunnel up, public links; off → tunnel down, private links until switched on
- **AC-CF21 - link dialog verifies reachability** - "Checking link reachability…" → "✓ Link is reachable" (green) or "✗ Link is not reachable" (red, with HTTP status or error); probe runs server-side (`GET api/link-check`) because a frontend fetch is blocked by CORS; only `kind`+`id` accepted, URL rebuilt server-side (no SSRF surface); certificate validation is off for the probe - the URL is the server's own, the question is reachability, so a self-signed hub certificate does not fail the check
- **AC-CF22 - CLI readable by default** - bare `jupyterlab_share_files` prints the full command reference (exit 0), not a usage error; help and human output conservatively coloured on a TTY (headers bold, keys cyan, `True`/`healthy` green, `False`/`down` red); plain when piped or `NO_COLOR` set
- **AC-CF23 - configure from the panel** - clicking the silhouette icon while no tunnel is configured opens a setup popup with the same inputs as `cloudflare setup`: API token, account id, public hostname, private base URL; each field carries a hint where to take the value from (token → dashboard My Profile/Account API Tokens with the two required policies; account id → domain Overview right-hand column; public hostname → a subdomain of a Cloudflare-managed domain; private base URL → this server's address as seen in the browser bar, prefilled, https required); submit runs the full setup server-side (`POST api/tunnel/setup`, blocking Cloudflare calls in an executor) with an in-progress notification and success/error outcome
- **AC-CF24 - reset from the link dialog** - the share-link popup carries a "Reset Cloudflare sharing settings" link at the bottom (shown only while a tunnel is configured); clicking it closes the popup and runs the same reset as `cloudflare reset` via `POST api/tunnel/reset` - credentials, tunnel state and private/public base URLs cleared, Cloudflare-side resources kept; links revert to the private address, the cloud icon returns to its "click to set up" state and the user must configure again (AC-CF23)
- **AC-CF25 - one implementation, two frontends** - ALL tunnel/Cloudflare behaviour lives once in the `tunnel` library module (config file, Cloudflare client, verify, `setup_and_start`, `tunnel_start`/`tunnel_stop`, autostart, `tunnel_state`/`tunnel_info`, `validate_config`, `reset_config`, connector lifecycle); the CLI (`cloudflare` subcommands) and the HTTP API (`api/tunnel*`) are thin dispatchers into it and must never grow their own logic - the two paths cannot diverge

### Password protection and hardening (AC-CF26-AC-CF31)

Motivated by the security review of internet exposure through the tunnel: the link alone is 40 bits, so resources can carry an optional second factor.

- **AC-CF26 - connector token never on argv** - `cloudflared tunnel run` receives its token via the `TUNNEL_TOKEN` environment variable, not `--token` on the command line; nothing secret in `ps`/`/proc/<pid>/cmdline` on shared hosts
- **AC-CF27 - optional password at creation** - share/request create dialogs carry an optional password field with a "Generate" button (xkcd-style passphrase via the `xkcdpass` library, `api/generate-password`); CLI parity: `--password`/`--generate-password` on `create-share`/`create-request`, plus `set-password` and `generate-password` subcommands; password optional everywhere - empty means open access, unchanged behaviour
- **AC-CF28 - password gates ALL public access** - with a password set, the standalone page, manifest, downloads (single, zip, download-all) and request uploads all require a valid unlock token (`X-Share-Token` header or `?t=` query); the token comes from `POST public/<kind>/<id>/unlock`, is HMAC-bound to the password (changing the password invalidates every outstanding token) and expires after 6h; plaintext password never leaves the owner's authenticated API
- **AC-CF29 - rate-limited unlock** - password attempts are limited per resource id via the `limits` library (in-memory moving window): a per-minute cap plus a mandatory cooldown between consecutive attempts; defaults generous (30/minute, 1s cooldown), tightened via `c.ShareFilesConfig.password_max_attempts_per_minute` / `password_attempt_cooldown_seconds`; exceeded → 429
- **AC-CF30 - owner manages and sees the password** - the link dialog shows the password (when set) next to the link with its own Copy button; right-click on a share/request row → "Set Password..." / "Change Password..." opens a dialog pre-filled with the current value, with Generate, save and clear (empty = remove); owner retrieval via authenticated `GET api/<shares|requests>/<id>/password`
- **AC-CF31 - connecting to a protected link** - connect probes the peer: a protected resource answers 401 `password_required`, the panel prompts and the password is verified against the peer's unlock endpoint at connect time (wrong password → re-prompt, peer rate limit surfaced as 429); the verified password is stored with the connection and every later peer fetch (manifest refresh, pick-up, send-to-request, panel downloads) unlocks automatically; the standalone page shows the same password gate before any content

## Verification log

Verified live against the `stellars` Cloudflare account (`d3786894d5db55e6074c57ab92e09888`) with an account-owned `cfat_` token.

- **AC-CF1/AC-CF2** - token + account id saved, mode `600`; `test_cloudflare_token_and_account_saved_with_0600`
- **AC-CF3 (live)** - the `cfat_` token returned `1000: Invalid API Token` from `/user/tokens/verify`, `status: active` from `/accounts/{id}/tokens/verify`; `test_cloudflare_verify_account_owned_token_falls_back`
- **AC-CF4-AC-CF6 (live)** - `token_valid: true`, account `stellars`, `can_bind_existing: true` (existing tunnel `jupyterhub.lab.stellars-tech.eu`, inactive); initially `can_create_tunnel: false` with `create_error: "10000: Authentication error"` - reported truthfully; after adding `Cloudflare Tunnel Edit` + zone-scoped `DNS Edit` on `duoptimum.com`: `can_create_tunnel: true` (probe created and deleted)
- **AC-CF8 (live)** - tunnel `share-files` (`5ac0f754-53b0-43db-a0ca-ec038c432960`) routed `share.duoptimum.com` → `https://jupyterhub.lab.stellars-tech.eu`; real share over the Cloudflare host: manifest 200, file download byte-exact, standalone page 200; repeated setup reused the tunnel; Host-header lesson: cloudflared forwards `Host: share.duoptimum.com` by default, the hub's reverse proxy 404s on it - fixed with `httpHostHeader`/`originServerName`
- **Exposure scope (live)** - `/`, `/hub/login`, authenticated `api/info` all 404 at the edge; only `/public/...` passes
- **AC-CF13 (live)** - `http://share.duoptimum.com/...` → `301 https://...` after setup switched `always_use_https` off→on
- **Unit coverage** - `test_cli.py` (dispatch, config save, verify paths, setup provision incl. ingress shape + HTTPS PATCH, tunnel reuse, refusal without create rights, mandatory/https `--private-base-url`, reset semantics); 10 tests in `test_public_origin.py` (origin precedence, scheme+host-only, post-reset fallback, path auto-detection, self-link recognition, toggle gating); 6 replay tests in `test_cloudflare_recorded.py` driven by real API envelopes in `tests/fixtures/cloudflare_responses.json` (secrets redacted)
- **AC-CF14-AC-CF17 (live)** - `cloudflare info` showed masked tokens (`...d676`, `...fQ==`), full account id, `daemon_running: True`, `tunnel_status: healthy`; `validate` all-green from saved credentials; `test_cloudflare_info_masks_tokens`, `test_human_output_is_default_json_optional`, `test_ensure_connector_*` (3-attempt retry, failure logged)
- **AC-CF18-AC-CF22 (unit + live)** - `test_cloudflare_start_and_stop_toggle_active_state`, `test_cloudflare_start_requires_setup`, `test_tunnel_inactive_reverts_links_to_private`, `test_own_cloudflare_link_recognised_while_tunnel_off`, `test_bare_invocation_prints_full_help`; live: after `cloudflared` went missing from the host, `cloudflare setup --private-base-url ...` reinstated it (`daemon_running: True`, `tunnel_status: healthy`, both green in the coloured output) and the public manifest answered 200 through `share.duoptimum.com`; probe pattern proven live - a public-path GET through the edge returns the server's own response, edge-blocked paths an empty-body 404
- **AC-CF26-AC-CF31 (unit)** - 30 tests in `test_password.py`: password persisted owner-only and stripped from every client-facing manifest/list, xkcdpass generation format, constant-time verify, unlock-token round trip incl. expiry and invalidation on password change, per-minute cap / per-resource isolation / cooldown / generous defaults, gate semantics (open without password, 401 without token, header and query tokens, stale token after change), connect-to-protected-share flow (probe 401 → password required, wrong password rejected and nothing persisted, correct password verified against the peer's unlock endpoint and stored, open share skips unlock, peer 429 surfaced), `_peer_auth_headers` (unlock with stored password, skip when unprotected, changed-password surfaced as PeerUnavailable), CLI flags (`--password`, `--generate-password`, `set-password`/`--clear`)

## Notes / limitations

- The create probe really creates (and deletes) a tunnel when the token has create rights; throwaway name `share-files-verify-<8 hex>`
- Format-level rejections: API tokens are ~40 characters, account-owned start with `cfat_`, a bare 32-hex value is an account id
- The running server picks up config changes (setup/reset/toggle) per request via an mtime check - loading NEW code still requires package reinstall + server restart
- Daemon output goes to `/tmp/cloudflared-share-files.log`; production deployments may prefer a managed service (systemd/docker) over the extension-spawned process
- AC-CF9/AC-CF10 specified but need a second peer to exercise; the building blocks are live-verified under AC-CF8
