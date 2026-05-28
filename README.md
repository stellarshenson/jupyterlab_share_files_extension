# jupyterlab_share_files_extension

[![GitHub Actions](https://github.com/stellarshenson/jupyterlab_share_files_extension/actions/workflows/build.yml/badge.svg)](https://github.com/stellarshenson/jupyterlab_share_files_extension/actions/workflows/build.yml)
[![npm version](https://img.shields.io/npm/v/jupyterlab_share_files_extension.svg)](https://www.npmjs.com/package/jupyterlab_share_files_extension)
[![PyPI version](https://img.shields.io/pypi/v/jupyterlab-share-files-extension.svg)](https://pypi.org/project/jupyterlab-share-files-extension/)
[![Total PyPI downloads](https://static.pepy.tech/badge/jupyterlab-share-files-extension)](https://pepy.tech/project/jupyterlab-share-files-extension)
[![JupyterLab 4](https://img.shields.io/badge/JupyterLab-4-orange.svg)](https://jupyterlab.readthedocs.io/en/stable/)
[![Brought To You By KOLOMOLO](https://img.shields.io/badge/Brought%20To%20You%20By-KOLOMOLO-00ffff?style=flat)](https://kolomolo.com)
[![Donate PayPal](https://img.shields.io/badge/Donate-PayPal-blue?style=flat)](https://www.paypal.com/donate/?hosted_button_id=B4KPBJDLLXTSA)

> [!TIP]
> Part of [stellars_jupyterlab_extensions](https://github.com/stellarshenson/stellars_jupyterlab_extensions). Install all at once: `pip install stellars_jupyterlab_extensions`

AirDrop for JupyterLab. Create a **share** (file drop) or **request** (inbox), copy the link, paste it in chat. Recipients open it in their JupyterLab panel or any plain browser.

## Features

- **Shares** - read-only file/folder drops; recipients download
- **Requests** - inboxes; recipients upload, organised per uploader
- **Connections** - paste someone's link to subscribe to their share or upload to their request
- **Drag-and-drop** from the file browser - drop zone (new share), share row (add files), request row (upload)
- **Right-click context menu** on the file browser ("Share Files..."), and on panel entries ("Copy to Current Folder", "Show in File Browser")
- **Standalone HTML page** - link works in any browser, no JupyterLab needed
- **Live upload notifications** when someone uploads to your request
- **Symlink-friendly** - sharing `@shared/...` and similar works
- **Settings toggles** - turn shares or requests on/off independently

## Install

Requires JupyterLab 4.0+.

```bash
pip install jupyterlab_share_files_extension
```

## Storage

Files live under `<server_root_dir>/uploads/<shares|requests>/<slug>-<id>/`. Each share/request is identified by an 8-char base32 token; the token is the credential.

Change the location via `jupyter_server_config.py`:

```python
c.ShareFilesConfig.shares_dir = "/path/to/storage"
```

## Settings

**Settings → Settings Editor → Share Files** toggles `enableShares` and `enableRequests` independently (both default on). Connections stay available either way.

## Security

The link is the credential (40 bits of entropy). HTTPS is inherited from your JupyterHub/Jupyter proxy. Suitable for trusted-channel sharing (Slack, email). No expiry, no PIN.

## Uninstall

```bash
pip uninstall jupyterlab_share_files_extension
```
