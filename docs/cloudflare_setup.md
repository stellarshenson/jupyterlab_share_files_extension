# Cloudflare Configuration

How to configure a Cloudflare account and API token so the extension can
expose share/request links beyond your hub or local network through a
Cloudflare tunnel. Once configured, `jupyterlab_share_files cloudflare
setup` provisions everything and generated links carry the public Cloudflare
hostname. Six orthogonal subcommands cover the whole lifecycle: `setup`,
`validate`, `info`, `start`, `stop`, `reset`.

## Prerequisites

- A Cloudflare account (the free plan is sufficient)
- A domain managed in that account (its nameservers delegated to Cloudflare) -
  the share hostname will be a subdomain of it, e.g. `share.example.com`
- `cloudflared` installed on the machine that will run the connector
  ([Cloudflare's install guide](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/))
- The extension's package installed, so the `jupyterlab_share_files` console
  script is on PATH

## API token policies

Create the token in the Cloudflare dashboard (My Profile → API Tokens, or
Account API Tokens for an account-owned token - both kinds work). It needs
exactly two permission policies:

| Scope              | Permission        | Level | Governs                                                     |
| ------------------ | ----------------- | ----- | ----------------------------------------------------------- |
| Account            | Cloudflare Tunnel | Edit  | create/read the tunnel, its ingress config, connector token |
| Zone (your domain) | DNS               | Edit  | the proxied CNAME routing the hostname to the tunnel        |

Caveats learned the hard way:

- **"Connectivity Directory" is a different product** from "Cloudflare
  Tunnel" (it covers WARP/device tunnels). A token with Connectivity
  Directory Admin still gets `10000: Authentication error` when creating a
  tunnel - it is the _Cloudflare Tunnel_ permission group that matters
- **Account-scoped DNS permissions do not govern zone records.** DNS View,
  Account DNS Settings and DNS Firewall at account level all leave the CNAME
  write failing; the policy must be **zone-scoped DNS Edit** on the domain
- Account-owned tokens (`cfat_...`) do not verify at `/user/tokens/verify`;
  the CLI handles this automatically by falling back to
  `/accounts/{id}/tokens/verify`

## Required configuration

- `--private-base-url` (mandatory for `setup`) - the base URL of the Jupyter
  server as the `cloudflared` connector reaches it (on JupyterHub:
  `https://hub.example.com/user/<name>/`). It is given explicitly, never
  inferred from the environment, so an internal address cannot sneak in by
  accident. It **must be `https`** - an `http` URL is refused with guidance.
  `localhost` is acceptable when the server (and connector) genuinely run
  there, as long as it is served over https
- The Cloudflare token and account id, given to `setup` once and stored in
  `~/.config/jupyterlab-share-files/config.json` (file mode `600`)

## Commands

Output is human-readable; add the global `--json` flag for machine-readable
JSON (`jupyterlab_share_files --json cloudflare info`).

```bash
# provision: save credentials, tunnel + DNS + HTTPS enforcement + link rewriting + daemon
jupyterlab_share_files cloudflare setup --token <api-token> --account-id <account-id> \
  --hostname share.example.com --private-base-url "https://hub.example.com/user/<name>/"

# end-to-end check of the saved config (creates a test tunnel and removes it)
jupyterlab_share_files cloudflare validate

# current configuration - tokens masked to their last 4 characters,
# daemon_running (cloudflared process) and tunnel_status (Cloudflare-side)
jupyterlab_share_files cloudflare info

# switch to public links: mark the tunnel active and start the daemon
jupyterlab_share_files cloudflare start

# switch to private links: stop the daemon; credentials, tunnel and DNS kept
jupyterlab_share_files cloudflare stop

# back to the unconfigured state (links revert to the local/hub address)
jupyterlab_share_files cloudflare reset
```

`validate` reports `token_valid`, `can_bind_existing` (can list tunnels),
`can_create_tunnel` - the last proven by creating a test tunnel and removing
it, so a policy gap shows up here and not halfway through setup - and
`cloudflared_available`/`cloudflared_path` (the extension launches the
connector itself; a binary missing from the server's PATH means the tunnel
can never come up).

`setup` creates or reuses a tunnel named `share-files-<sluggified
private base URL>` (e.g. `share-files-hub-example-com-user-alice` -
deterministic so repeated setups reuse it, unique per user/server on a
shared account), routes the hostname
to the origin, upserts a proxied CNAME `<hostname> → <tunnel>.cfargotunnel.com`,
switches the zone's **Always Use HTTPS** on (plain `http` requests are
301-redirected at the Cloudflare edge - only secure connections reach a
share), fetches the connector token, saves
`public_base_url = https://<hostname>`, and starts the connector daemon. The
running server picks the value up on the next request - no restart.

## Connector daemon lifecycle

The extension guarantees the `cloudflared` daemon runs: at server startup
(when Cloudflare sharing is configured) and right after `setup` it checks for
a running connector and otherwise launches `cloudflared tunnel run`, retrying
up to `c.ShareFilesConfig.cloudflared_retries` times (default 3, set in
`jupyter_server_config.py`). All attempts failing is logged as an error -
Cloudflare links will not work until the daemon runs; success is logged as
info. Daemon output goes to `/tmp/cloudflared-share-files.log`; `cloudflare
info` shows both `daemon_running` and the Cloudflare-side `tunnel_status`.

Autostart is a user setting: Settings Editor → Share Files → "Start the
Cloudflare tunnel automatically" (default off). The server starts with the
tunnel inactive - links stay private until switched on via the cloud icon
or `cloudflare start`.

## Public/private toggle

`start`/`stop` (and the cloud icon in the panel header, left of the filter
icon) switch between the two link modes without touching credentials,
tunnel or DNS. The icon is always visible; while nothing is configured,
clicking it opens a setup popup with the same inputs as `cloudflare setup`
(token, account id, public hostname, private base URL - each with a hint
where to find the value):

- **on** (`start`, green filled cloud) - daemon running, generated links
  carry the public hostname
- **off** (`stop`, dim dashed cloud) - daemon stopped, links carry the
  private/request address
- **connecting** - blinking blue cloud while a switch-on is in flight

The toggle is read per request (mtime-cached config file), so it takes
effect on the next request in either direction - no restart. The same
switch is exposed to the frontend as `POST api/tunnel {"active": bool}`.
The link dialog also probes reachability server-side (`api/link-check`)
and shows "Link is reachable" / "not reachable" for the displayed link.

## What is exposed

Only the extension's unauthenticated `/public/...` capability endpoints pass
through the tunnel - the ingress carries a path rule
(`^(/user/[^/]+)?/jupyterlab-share-files-extension/public/.*`) and a catch-all 404. The hub login, the authenticated `/api/*` endpoints, and everything else
on the private network answer 404 at the Cloudflare edge. The ingress also
sets the origin's own hostname as HTTP Host header and TLS SNI
(`httpHostHeader` / `originRequest.originServerName`), so a reverse proxy in
front of the hub routes the forwarded requests correctly; `noTLSVerify` is
enabled for `https` origins to tolerate self-signed hub certificates.

The share/request link itself remains the only credential (40 bits of
entropy), now reachable from the whole internet - share it over trusted
channels.
