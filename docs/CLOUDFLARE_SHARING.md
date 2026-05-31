# Sharing Beyond the Hub - Cloudflare Roadmap

## Overview

Today share and request links live under the owner's `/user/<name>/` path and are only reachable through the JupyterHub proxy, so they work for people on the same hub but not for outside recipients. The plan is to expose the extension's public endpoints through Cloudflare so a link can be opened by anyone, regardless of hub access. This document records the direction and the concrete first step.

**Implementation status:** roadmap only. No code yet.

## How links are generated today

The extension does not store its own address - it infers the link per request. `_public_origin` (`jupyterlab_share_files_extension/routes.py`) reads `X-Forwarded-Proto` and `X-Forwarded-Host`, falling back to `request.host`, and `_public_share_url` / `_public_request_url` join that origin with the namespaced public path. When a browser reaches the panel through the hub proxy, the proxy stamps the public host and `/user/<name>/` prefix, so the link comes out correct. A request that bypasses the proxy (for example a direct internal call) produces an internal, unshareable link.

This inference is the hinge the Cloudflare work turns on.

## Two ways Cloudflare fits

A Cloudflare Tunnel that forwards `X-Forwarded-Host`/`-Proto` set to the Cloudflare hostname would make links come out as the public Cloudflare URL automatically, with no code change. This works but is fragile - it depends on the proxy chain setting the headers exactly right, and there is no single place that declares the canonical external address.

The deterministic alternative is a configuration trait that names the external base directly, so the extension stops inferring and always emits the same external link. This is the recommended first step.

## First step - `public_base_url` config trait

Add an optional trait to `ShareFilesConfig` (`jupyterlab_share_files_extension/config.py`) that, when set, overrides the inferred origin for all generated links.

- **Trait:** `public_base_url` (Unicode, default empty) - the external base the links should use, for example `https://share.example.com/user/<name>/` (Cloudflare hostname).
- **Wiring:** `_public_origin` / `_public_share_url` / `_public_request_url` in `routes.py` prefer `public_base_url` when it is non-empty, and fall back to the current `X-Forwarded-*` inference otherwise. The path suffix (`jupyterlab-share-files-extension/public/<kind>/<id>`) is unchanged.
- **Behaviour:** unset → today's behaviour exactly (no regression); set → every share/request link, the standalone pages, and the manifests resolve to the configured external base.

Set it in `jupyter_server_config.py`:

```python
c.ShareFilesConfig.public_base_url = "https://share.example.com/user/alice/"
```

Tests would mirror the existing `tests/test_config.py` trait coverage plus a `routes` assertion that a set `public_base_url` wins over forwarded headers.

## Considerations to settle when building

- **Link protection.** External exposure reopens the question deferred earlier - the link is currently a 40-bit capability URL with no PIN or expiry. Cloudflare Access or signed URLs could add auth or TTL at the edge without changing the app. Decide whether reaching outside the hub raises that bar.
- **Avoiding the hub spawn breach.** The earlier spawn-as-owner issue was specific to traffic routed through `/user/<owner>/`. A Cloudflare-fronted path that does not traverse the hub's per-user auth sidesteps that class of problem. Whether the tunnel targets the single-user server or a dedicated standalone service is part of this decision.
- **MCP server.** No change needed. It hands out whatever link the server generates, so once `public_base_url` is set the agent's `create_share` / `create_request` outputs become externally shareable for free.

## Out of scope for the first step

- The Cloudflare Tunnel / Access configuration itself (infrastructure, not extension code)
- A standalone Hub service decoupled from the single-user server
- Any change to the capability-link protection model
