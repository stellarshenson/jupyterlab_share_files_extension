---
name: jupyterlab_share_files
description: Operate the Share Files JupyterLab extension from the command line - create and manage shares and requests, add or remove files, set passwords, connect to peers, and turn on or configure Cloudflare tunnel sharing. Use when asked to share a file or folder, make a request inbox, add/remove files from a share, set a share password, expose links publicly via Cloudflare, or otherwise drive the `jupyterlab_share_files` CLI.
---

# Share Files CLI

The `jupyterlab_share_files` command is a thin authenticated client over the extension's HTTP API - every panel operation is a subcommand. Use it for scripts, automation, and agent-driven sharing. Human-readable output by default; `--json` (placed before the subcommand) for machine-readable output.

This skill ships inside the package. Install it into your own `~/.claude/skills/` with `jupyterlab_share_files install-claude-skill` - it asks for confirmation before writing.

## Setup

The CLI needs the server URL and an API token, taken from environment variables.

- **`SHARE_FILES_BASE_URL`** - base URL of the Jupyter server; falls back to `JUPYTER_SERVER_URL`
- **`SHARE_FILES_TOKEN`** - API token; falls back to `JUPYTERHUB_API_TOKEN` / `JUPYTER_TOKEN`
- **`SHARE_FILES_INSECURE=1`** - skip TLS verification (self-signed certificates); off by default
- **JupyterHub** - `SHARE_FILES_BASE_URL` must be the public user URL (e.g. `https://hub.example.com/user/<name>/`) so generated links carry the public host

Inside a running single-user server these are usually already set:

```bash
export SHARE_FILES_BASE_URL="$JUPYTER_SERVER_URL"
export SHARE_FILES_TOKEN="$JUPYTERHUB_API_TOKEN"
```

Run `jupyterlab_share_files --help` for the live command list, `jupyterlab_share_files <command> --help` for a command's flags.

## Key facts

- **Shares copy, never reference** - files are copied into the share's own pool under `shares_dir`; editing or deleting the source afterwards does not change what recipients get
- **The link is the credential** - 40 bits of entropy, no expiry; share over trusted channels
- **Passwords are plaintext, owner-only** - shown to the owner (link dialog / `set-password`), never returned by public endpoints; brute force is rate limited server-side
- **Ids vs keys** - your own shares/requests are addressed by **id** (e.g. `AB23CD45`); connections to other people's shares/requests are addressed by **key** (from `list-items`)

## Shares and requests

- **`list-items`** - list your shares, your requests (with upload counts), and your connections, with ids, keys and links
- **`create-share <name> [paths...] [--password PW | --generate-password]`** - create a read-only drop from workspace-relative paths
- **`create-request <name> [--password PW | --generate-password]`** - create an inbox recipients upload into
- **`close-share <id>`** / **`close-request <id>`** - delete one of your own shares/requests (a request deletes with its uploads)

```bash
# share two files, generated passphrase
jupyterlab_share_files create-share report data/a.csv notes.md --generate-password

# share a whole folder
jupyterlab_share_files create-share docs project/docs

# open an inbox
jupyterlab_share_files create-request submissions
```

## Add and remove files

- **`add-files <share-id> <paths...>`** - copy more workspace paths into an existing share (same isolated-pool copy as create)
- **`remove-files <share-id> <names...>`** - remove top-level entries from a share by **name** (the names shown in the manifest / `list-items`, not workspace paths)
- **`remove-upload <request-id> <uploader-hash> <name>`** - remove a single uploaded file from one of your requests (uploader hash + file name come from `list-request-uploads`; uploaders are keyed by a server-issued hash, the display name is just a label)

```bash
jupyterlab_share_files add-files AB23CD45 extra/diagram.png changelog.md
jupyterlab_share_files remove-files AB23CD45 diagram.png
jupyterlab_share_files list-request-uploads RQ77ZZ12
jupyterlab_share_files remove-upload RQ77ZZ12 K3J5H2 draft.pdf
```

## Passwords

- **`set-password <share|request> <id> [PW] [--generate] [--clear]`** - set, change, or clear; `--generate` makes an xkcd-style passphrase, `--clear` removes it
- **`generate-password`** - print a passphrase without applying it
- Changing or clearing a password instantly locks out everyone holding the old one (the unlock token is bound to the password)

```bash
jupyterlab_share_files set-password share AB23CD45 --generate
jupyterlab_share_files set-password share AB23CD45 --clear
```

## Connections (other people's links)

- **`connect <link>`** - subscribe to someone's share or upload to their request; prompts for the password if the peer requires one
- **`disconnect <key>`** - remove a connection
- **`pick-up <key> [names...] [--target-dir DIR]`** - save files from a connected share into your workspace (all files if no names given)
- **`send-to-request <key> <paths...> [--uploader NAME]`** - upload files to a connected request
- **`list-request-uploads <id>`** - list files uploaded to one of *your* requests, grouped by uploader

## Cloudflare tunnel sharing

Exposes the `/public/...` links beyond the hub or local network through a Cloudflare tunnel: outbound-only connector, HTTPS enforced at the edge, only the unauthenticated public endpoints are routable. Six orthogonal subcommands cover the lifecycle. Token policies required: `Account → Cloudflare Tunnel → Edit` plus zone-scoped `DNS → Edit` for the hostname's domain.

- **`cloudflare setup --token <T> --account-id <A> [--hostname <H>] --private-base-url <URL>`** - save credentials (chmod-600 config) and provision end to end: create/reuse the tunnel, route the hostname, add a proxied CNAME, enforce HTTPS, save `public_base_url`, start the connector. `--private-base-url` is **required**, must be `https`, and is this server's URL as the connector reaches it (e.g. `https://hub.example.com/user/<name>/`)
- **`cloudflare validate`** - end-to-end check of the saved config: token validity, tunnel existence/status/name, proxied CNAME, ingress rule, `cloudflared` binary, daemon/toggle state
- **`cloudflare info`** - current configuration; tokens masked to last 4 chars; shows `tunnel_active`, `daemon_running`, Cloudflare-side `tunnel_status`
- **`cloudflare start`** - switch to public links: mark the tunnel active and start the cloudflared daemon
- **`cloudflare stop`** - switch to private links: stop the daemon; credentials, tunnel and DNS are kept
- **`cloudflare reset`** - clear the saved token and derived state (account id, tunnel state, `public_base_url`); links revert to hub-local; Cloudflare-side resources untouched

```bash
# turn it on / configure end to end
jupyterlab_share_files cloudflare setup \
  --token <api-token> --account-id <account-id> \
  --hostname share.example.com \
  --private-base-url "https://hub.example.com/user/<name>/"

# check it, then toggle public/private
jupyterlab_share_files cloudflare validate
jupyterlab_share_files cloudflare info
jupyterlab_share_files cloudflare start   # public links
jupyterlab_share_files cloudflare stop    # private links
```

Once configured, generated links automatically use the Cloudflare host while the tunnel is active; `stop` (or `reset`) returns links to the hub-local address. No server restart needed for the toggle.

## Scripting

Put `--json` before the subcommand and parse stdout:

```bash
ID=$(jupyterlab_share_files --json create-share tmp file.txt | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
jupyterlab_share_files --json list-items
```

Non-zero exit on failure; the error message goes to stderr. `NO_COLOR=1` disables colour for clean capture.
