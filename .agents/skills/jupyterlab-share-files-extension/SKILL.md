---
name: jupyterlab-share-files-extension
description: Shares, file requests and connections of the Share Files JupyterLab extension, through the `jupyterlab_share_files` CLI of jupyterlab_share_files_extension. Use when sharing a file or folder from a Jupyter workspace as a link, opening a request that others upload files into, adding or removing files in a share, setting or clearing a share or request password, connecting to someone else's share or request link, saving files from it or uploading files to it, or turning on the Cloudflare tunnel that makes the links public.
---

# jupyterlab_share_files

Runs wherever the Jupyter server is reachable; calls the extension's authenticated API. `cloudflare` runs tunnel code in the CLI process, on the CLI's machine. Commands, flags, output, modes, errors, environment: `jupyterlab_share_files --help`, `jupyterlab_share_files <command> --help`. Read first.

## Rules

- Command output goes into transcript. Never echo `SHARE_FILES_TOKEN`, `JUPYTERHUB_API_TOKEN`, `JUPYTER_TOKEN`
- Password commands print the password: `create-share` and `create-request` with a password, `set-password`, `generate-password`. Give it to user once. Never into file, commit, log
- Link is the credential. Hand it only to its recipient
- `list-items`, `list-request-uploads` output long. Use `--json`, keep needed fields
- Ask user before `close-share`, `close-request`, `remove-files`, `remove-upload`, `cloudflare reset`. Cannot undo
- Never add `--trust-certificate` alone. Show user host and fingerprint; add on their word
- Hub mode: exit 0 means accepted, not done. `create-share`, `add-files`, `send-to-request` finish later; result shows in `list-items` or the panel
- Cloudflare token is a secret. Never literal on command line: first `cloudflare setup` takes it from a command substitution, later ones run without `--token`
