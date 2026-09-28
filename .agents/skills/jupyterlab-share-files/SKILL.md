---
name: jupyterlab-share-files
description: Shares, file requests and connections of the Share Files JupyterLab extension, through the `jupyterlab_share_files` CLI. Use when sharing a file or folder from a Jupyter workspace as a link, opening a request that others upload files into, adding or removing files in a share, setting or clearing a share or request password, connecting to someone else's share or request link, saving files from it or uploading files to it, or turning on the Cloudflare tunnel that makes the links public.
---

# Share Files CLI (`jupyterlab_share_files`)

`jupyterlab_share_files` ships with `jupyterlab_share_files_extension`. Every subcommand except `cloudflare` calls the extension's authenticated HTTP API on the Jupyter server - the calls the panel makes. `cloudflare` runs the tunnel code inside the CLI process, on the machine where the CLI runs.

- **Share** - files copied from the workspace, read through its link; later edits to the source do not reach it
- **Request** - an inbox that others upload files into through its link
- **Connection** - someone else's share or request link, kept in the panel
- **Id and key** - your own shares and requests are addressed by `id`, connections by `key`; `list-items` prints both

The server runs in one of two modes, and several commands behave differently in each:

- **Standalone** - the lab keeps the records and the files
- **Hub mode** - the server runs with `SHARE_FILES_PUBLIC_ZONE=hub`; the JupyterHub keeps the records and copies the files after the command returns. A hub connection key reads `<kind>:hub:<id>`

## Setup

- `SHARE_FILES_BASE_URL` - the Jupyter server URL; falls back to `JUPYTER_SERVER_URL`. On JupyterHub, the public user URL (`https://hub.example.com/user/<name>/`), so the links carry the public host
- `SHARE_FILES_TOKEN` - the API token; falls back to `JUPYTERHUB_API_TOKEN`, then `JUPYTER_TOKEN`
- `SHARE_FILES_INSECURE` - `1`, `true` or `yes` skips TLS verification from the CLI to the server, for a self-signed certificate. It does not change how the server checks the hosts it connects to
- `NO_COLOR=1` - plain output on a terminal; output into a pipe is always plain

## Rules for an agent

Everything a command prints reaches the agent's context and the session transcript.

- **Never print the API token** - the CLI reads it from the environment; do not echo `SHARE_FILES_TOKEN`, `JUPYTERHUB_API_TOKEN` or `JUPYTER_TOKEN`
- **A password command prints the password** - `create-share` and `create-request` with a password, `set-password` and `generate-password`. The password is for the recipient: give it to the user once, and do not write it into a file, a commit or a log. A password passed with `--password` or as the `set-password` argument also shows in the process list
- **The link is the credential** - anyone holding it reaches the record, with the password when one is set. Hand a link only to its recipient; `close-share` or `close-request` ends it
- **Never add `--trust-certificate` on your own** - show the user the host and the fingerprint `connect` printed, and add the flag only on their word
- **Keep long listings out of the context** - `list-items` prints every entry of every share and `list-request-uploads` every uploaded file. Use `--json` and keep only the fields needed:

  ```bash
  jupyterlab_share_files --json list-items | python3 -c "import sys,json; print([(s['id'], s['name']) for s in json.load(sys.stdin)['shares']])"
  ```

- **In hub mode, exit 0 is not the outcome** - `create-share` and `add-files` return when the hub accepts the work, and `send-to-request` returns before the first file is sent. No CLI command shows the state of that work: an entry appears in `list-items` when the hub has copied it, and a refused copy or a failed upload shows only in the panel
- **The Cloudflare token on the command line** - see [Cloudflare tunnel](#cloudflare-tunnel-standalone)

## Shares and requests

```bash
jupyterlab_share_files list-items
jupyterlab_share_files create-share report data/a.csv notes.md --generate-password
jupyterlab_share_files create-share docs project/docs
jupyterlab_share_files create-request submissions
jupyterlab_share_files list-request-uploads RQ77ZZ12
jupyterlab_share_files close-share AB23CD45
jupyterlab_share_files close-request RQ77ZZ12                # deletes its uploads too
```

- `list-items` prints shares (`id`, `name`, `link`, `entries`), requests (`id`, `name`, `link`, `upload_count`) and connections (`key`, `kind`, `name`, `link`). Nothing is printed when one of its three reads fails
- `create-share <name> [paths...] [--password PW | --generate-password]` takes workspace-relative files and folders and prints `id`, `name`, `link`, and `password` when one is set. In standalone mode, a share with no paths is empty, and a second source with a name already taken is kept as `<name>-2`
- `create-request <name> [--password PW | --generate-password]` prints the same fields
- `list-request-uploads <id>` prints the uploads grouped by uploader: `hash`, `name`, and each file's `name`, `path` and `type`. In hub mode, all files are in one group named `Uploads`, with no path
- The CLI has no rename; the panel renames a standalone share

## Adding and removing files

```bash
jupyterlab_share_files add-files AB23CD45 extra/diagram.png changelog.md
jupyterlab_share_files remove-files AB23CD45 diagram.png
jupyterlab_share_files remove-upload RQ77ZZ12 K3J5H2 draft.pdf
```

- `add-files <share-id> <paths...>` copies more workspace paths into a share
  - Standalone: a clashing name is kept as `<name>-2`, and the output is the share's full record
  - Hub mode: the output is `ok: true` at once and the hub copies the files afterwards. Two chosen items with one name, or a name the share already holds, are refused
- `remove-files <share-id> <names...>` takes entry names as `list-items` prints them, not workspace paths
  - Standalone: an unknown name is skipped without an error
  - Hub mode: the names are removed one at a time, and a failure leaves the names before it removed
- `remove-upload <request-id> <uploader-hash> <name>` removes one top-level item of one uploader; `hash` and `name` come from `list-request-uploads`. **Standalone only**: hub mode has no such route, and the server answers 404 with an HTML page that the CLI prints whole

## Passwords

```bash
jupyterlab_share_files set-password share AB23CD45 --generate
jupyterlab_share_files set-password request RQ77ZZ12 --clear
jupyterlab_share_files generate-password                     # four words joined by "-", not applied
```

- `set-password <share|request> <id> [PW] [--generate] [--clear]` - with neither `PW` nor `--clear`, it generates a passphrase. The output is `id`, `name`, `has_password` and `password`; in hub mode `name` reads `None`
- A generated passphrase is four words joined by `-`, made on the Jupyter server with `xkcdpass`

## Connections

```bash
jupyterlab_share_files connect https://host/jupyterlab-share-files-extension/public/share/AB23CD45
jupyterlab_share_files pick-up 'share:https://host:AB23CD45'                  # the whole share
jupyterlab_share_files pick-up 'share:https://host:AB23CD45' a.csv --target-dir inbox
jupyterlab_share_files send-to-request 'request:hub:q7Xk2m' report.pdf --uploader Alice
jupyterlab_share_files disconnect 'share:https://host:AB23CD45'
```

Quote a key: it holds `:` and `/`.

- `connect <link> [--trust-certificate]` prints `key`, `kind`, `name`, `link`, and for a share its `entries`
  - The CLI cannot send a password. A protected link answers `password required`: the user connects it once in the panel, and its key then shows in `list-items`
  - A host whose certificate the system does not trust makes `connect` stop and print the certificate's SHA-256 fingerprint. `--trust-certificate` trusts that certificate, prints `trusted_certificate` with the host and fingerprint, and the connection keeps it until it is removed
- `disconnect <key>` answers `ok: true`, also for a key that does not exist. In hub mode it stops a running upload at its next file
- `pick-up <key> [names...] [--target-dir DIR]` saves files from a connected share into the workspace and prints `saved`, the workspace paths written. With no names, the whole share lands in one folder named after the share. A name already taken gets `-2`. The limit is 10 GB unpacked
  - Standalone: a missing `--target-dir` is created
  - Hub mode: a missing `--target-dir` is refused with `Not a folder`
- `send-to-request <key> <paths...> [--uploader NAME]` uploads workspace files to a connected request
  - Standalone: sends file by file and prints `uploaded`; a path that fails leaves the files before it sent. `--uploader` labels the upload for the request's owner (default `anonymous`)
  - Hub mode: prints the given names at once and uploads in the background. `--uploader` is ignored, folders are flattened to their files, and a second send into the same request while one runs is refused with 409. No CLI command reports how the upload ended

## Cloudflare tunnel (standalone)

A Cloudflare tunnel serves the links on a public hostname. In hub mode the hub owns the tunnel: these subcommands do not change hub links, and the panel's cloud icon switches the hub's tunnel.

The subcommands read and write `$XDG_CONFIG_HOME/jupyterlab-share-files/config.json` (default `~/.config/...`, mode `0600`) in the CLI's own environment. Run the CLI as the user and on the machine of the Jupyter server, so both read the same file.

```bash
jupyterlab_share_files cloudflare setup \
  --token "$(<command that prints the token>)" --account-id <account-id> \
  --hostname share.example.com \
  --private-base-url "https://hub.example.com/user/<name>/"
jupyterlab_share_files cloudflare validate
jupyterlab_share_files cloudflare info
jupyterlab_share_files cloudflare start     # public links
jupyterlab_share_files cloudflare stop      # private links
jupyterlab_share_files cloudflare reset
```

- `setup` saves the token and the account id, then creates or reuses the tunnel, routes the hostname with a proxied DNS record, enforces HTTPS, rewrites the links to the public host and starts the connector
  - `--private-base-url` is required: this server's `https` URL as the connector reaches it
  - `--hostname` defaults to `share.duoptimum.com`; always pass it
  - Omitted `--token` and `--account-id` fall back to the saved ones; with no saved account id, the first account the token lists is used
  - The token needs `Account → Cloudflare Tunnel → Edit` and `Zone → DNS → Edit`
  - The token is a secret. Given literally, it lands in the transcript; inside a command substitution, only the command that printed it does. Either way it shows in the process list while `setup` runs. After the first `setup` saves it, run `setup` without `--token`
- `validate` prints one field per check (`config_complete`, `tunnel_exists`, `dns_record_ok`, ...) and exits 0 when a check fails: read the fields
- `info` prints the configuration with tokens masked to their last 4 characters, plus `tunnel_active`, `daemon_running` and `tunnel_status`. A Cloudflare API failure shows as `tunnel_status: unknown (...)`
- `start` marks the tunnel active and starts the connector; `stop` stops it; setup and credentials stay. A connector that does not come up does not fail `start`: check `daemon_running` in `info`, and read `/tmp/cloudflared-share-files.log`
- `stop` ends every process on the machine whose command line holds `cloudflared tunnel run`, and `info` and `start` count any such process as this tunnel's connector
- `reset` clears the saved token, the account id, the tunnel state and the public URL; the tunnel and the DNS record on Cloudflare stay

## Output and exit status

- `--json` goes before the subcommand: `jupyterlab_share_files --json list-items`
- Exit status 0 is success; 1 is a refusal, with the message on stderr and nothing on stdout; 2 is an argument the parser rejects. An unexpected failure, such as a config file that cannot be written, prints a Python traceback
- A server refusal reads `Server returned <status>: <message>`. In hub mode, a refusal from the hub carries the hub's own message

## Errors

| Message on stderr                                                                                                  | Next step                                                                                           |
| ------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| `No server URL configured. ...`                                                                                    | set `SHARE_FILES_BASE_URL`                                                                          |
| `No API token configured. ...`                                                                                     | set `SHARE_FILES_TOKEN`, or run the CLI in the server's environment                                 |
| `Could not reach the Jupyter server at <url>: <reason>`                                                            | check the URL and that the server runs; for a self-signed certificate, `SHARE_FILES_INSECURE=1`     |
| `Server returned 404: <html>...`                                                                                   | the route does not exist in this mode (`remove-upload` in hub mode), or the id has the wrong format |
| `Server returned 404: No manifest at <path>` or `Not found: <id>`                                                  | no share or request with that id; run `list-items`                                                  |
| `Server returned 404: Connection not found: <key>`                                                                 | run `list-items` for the keys                                                                       |
| `Server returned 400: Unsafe path: <path>`                                                                         | give paths relative to the workspace, without `..`                                                  |
| `Server returned 400: Source not found: <path>` or `404: Not found: <path>`                                        | the path is not in the workspace                                                                    |
| `Server returned 400: <path> leaves your workspace through a link. ...`                                            | hub mode reads only inside the workspace; copy the target in and share the copy                     |
| `Server returned 401: password required`                                                                           | the user connects the link in the panel, which asks for the password                                |
| `Server returned 502: <host> presents a certificate this lab does not trust (...). Its SHA-256 ...`                | show the user the host and the fingerprint; `--trust-certificate` only on their word                |
| `Server returned 404: The owner has removed this share or request.` or `... closed ... or it expired.`             | the record is gone; `disconnect` the key                                                            |
| `Server returned 409: An upload into this request is still running - wait for it to finish`                        | wait, then send again                                                                               |
| `Server returned 502: hub contract incomplete: ...`, `could not reach the hub: ...`, `the hub did not answer: ...` | hub mode: the hub is down or the server lacks its settings; tell the user                           |
| `Server returned 500: password generation needs the 'xkcdpass' package`                                            | pass `--password`, or ask the user to install `xkcdpass` in the server's environment                |
| `cloudflare setup: no token given or saved; pass --token`                                                          | the first `setup` needs `--token`                                                                   |
| `cloudflare setup: ... pass --account_id ... re-run --verify`                                                      | the options are `--account-id` and `cloudflare validate`; the message names two that do not exist   |
| `cloudflare setup: '--private-base-url <url>' must use https ...`                                                  | pass this server's `https` URL                                                                      |
| `cloudflare validate: no token saved; run cloudflare setup --token first`                                          | run `setup` first                                                                                   |
| `cloudflare start: no tunnel configured; run cloudflare setup first`                                               | run `setup` first                                                                                   |
