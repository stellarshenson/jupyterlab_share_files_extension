# Cloudflare Configuration

How to configure a Cloudflare account and API token so the extension can
expose share/request links beyond your hub or local network through a
Cloudflare tunnel. Once configured, `jupyterlab_share_files cloudflare
--setup` provisions everything and generated links carry the public
Cloudflare hostname.

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

- `--local-base-url` (mandatory with `--setup`) - the base URL of the Jupyter
  server as the `cloudflared` connector reaches it (on JupyterHub:
  `https://hub.example.com/user/<name>/`). It is given explicitly, never
  inferred from the environment, so an internal address cannot sneak in by
  accident. It **must be `https`** - an `http` URL is refused with guidance.
  `localhost` is acceptable when the server (and connector) genuinely run
  there, as long as it is served over https
- The Cloudflare token and account id, saved once via the CLI (stored in
  `~/.config/jupyterlab-share-files/config.json`, file mode `600`)

## Commands

```bash
# 1. save credentials and check what the token can do
jupyterlab_share_files cloudflare --token <api-token> --account_id <account-id> --verify

# 2. provision: tunnel + DNS + HTTPS enforcement + link rewriting (and start the connector)
jupyterlab_share_files cloudflare --setup --hostname share.example.com \
  --local-base-url "https://hub.example.com/user/<name>/" --run

# back to the unconfigured state (links revert to the local/hub address)
jupyterlab_share_files cloudflare --reset
```

`--verify` reports `token_valid`, `can_bind_existing` (can list tunnels) and
`can_create_tunnel` - the last proven by creating a throwaway tunnel and
deleting it, so a policy gap shows up here and not halfway through setup.

`--setup` creates or reuses a tunnel named `share-files`, routes the hostname
to the origin, upserts a proxied CNAME `<hostname> → <tunnel>.cfargotunnel.com`,
switches the zone's **Always Use HTTPS** on (plain `http` requests are
301-redirected at the Cloudflare edge - only secure connections reach a
share), fetches the connector token, and saves
`public_base_url = https://<hostname>`.
The running server picks the value up on the next request - no restart. Without
`--run` it prints the `cloudflared tunnel run --token <...>` command to start
the connector yourself (production should run it as a managed service).

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
