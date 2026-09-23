"""Tornado handlers for the share-files extension.

Two handler groups:
  * api/*       - authenticated, used by the side panel
  * public/*    - unauthenticated, used by standalone HTML pages and by
                  remote JupyterLab instances that have connected to a link

The public endpoints rely on the share/request ID being secret (8-char base32).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import ssl
import tempfile
import time
import zipfile
import zlib

try:
    import lzma
except ImportError:  # a liblzma-less Python: LZMA members raise NotImplementedError
    lzma = None
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import tornado
import tornado.httpclient
import tornado.httputil
import tornado.simple_httpclient
import tornado.ioloop
import tornado.web
from jupyter_server.base.handlers import APIHandler
from jupyter_server.utils import url_path_join
from tornado.iostream import StreamClosedError
from tornado.simple_httpclient import HTTPStreamClosedError, HTTPTimeoutError
from tornado.web import StaticFileHandler

from .config import EXCLUDED_NAMES, ShareFilesConfig
from .hub import hub_mode
from .storage import (
    SHARES_DIR_NAME,
    ConnectionStore,
    NotFoundError,
    RequestStore,
    ShareStore,
    StorageError,
    _is_safe_relative,
    _resolve_unique_target,
    _safe_name,
    generate_password,
    mint_uploader_hash,
    resolve_shares_dir,
    settle_mode,
    verify_password,
)


EXTENSION_NAMESPACE = "jupyterlab-share-files-extension"


def _shares_dir_setting(handler) -> str:
    """Return the configured shares_dir override (or empty string)."""
    cfg: ShareFilesConfig = handler.settings.get("share_files_config")
    if cfg is None:
        return ""
    return cfg.shares_dir or ""


def _use_trash_setting(handler) -> bool:
    """Return whether deletes should go to the OS trash."""
    cfg: ShareFilesConfig = handler.settings.get("share_files_config")
    if cfg is None:
        return True  # match default
    return bool(cfg.use_trash)


def _excluded_names_setting(handler) -> list[str]:
    """Return the catalogue of names never copied into a share."""
    cfg: ShareFilesConfig = handler.settings.get("share_files_config")
    if cfg is None:
        return list(EXCLUDED_NAMES)  # match default
    return list(cfg.excluded_names)


def _verify_peer_tls_setting(handler) -> bool:
    """Return whether peer (cross-server) TLS certs should be verified."""
    cfg: ShareFilesConfig = handler.settings.get("share_files_config")
    if cfg is None:
        return True  # match default
    return bool(cfg.verify_peer_tls)


class PeerUnavailable(Exception):
    """A server-side fetch to a connected peer could not be completed."""

    # the slug a connected row reads, where the panel shows one
    reason = ""


class PeerRefused(PeerUnavailable):
    """A connected peer answered an upload with an error status; its body
    carries the peer's reason."""

    def __init__(self, code: int, body: bytes):
        super().__init__(f"Upload failed: {code}")
        self.code = code
        self.body = body


class PeerUntrusted(PeerUnavailable):
    """A connected peer's certificate verified neither against the system's
    certificate authorities nor against the certificate the user trusted for
    the connection."""

    reason = "certificate_untrusted"

    def __init__(self, url: str, detail: str):
        self.host = urlparse(url).netloc
        self.detail = detail
        super().__init__(
            f"{self.host} presents a certificate this connection does not trust "
            f"({detail}) - choose Connect Again in its row menu to decide whether to trust it."
        )


def _tls(verify: bool, certificate: str = "") -> dict:
    """The fetch options that check a peer's certificate: against the
    system's certificate authorities, or only against ``certificate`` - the
    PEM the user trusted for the connection; no check with `verify_peer_tls`
    off."""
    if not (verify and certificate):
        return {"validate_cert": verify}
    context = ssl.create_default_context(cadata=certificate)
    # the user trusted this certificate, whatever names it carries
    context.check_hostname = False
    # one a private authority signed is the whole chain on its own, and the
    # user trusted it as it is: the certificate profile checks add nothing
    context.verify_flags = (context.verify_flags | ssl.VERIFY_X509_PARTIAL_CHAIN) & ~ssl.VERIFY_X509_STRICT
    return {"ssl_options": context}


async def _handshake(parts, context: ssl.SSLContext) -> bytes:
    """Open a TLS connection to the host in ``parts`` and return the
    certificate it presents, DER-encoded."""
    _reader, writer = await asyncio.wait_for(
        asyncio.open_connection(parts.hostname, parts.port or 443, ssl=context),
        PEER_TIMEOUT_SECONDS,
    )
    try:
        return writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()


async def peer_certificate(link: str) -> tuple[str, str, str] | None:
    """None when the system's certificate authorities vouch for the host of
    ``link``, or it is not https. Otherwise the certificate the host presents,
    read without checking it: ``(PEM, SHA-256 fingerprint, why the check
    failed)``, the fingerprint colon-separated as browsers show it. Raises
    ``PeerUnavailable`` when the host cannot be reached."""
    parts = urlparse(link)
    if parts.scheme != "https":
        return None
    unchecked = ssl.create_default_context()
    unchecked.check_hostname = False
    unchecked.verify_mode = ssl.CERT_NONE
    try:
        try:
            await _handshake(parts, ssl.create_default_context())
            return None
        except ssl.SSLCertVerificationError as exc:
            detail = (exc.verify_message or str(exc)).rstrip(".")
        der = await _handshake(parts, unchecked)
    except (OSError, asyncio.TimeoutError):
        raise PeerUnavailable("Could not reach the peer") from None
    return ssl.DER_cert_to_PEM_cert(der), hashlib.sha256(der).digest().hex(":").upper(), detail


class SaveTooLarge(Exception):
    """A save from a connected peer passed the download limit, unpacked."""


class RelayAbandoned(Exception):
    """The browser gave up on a download the lab was relaying from a peer."""


# seconds a download from a connected peer may take, for every GB of its
# limit and at least for one: the 10 GB default allows 3000 s
PEER_DOWNLOAD_SECONDS_PER_GB = 300
# seconds an upload to a connected peer may take, for every GB of the file
# and at least for one: a 2 GB file is allowed 600 s
PEER_UPLOAD_SECONDS_PER_GB = 300
# GB one download from a connected peer may carry - a save, unpacked, or one
# entry handed to the browser - when the request names no limit; the panel
# sends the Settings Editor's choice (peerDownloadMaxGb) with every request
PEER_DOWNLOAD_DEFAULT_GB = 10
GB = 1000**3
# seconds a connected peer has to accept the connection, and to answer any
# request other than a download or an upload
PEER_TIMEOUT_SECONDS = 20


# --------------------------------------------------------------------------- #
# Base classes
# --------------------------------------------------------------------------- #


# Unlock tokens for password-protected peers, keyed (link, password): the
# peer signs its token on the password, so a changed password gives a new key
# and the stale entry is never sent again. Process-local; a restart unlocks
# once more.
_PEER_TOKENS: dict[tuple[str, str], str] = {}

# the peer answered 401 to the password the connection holds (or holds none
# and the peer now wants one): the owner changed it since the connect
PEER_PASSWORD_CHANGED = (
    "The peer no longer accepts this connection's password - reconnect the link with the current one."
)


def _unlock_token(body: bytes) -> str:
    """The token in a peer's unlock answer, '' when the body is not the
    ``{"token": "<string>"}`` object this extension sends."""
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    token = data.get("token") if isinstance(data, dict) else None
    return token if isinstance(token, str) else ""


def _token_expiry(token: str) -> int:
    """The expiry a peer's ``<expiry>.<sig>`` unlock token carries, 0 when
    it has none."""
    head = token.split(".", 1)[0]
    return int(head) if head.isdigit() else 0


class _Base(APIHandler):
    """Common helpers: store factories, JSON body parsing, error reporting."""

    @property
    def workspace_root(self) -> str:
        return os.path.expanduser(self.settings["server_root_dir"])

    @property
    def shares_dir(self) -> str:
        return _shares_dir_setting(self)

    @property
    def use_trash(self) -> bool:
        return _use_trash_setting(self)

    @property
    def excluded_names(self) -> list[str]:
        return _excluded_names_setting(self)

    @property
    def verify_peer_tls(self) -> bool:
        return _verify_peer_tls_setting(self)

    @contextlib.contextmanager
    def _spool(self):
        """A file for one download from a peer, under the store's ``tmp``
        folder so it lands on the workspace disk; removed on exit."""
        tmp = resolve_shares_dir(self.workspace_root, self.shares_dir) / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        spool = tempfile.NamedTemporaryFile(dir=tmp, prefix="download-", suffix=".part", delete=False)
        try:
            with spool:
                yield spool
        finally:
            # after the close: Windows refuses to remove a name still open
            Path(spool.name).unlink(missing_ok=True)

    async def _peer_fetch(self, url: str, spool=None, max_bytes: int = 0, started=None, certificate: str = "", **kwargs):
        """Fetch a connected peer's public endpoint server-side.

        A download names a ``spool`` - a binary file the body is written to
        as it arrives, never held in memory - and the ``max_bytes`` it may
        carry; it may take PEER_DOWNLOAD_SECONDS_PER_GB for every GB of that
        limit and at least for one, any other request PEER_TIMEOUT_SECONDS. ``started`` is a
        future the caller may pass: it resolves with the peer's headers the
        moment a 200 within the limit is announced, so the caller can relay
        the spool while the body is still arriving. The peer's certificate is
        checked as `_tls` says, against ``certificate`` when the user trusted
        one for the connection, and one that fails raises `PeerUntrusted`.
        TLS / connection errors, a timeout, a closed connection and an answer
        over the limit - which `raise_error=False` does NOT suppress - become a
        `PeerUnavailable` the handler maps to a 502.
        """
        writes = []  # what the spool raised: the workspace or the browser, not the peer
        if spool is None:
            client = tornado.httpclient.AsyncHTTPClient()
            request_timeout = PEER_TIMEOUT_SECONDS
        else:

            def store(chunk: bytes) -> None:
                try:
                    if spool.closed:
                        # the relay closed it: nothing wants the rest
                        raise RelayAbandoned()
                    spool.write(chunk)
                    # flushed, so a reader tailing the spool sees the chunk
                    # and a disk that fills inside it raises here
                    spool.flush()
                except (OSError, RelayAbandoned) as exc:
                    writes.append(exc)
                    raise

            # tornado takes max_body_size only at client construction, and
            # the shared client keeps its 100 MiB default: one client per
            # download, closed after it
            client = tornado.simple_httpclient.SimpleAsyncHTTPClient(
                force_instance=True, max_body_size=max_bytes
            )
            # the floor holds when the limit is what a save has left
            request_timeout = PEER_DOWNLOAD_SECONDS_PER_GB * max(1.0, max_bytes / GB)
            kwargs["streaming_callback"] = store
            # every answer lands in the spool, a 401's body too: a retry
            # with a fresh unlock starts it empty
            spool.seek(0)
            spool.truncate()
        lengths = []  # the Content-Length the peer announced
        block = []  # the status line and the header lines of the answer being read

        def note_headers(line: str) -> None:
            if line != "\r\n":
                block.append(line)
                name, _, value = line.partition(":")
                if name.lower() == "content-length" and value.strip().isdigit():
                    lengths.append(int(value))
                return
            # the empty line ends the block: a 200 the limit allows starts
            # the relay, once; a 401 leaves it for the retry, an over-limit
            # announcement for the 502 below
            status = block[0].split(" ", 2)[1]
            if (
                started is not None
                and not started.done()
                and status == "200"
                and (not lengths or lengths[-1] <= max_bytes)
            ):
                started.set_result(tornado.httputil.HTTPHeaders.parse("".join(block[1:])))
            block.clear()

        try:
            return await client.fetch(
                url,
                raise_error=False,
                connect_timeout=PEER_TIMEOUT_SECONDS,
                request_timeout=request_timeout,
                header_callback=note_headers,
                **_tls(self.verify_peer_tls, certificate),
                **kwargs,
            )
        except ssl.SSLCertVerificationError as exc:
            raise PeerUntrusted(url, (exc.verify_message or str(exc)).rstrip(".")) from None
        except ssl.SSLError as exc:
            raise PeerUnavailable(f"The TLS connection to the peer failed ({exc})") from None
        except (OSError, ConnectionError, ValueError):
            raise PeerUnavailable("Could not reach the peer") from None
        except HTTPTimeoutError as exc:
            # tornado says "Timeout during request" when the answer ran out of
            # time, otherwise the connection did not open in time
            waited = request_timeout if "during request" in str(exc) else PEER_TIMEOUT_SECONDS
            raise PeerUnavailable(f"The peer did not answer within {waited:g} s") from None
        except HTTPStreamClosedError:
            if writes:
                # the spool refused a chunk, or the relay was abandoned:
                # tornado logged the error, closed the connection and raised
                # its own
                raise writes[0] from None
            # tornado closes the connection itself, with the same error, when
            # the announced body passes its size limit
            if spool is not None and lengths and lengths[-1] > max_bytes:
                raise PeerUnavailable(
                    f"The peer's download is larger than the {max_bytes / GB:g} GB limit"
                ) from None
            if spool is not None and not lengths:
                # a chunked body - every folder and Save All zip - that passes
                # the limit is closed with this same error, and nothing tells
                # the two apart
                raise PeerUnavailable(
                    f"The peer's download passed the {max_bytes / GB:g} GB limit, "
                    "or the peer closed the connection before it finished"
                ) from None
            raise PeerUnavailable("The peer closed the connection before the download finished") from None
        finally:
            if spool is not None:
                client.close()

    async def _peer_auth_headers(self, conn: dict, fresh: bool = False) -> dict:
        """Unlock a password-protected peer resource before fetching from it.

        Connections to protected shares/requests persist the password; this
        trades it for a short-lived unlock token via the peer's public unlock
        endpoint and returns the ``X-Share-Token`` header to send on every
        subsequent peer fetch. Unprotected connections return no headers.

        The token is kept per process until it expires: the peer charges
        every unlock against its password limiter (a 1 s cooldown by
        default), so the panel's 15 s polls must not unlock again and again
        or a save right after a poll reads as a wrong password. ``fresh``
        drops the kept token first, for a retry after the peer refused it.
        """
        password = conn.get("password") or ""
        if not password:
            return {}
        link = (conn.get("link") or "").rstrip("/")
        if not link:
            return {}
        cache_key = (link, password)
        if fresh:
            _PEER_TOKENS.pop(cache_key, None)
        token = _PEER_TOKENS.get(cache_key)
        if token and _token_expiry(token) > time.time() + 60:
            return {"X-Share-Token": token}
        resp = await self._peer_fetch(
            link + "/unlock",
            method="POST",
            body=json.dumps({"password": password}),
            headers={"Content-Type": "application/json"},
            certificate=conn.get("certificate", ""),
        )
        if resp.code == 429:
            raise PeerUnavailable("too many password attempts - wait before retrying")
        if resp.code != 200:
            # 401 is the password; 404 the owner removed the share; the rest
            # is the peer being unavailable - the same reading as a fetch
            raise PeerUnavailable(_peer_answer(resp.code)[1])
        token = _unlock_token(resp.body)
        if not token:
            return {}
        _PEER_TOKENS[cache_key] = token
        return {"X-Share-Token": token}

    @property
    def share_store(self) -> ShareStore:
        return ShareStore(
            self.workspace_root, self.shares_dir, self.use_trash, self.excluded_names
        )

    @property
    def request_store(self) -> RequestStore:
        return RequestStore(
            self.workspace_root, self.shares_dir, self.use_trash, self.excluded_names
        )

    @property
    def connection_store(self) -> ConnectionStore:
        return ConnectionStore(self.workspace_root, self.shares_dir)

    def write_error_json(self, status: int, message: str, reason: str = "") -> None:
        self.set_status(status)
        self.finish(json.dumps({"error": message, "reason": reason} if reason else {"error": message}))

    async def _connect_certificate(self, link: str, trust: str, kept: str) -> str | None:
        """The certificate a connection to ``link`` trusts: '' when the
        system's certificate authorities vouch for its host, else the host's
        own when the user trusted it - in the panel's dialog just now
        (``trust`` is its fingerprint) or at an earlier connect (``kept``).
        None when the answer is written: what the dialog shows for a
        certificate the user has not decided on, why a certificate cannot be
        used even once trusted (an expired one), or why the host could not be
        reached."""
        if not self.verify_peer_tls:
            return ""
        try:
            found = await peer_certificate(link)
        except PeerUnavailable as exc:
            self.write_error_json(502, str(exc))
            return None
        if found is None:
            return ""
        pem, fingerprint, detail = found
        if fingerprint == trust or pem == kept:
            return pem
        host = urlparse(link).netloc
        try:
            # a certificate the lab could not use even once trusted - an
            # expired one - is named now instead of offered in the dialog
            await _handshake(urlparse(link), _tls(True, pem)["ssl_options"])
        except ssl.SSLCertVerificationError as exc:
            self.write_error_json(502, f"{host} presents a certificate that cannot be used ({(exc.verify_message or str(exc)).rstrip('.')})")
            return None
        except (OSError, asyncio.TimeoutError):
            self.write_error_json(502, "Could not reach the peer")
            return None
        self.set_status(502)
        self.write_json({
            "error": f"{host} presents a certificate this lab does not trust ({detail})",
            "certificate": {"host": host, "fingerprint": fingerprint, "reason": detail},
        })
        return None

    def write_json(self, payload: Any) -> None:
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(payload))


class _ZipSink:
    """The write-only stream a streamed zip is built on: zipfile appends to
    ``buf`` and the handler takes it after every chunk. No seek and no tell,
    so zipfile writes data descriptors instead of seeking back to fix up
    each member's header."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, data) -> int:
        self.buf += data
        return len(data)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


class _PublicBase(tornado.web.RequestHandler):
    """Unauthenticated base for standalone pages and cross-peer endpoints.

    Extends plain tornado RequestHandler (not JupyterHandler) so we don't
    inherit jupyter_server's identity / origin / CSRF prepare() machinery -
    those reject unauthenticated requests in JupyterHub setups.
    """

    def check_xsrf_cookie(self):  # noqa: D401
        return None

    async def _serve_zip(self, directory: Path, name: str, prefix: str):
        """Stream ``directory`` as ``<name>.zip``, members named under
        ``prefix`` ('' for bare relative names). Every chunk read goes
        through zipfile's compressor and straight to the response, so one
        chunk and the compressor's state are all that is held; no
        Content-Length, the archive's size is known only at its end."""
        self.set_header("Content-Type", "application/zip")
        # RFC 5987 form: a plain filename= is latin-1 only, and tornado refuses
        # a header value with a character above U+00FF
        self.set_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name + ".zip"))
        sink = _ZipSink()
        try:
            with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _dirs, files in os.walk(directory):
                    for fname in files:
                        abs_path = os.path.join(root, fname)
                        arcname = os.path.join(prefix, os.path.relpath(abs_path, directory))
                        # a date outside zip's 1980-2107 range is clamped, as
                        # every archiver does, instead of failing the folder
                        info = zipfile.ZipInfo.from_file(abs_path, arcname, strict_timestamps=False)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        with zf.open(info, "w") as dst, open(abs_path, "rb") as src:
                            while chunk := src.read(_EXTRACT_CHUNK_BYTES):
                                dst.write(chunk)
                                self.write(sink.take())
                                await self.flush()
        except (OSError, ValueError):
            # zipfile refuses a member whose name is not UTF-8 with a ValueError
            if not self._headers_written:
                # the first member failed before any byte was out: a status
                # line is the only way the browser can name the failure, and
                # the answer is the sentence every public error carries, not
                # the archive
                self.clear_header("Content-Disposition")
                self.set_header("Content-Type", "application/json")
                self.set_status(500)
                self.finish(json.dumps({"error": "Could not read the folder"}))
                return
            # a member vanished under the walk, or the browser gave up: bytes
            # are out, so the closed connection is what marks the download
            # failed - a finished answer would pass a cut archive as complete
            self.request.connection.close()
            return
        # the central directory, written when the archive closed
        self.write(sink.take())
        await self.flush()
        self.finish()

    def set_default_headers(self):
        # Cooperative call so this class never terminates the chain. Note the
        # invariant it does NOT provide: `_UncachedPublicMixin` must be listed
        # FIRST in the bases (`class X(_UncachedPublicMixin, _PublicBase)`) -
        # listed second it lands after RequestHandler in the MRO, which does
        # not call super(), so its Cache-Control would silently never be set
        # while CORS still worked. Pinned by test_mixin_precedes_public_base.
        super().set_default_headers()
        # Allow cross-origin GETs of manifests and downloads so other peers'
        # JupyterLab panels can fetch directly from this server.
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.set_header(
            "Access-Control-Allow-Headers", "Content-Type, X-Share-Token"
        )

    def options(self, *args, **kwargs):
        self.set_status(204)
        self.finish()


    @property
    def workspace_root(self) -> str:
        return os.path.expanduser(self.settings["server_root_dir"])

    @property
    def shares_dir(self) -> str:
        return _shares_dir_setting(self)

    @property
    def use_trash(self) -> bool:
        return _use_trash_setting(self)

    @property
    def excluded_names(self) -> list[str]:
        return _excluded_names_setting(self)

    @property
    def share_store(self) -> ShareStore:
        return ShareStore(
            self.workspace_root, self.shares_dir, self.use_trash, self.excluded_names
        )

    @property
    def request_store(self) -> RequestStore:
        return RequestStore(
            self.workspace_root, self.shares_dir, self.use_trash, self.excluded_names
        )


# Cached content of the CLI config file, keyed by mtime so `cloudflare setup`
# / `reset` / the api/tunnel toggle take effect on the next request without a
# server restart and without re-reading the file on every link built.
_CONFIG_FILE_CACHE: dict[str, Any] = {"path": None, "mtime": None, "value": {}}


def _config_file_state() -> dict:
    """Read the CLI config file (mtime-cached)."""
    from .tunnel import config_path

    path = config_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _CONFIG_FILE_CACHE["path"] == path and _CONFIG_FILE_CACHE["mtime"] == mtime:
        return _CONFIG_FILE_CACHE["value"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    _CONFIG_FILE_CACHE.update(path=path, mtime=mtime, value=value)
    return value


def _parse_origin(raw: str) -> str:
    """Reduce a base URL to scheme://host, or ''."""
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    if not parsed.netloc:
        return ""
    return (parsed.scheme or "https") + "://" + parsed.netloc


def _raw_public_origin(handler: tornado.web.RequestHandler) -> str:
    """Configured external origin regardless of the tunnel toggle.

    Used where "is this OUR address?" must hold even while the tunnel is
    switched off (self-connect detection) and for the configured/active
    distinction in api/info.
    """
    cfg: ShareFilesConfig = handler.settings.get("share_files_config")
    raw = (cfg.public_base_url if cfg is not None else "") or ""
    if not raw:
        raw = _config_file_state().get("public_base_url") or ""
    return _parse_origin(raw)


def _tunnel_active() -> bool:
    """The api/tunnel toggle: public links when on, private when off."""
    return bool(_config_file_state().get("tunnel_active", True))


def _configured_public_origin(handler: tornado.web.RequestHandler) -> str:
    """External origin (scheme://host) generated links should carry, or ''.

    Precedence: the ``public_base_url`` trait (always wins - explicit admin
    override), else the value the CLI wrote to
    ``~/.config/jupyterlab-share-files/config.json`` - the latter only while
    the tunnel toggle (``tunnel_active``) is on; switched off, links revert
    to the private/request address. Only scheme + host are kept - the base
    path stays auto-detected from the server's own ``base_url``.
    """
    cfg: ShareFilesConfig = handler.settings.get("share_files_config")
    raw = (cfg.public_base_url if cfg is not None else "") or ""
    if raw:
        return _parse_origin(raw)
    if not _tunnel_active():
        return ""
    return _parse_origin(_config_file_state().get("public_base_url") or "")


def _public_origin(handler: tornado.web.RequestHandler) -> str:
    """Pick scheme + host the browser actually sees.

    A configured external origin (Cloudflare tunnel hostname set up via
    ``cloudflare --setup``, or the ``public_base_url`` trait) wins. Otherwise,
    behind a TLS-terminating proxy (JupyterHub, Traefik, nginx) Tornado's
    ``request.protocol`` reports ``http`` unless ``trust_xheaders`` is enabled
    on the server. Honour ``X-Forwarded-Proto`` / ``X-Forwarded-Host``
    explicitly so HTTPS-facing sessions emit HTTPS links, while plain p2p
    sessions stay on HTTP.
    """
    configured = _configured_public_origin(handler)
    if configured:
        return configured
    return _request_origin(handler)


def _request_origin(handler: tornado.web.RequestHandler) -> str:
    """The scheme + host the browser reached this server on, from the
    forwarded headers a TLS-terminating proxy sets, else the request itself."""
    proto = handler.request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    if not proto:
        proto = handler.request.protocol
    host = handler.request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    if not host:
        host = handler.request.host
    return proto + "://" + host


def _own_link_prefixes(handler: tornado.web.RequestHandler) -> set[str]:
    """All `scheme://host/base_url/` prefixes that mean "this server".

    Used by self-connect detection. The configured external origin (Cloudflare
    tunnel) is also "us" - without it, pasting one's own Cloudflare link would
    create a loop connection.
    """
    own_base_url = handler.settings.get("base_url", "/")
    if not own_base_url.endswith("/"):
        own_base_url += "/"
    prefixes = {
        handler.request.protocol + "://" + handler.request.host + own_base_url
    }
    # Raw (toggle-independent): one's own Cloudflare link stays "us" even
    # while the tunnel is switched off.
    configured = _raw_public_origin(handler)
    if configured:
        prefixes.add(configured + own_base_url)
    return prefixes


def _public_share_url(handler: tornado.web.RequestHandler, id_: str) -> str:
    """Build an absolute URL the share's public page is reachable at."""
    path = url_path_join(handler.settings.get("base_url", "/"), EXTENSION_NAMESPACE, "public", "share", id_)
    return _public_origin(handler) + path


def _public_request_url(handler: tornado.web.RequestHandler, id_: str) -> str:
    path = url_path_join(handler.settings.get("base_url", "/"), EXTENSION_NAMESPACE, "public", "request", id_)
    return _public_origin(handler) + path


def _strip_owner_fields(manifest: dict) -> dict:
    """Remove owner-only filesystem metadata before a manifest crosses the
    public boundary. The stores stamp `path` (the owner's workspace-relative
    path) and `mtime` on entries for the authenticated owner panel; a public
    recipient must never see the owner's on-disk layout or file timestamps.
    Mutates and returns the freshly built, per-request manifest in place."""
    manifest.pop("path", None)
    for entry in manifest.get("entries", []):
        entry.pop("path", None)
        entry.pop("mtime", None)
    for uploader in manifest.get("uploaders", []):
        for entry in uploader.get("entries", []):
            entry.pop("path", None)
            entry.pop("mtime", None)
    return manifest


# --------------------------------------------------------------------------- #
# Password protection: rate-limited unlock + capability token
# --------------------------------------------------------------------------- #

# In-memory rate limiter (the `limits` library) guarding the public unlock
# endpoint against brute force. Per-resource keying (share/request id) - one
# protected resource being hammered cannot lock out a different one. Storage is
# process-local; on a multi-process server each worker keeps its own counters,
# which only makes the limit more lenient, never less safe.
from limits import RateLimitItemPerMinute, RateLimitItemPerSecond  # noqa: E402
from limits import storage as _limits_storage  # noqa: E402
from limits import strategies as _limits_strategies  # noqa: E402

_RATE_STORAGE = _limits_storage.MemoryStorage()
_RATE_LIMITER = _limits_strategies.MovingWindowRateLimiter(_RATE_STORAGE)
# Token-of-the-day style secret: random per process. Unlock tokens are also
# bound to the password value, so a restart simply asks recipients to re-enter
# the password (tokens expire); no persistence needed.
_TOKEN_TTL_SECONDS = 6 * 3600


def _rate_limit_ok(handler, kind: str, id_: str) -> bool:
    """Charge one password attempt against the per-resource limits.

    Returns False (and the caller should 429) when either the per-attempt
    cooldown or the per-minute cap is exceeded. Defaults are generous; both
    are tunable via ShareFilesConfig.
    """
    cfg: ShareFilesConfig = handler.settings.get("share_files_config")
    per_min = getattr(cfg, "password_max_attempts_per_minute", 30) or 30
    cooldown = getattr(cfg, "password_attempt_cooldown_seconds", 1) or 0
    key = f"pw:{kind}:{id_}"
    burst = RateLimitItemPerMinute(per_min)
    if not _RATE_LIMITER.test(burst, key):
        return False
    if cooldown > 0:
        gap = RateLimitItemPerSecond(1, cooldown)
        if not _RATE_LIMITER.test(gap, key):
            return False
        _RATE_LIMITER.hit(gap, key)
    _RATE_LIMITER.hit(burst, key)
    return True


def _make_unlock_token(id_: str, password: str) -> str:
    """Signed capability token for a successfully-unlocked resource.

    HMAC-keyed on the password itself, so changing the password invalidates
    every outstanding token automatically. Format: ``<expiry>.<hex-sig>``.
    """
    exp = int(time.time()) + _TOKEN_TTL_SECONDS
    msg = f"{id_}.{exp}".encode("utf-8")
    sig = hmac.new(password.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _check_unlock_token(id_: str, password: str, token: str) -> bool:
    """Validate an unlock token against the current password (constant-time)."""
    if not token or not password:
        return False
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if exp < int(time.time()):
        return False
    msg = f"{id_}.{exp}".encode("utf-8")
    expected = hmac.new(password.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _password_gate(handler, store, id_: str) -> bool:
    """Allow a public request through, or write 401 and return False.

    No password set on the resource -> always allowed. Otherwise a valid
    unlock token (header ``X-Share-Token`` or ``?t=``) is required.
    """
    password = store.get_password(id_)
    if not password:
        return True
    token = handler.request.headers.get("X-Share-Token", "")
    if not token:
        token = handler.get_query_argument("t", default="")
    if _check_unlock_token(id_, password, token):
        return True
    handler.set_status(401)
    handler.set_header("Content-Type", "application/json")
    handler.finish(json.dumps({"error": "password required", "password_required": True}))
    return False


# --------------------------------------------------------------------------- #
# Authenticated API handlers
# --------------------------------------------------------------------------- #


class InfoHandler(_Base):
    """Return basic info about the extension so the UI can display where
    shares and requests live on disk."""

    @tornado.web.authenticated
    def get(self):
        workspace = Path(self.workspace_root).resolve()
        storage = resolve_shares_dir(self.workspace_root, self.shares_dir)
        # Express storage path relative to the workspace root when possible,
        # so the frontend can show something like "./uploads"
        try:
            rel = storage.relative_to(workspace)
            display_path = "./" + str(rel).replace(os.sep, "/")
        except ValueError:
            display_path = str(storage)
        self.write_json({
            "storage_path": display_path,
            "shares_subdir": "shares",
            "requests_subdir": "requests",
            # '' while the tunnel toggle is off - links are private then
            "public_base_url": _configured_public_origin(self),
            **_tunnel_state(self),
        })


def _tunnel_state(handler: tornado.web.RequestHandler) -> dict:
    """Tunnel state for the frontend: configured / active / running."""
    from .tunnel import tunnel_state as _lib_tunnel_state

    state = _lib_tunnel_state()
    # handler-aware: the public_base_url trait also counts as configured
    state["tunnel_configured"] = bool(_raw_public_origin(handler))
    return state


class TunnelHandler(_Base):
    """Cloudflare tunnel toggle (api/tunnel): GET state, POST switch.

    POST ``{"active": true}`` switches to public links (daemon started,
    ``public_base_url`` applied to generated links); ``{"active": false}``
    stops the daemon and reverts links to the private/request address.
    POST ``{"autostart": bool}`` persists whether the server brings the
    tunnel up at startup.
    """

    @tornado.web.authenticated
    def get(self):
        self.write_json(_tunnel_state(self))

    @tornado.web.authenticated
    async def post(self):
        from .tunnel import (
            _load_config,
            set_tunnel_autostart,
            tunnel_start,
            tunnel_stop,
        )

        body = self.get_json_body() or {}
        if not _load_config().get("cloudflare_tunnel_token"):
            return self.write_error_json(
                400, "Cloudflare sharing is not configured (run cloudflare setup)"
            )
        # off the event loop, as the setup handler runs: starting the
        # connector sleeps 2 s per attempt and stopping it polls for 5 s
        loop = tornado.ioloop.IOLoop.current()
        try:
            if "autostart" in body:
                set_tunnel_autostart(bool(body["autostart"]))
            if body.get("active") is True:
                share_config: ShareFilesConfig = self.settings.get("share_files_config")
                retries = share_config.cloudflared_retries if share_config else 3
                result = await loop.run_in_executor(None, tunnel_start, retries)
                if not result["daemon_running"]:
                    # links marked public with no connector serving them would
                    # show a green cloud over a dead hostname: back to private
                    # links, and the sentence names the log
                    await loop.run_in_executor(None, tunnel_stop)
                    return self.write_error_json(
                        400, f"cloudflared did not start - see {result['connector_log']}"
                    )
            elif body.get("active") is False:
                await loop.run_in_executor(None, tunnel_stop)
        except RuntimeError as exc:
            return self.write_error_json(400, str(exc))
        except OSError as exc:
            # the configuration file could not be written (a read-only disk)
            return self.write_error_json(
                500, f"Could not write the Cloudflare configuration: {exc.strerror}"
            )
        self.write_json(_tunnel_state(self))


# seconds the link check waits for a link to answer
LINK_CHECK_TIMEOUT_SECONDS = 10


async def probe_link(link: str) -> dict:
    """GET ``link`` from this server and report what answered.

    ``status`` is the HTTP status the link answered - a redirect is reported
    as the status it is, never followed; ``error`` says in plain words why
    nothing answered. The link is one the server composed itself, so the
    question is "does it answer", not "is the certificate trusted": a
    self-signed certificate does not fail the probe.
    """
    client = tornado.httpclient.AsyncHTTPClient()
    try:
        resp = await client.fetch(
            link, raise_error=False, validate_cert=False, follow_redirects=False,
            request_timeout=LINK_CHECK_TIMEOUT_SECONDS,
        )
    except HTTPTimeoutError:
        error = f"no answer within {LINK_CHECK_TIMEOUT_SECONDS:g} s"
    except ConnectionRefusedError:
        error = "connection refused"
    except socket.gaierror:
        error = "no address for the host name"
    except ssl.SSLError:
        error = "a TLS error"
    except (ConnectionError, HTTPStreamClosedError):
        error = "a closed connection"
    except (OSError, tornado.httpclient.HTTPClientError):
        error = "a network error"
    else:
        return {"link": link, "reachable": resp.code == 200, "status": resp.code}
    return {"link": link, "reachable": False, "error": error}


class LinkCheckHandler(_Base):
    """Probe a generated public link server-side (api/link-check).

    The link dialog asks "is this link actually reachable?" - a frontend
    fetch would be blocked by CORS, so the server fetches its own public
    link (through the Cloudflare edge when configured) and reports the
    outcome. Only kind+id are accepted; the URL is rebuilt server-side,
    never taken from the client.
    """

    @tornado.web.authenticated
    async def get(self):
        kind = self.get_query_argument("kind", "")
        id_ = self.get_query_argument("id", "")
        if not re.fullmatch(r"[A-Z2-7]{6,16}", id_):
            return self.write_error_json(400, "invalid id")
        if kind == "share":
            link = _public_share_url(self, id_)
        elif kind == "request":
            link = _public_request_url(self, id_)
        else:
            return self.write_error_json(400, "kind must be 'share' or 'request'")
        self.write_json(await probe_link(link))


class TunnelSetupHandler(_Base):
    """Provision Cloudflare sharing from the panel (api/tunnel/setup).

    Same inputs and sequence as ``cloudflare setup``: token, account id,
    hostname (the public share host) and the mandatory https
    private_base_url. The blocking Cloudflare API calls run in an executor
    so the event loop stays responsive.
    """

    @tornado.web.authenticated
    async def post(self):
        from .tunnel import setup_and_start

        body = self.get_json_body() or {}
        token = str(body.get("token") or "")
        account_id = str(body.get("account_id") or "")
        hostname = str(body.get("hostname") or "")
        private_base_url = str(body.get("private_base_url") or "")
        if not hostname or not private_base_url:
            return self.write_error_json(
                400, "hostname and private_base_url are required"
            )
        try:
            result = await tornado.ioloop.IOLoop.current().run_in_executor(
                None,
                setup_and_start,
                token,
                account_id,
                hostname,
                private_base_url,
            )
        except RuntimeError as exc:
            return self.write_error_json(400, str(exc))
        self.write_json({**result, **_tunnel_state(self)})


class TunnelResetHandler(_Base):
    """Reset Cloudflare sharing from the panel (api/tunnel/reset).

    Same as ``cloudflare reset``: credentials, tunnel state and the
    private/public base URLs are cleared; links revert to the private
    address on the next request; Cloudflare-side resources are kept.
    """

    @tornado.web.authenticated
    def post(self):
        from .tunnel import reset_config

        result = reset_config()
        self.write_json({**result, **_tunnel_state(self)})


class PasswordHandler(_Base):
    """Owner-side password management (api/<shares|requests>/<id>/password).

    GET returns the stored plaintext (owner-only - the link dialog shows it
    with a copy button); POST sets/changes it, an empty value clears it.
    """

    def _store(self, kind: str):
        return self.share_store if kind == "shares" else self.request_store

    @tornado.web.authenticated
    def get(self, kind, id_):
        store = self._store(kind)
        if not store.exists(id_):
            return self.write_error_json(404, "not found")
        self.write_json({"id": id_, "password": store.get_password(id_)})

    @tornado.web.authenticated
    def post(self, kind, id_):
        body = self.get_json_body() or {}
        password = str(body.get("password") or "")
        store = self._store(kind)
        try:
            manifest = store.set_password(id_, password)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        manifest["password"] = password
        self.write_json(manifest)


class GeneratePasswordHandler(_Base):
    """xkcd-style passphrase generation (api/generate-password)."""

    @tornado.web.authenticated
    def get(self):
        try:
            self.write_json({"password": generate_password()})
        except RuntimeError as exc:
            self.write_error_json(500, str(exc))


class PublicUnlockHandler(_PublicBase):
    """Password attempt for a protected resource (public/<kind>/<id>/unlock).

    Rate limited per resource id - a generous per-minute cap plus a
    per-attempt cooldown (both tunable via ShareFilesConfig). A correct
    password returns a signed unlock token the standalone page and peers
    send back via the ``X-Share-Token`` header (or ``?t=`` for plain
    download links). Wrong password and rate-limit both burn one attempt.
    """

    def post(self, kind, id_):
        store = self.share_store if kind == "share" else self.request_store
        password = store.get_password(id_)
        if not store.exists(id_):
            self.set_status(404)
            self.finish(json.dumps({"error": "not found"}))
            return
        if not password:
            self.finish(json.dumps({"unlocked": True, "token": ""}))
            return
        if not _rate_limit_ok(self, kind, id_):
            self.set_status(429)
            self.finish(json.dumps({
                "error": "too many attempts - wait before retrying",
            }))
            return
        try:
            body = json.loads(self.request.body or b"{}")
        except ValueError:
            body = {}
        attempt = str(body.get("password") or "")
        if verify_password(password, attempt):
            self.finish(json.dumps({
                "unlocked": True,
                "token": _make_unlock_token(id_, password),
            }))
        else:
            self.set_status(401)
            self.finish(json.dumps({"error": "wrong password"}))


class SharesListHandler(_Base):
    @tornado.web.authenticated
    def get(self):
        items = self.share_store.list()
        for item in items:
            item["link"] = _public_share_url(self, item["id"])
        self.write_json({"shares": items})

    @tornado.web.authenticated
    def post(self):
        body = self.get_json_body() or {}
        name = (body.get("name") or "").strip()
        paths = body.get("paths") or []
        password = str(body.get("password") or "")
        if not name:
            return self.write_error_json(400, "Missing 'name'")
        if not isinstance(paths, list):
            return self.write_error_json(400, "'paths' must be a list")
        try:
            manifest = self.share_store.create(name, paths, password=password)
        except (StorageError, NotFoundError) as exc:
            return self.write_error_json(400, str(exc))
        manifest["link"] = _public_share_url(self, manifest["id"])
        self.write_json(manifest)


class ShareItemHandler(_Base):
    @tornado.web.authenticated
    def get(self, id_):
        try:
            manifest = self.share_store.get(id_)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        manifest["link"] = _public_share_url(self, id_)
        self.write_json(manifest)

    @tornado.web.authenticated
    def delete(self, id_):
        try:
            self.share_store.delete(id_)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        self.write_json({"ok": True})


class ShareItemsHandler(_Base):
    @tornado.web.authenticated
    def post(self, id_):
        body = self.get_json_body() or {}
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths:
            return self.write_error_json(400, "'paths' must be a non-empty list")
        try:
            manifest = self.share_store.add_items(id_, paths)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        except StorageError as exc:
            return self.write_error_json(400, str(exc))
        manifest["link"] = _public_share_url(self, id_)
        self.write_json(manifest)

    @tornado.web.authenticated
    def delete(self, id_):
        """Remove items from a share.

        Names come via query parameters (`?name=foo&name=bar`) rather than
        request body - browsers' fetch() does send DELETE bodies, but the
        downstream Jupyter ServerConnection layer can drop them, and many
        proxies strip them too. Query params are universally reliable.
        """
        names = [n for n in self.get_arguments("name") if n]
        if not names:
            return self.write_error_json(400, "Pass at least one ?name=...")
        try:
            manifest = self.share_store.remove_items(id_, names)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        except StorageError as exc:
            return self.write_error_json(400, str(exc))
        manifest["link"] = _public_share_url(self, id_)
        self.write_json(manifest)


class RequestsListHandler(_Base):
    @tornado.web.authenticated
    def get(self):
        items = []
        for m in self.request_store.list():
            full = self.request_store.get(m["id"])
            full["link"] = _public_request_url(self, m["id"])
            items.append(full)
        self.write_json({"requests": items})

    @tornado.web.authenticated
    def post(self):
        body = self.get_json_body() or {}
        name = (body.get("name") or "").strip()
        password = str(body.get("password") or "")
        if not name:
            return self.write_error_json(400, "Missing 'name'")
        manifest = self.request_store.create(name, password=password)
        manifest["link"] = _public_request_url(self, manifest["id"])
        manifest["uploaders"] = []
        self.write_json(manifest)


class RequestItemHandler(_Base):
    @tornado.web.authenticated
    def get(self, id_):
        try:
            manifest = self.request_store.get(id_)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        manifest["link"] = _public_request_url(self, id_)
        self.write_json(manifest)

    @tornado.web.authenticated
    def delete(self, id_):
        try:
            self.request_store.delete(id_)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        self.write_json({"ok": True})


class RecordSaveHandler(_Base):
    """api/<shares|requests>/<id>/save - write a whole record into the
    workspace, as a folder of its files or as one zip beside them.

    The bytes are already on this disk in standalone mode, so this is a copy
    out of the store rather than a transfer; the hub-mode table answers the
    same route by asking the hub, which owns the bytes there.
    """

    @tornado.web.authenticated
    def post(self, kind, id_):
        body = self.get_json_body() or {}
        archive = str(body.get("archive") or "")
        if archive not in ("", "zip"):
            return self.write_error_json(400, "'archive' must be 'zip' or absent")
        try:
            target = _resolve_workspace_target_dir(
                self.workspace_root, str(body.get("target_dir") or ""), self.shares_dir
            )
        except StorageError as exc:
            return self.write_error_json(400, str(exc))
        if not target.is_dir():
            return self.write_error_json(404, f"Not a folder: {body.get('target_dir') or '.'}")
        store = self.share_store if kind == "shares" else self.request_store
        try:
            path = store.save_out(id_, target, archive)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        except (OSError, StorageError) as exc:
            return self.write_error_json(500, f"Could not save the record: {exc}")
        self.write_json({"ok": True, "path": path})


class RequestUploadsHandler(_Base):
    @tornado.web.authenticated
    def delete(self, id_):
        """Remove an upload. Pass uploader and name as query params, not body."""
        uploader = self.get_argument("uploader", default="")
        name = self.get_argument("name", default="")
        if not uploader or not name:
            return self.write_error_json(400, "Missing ?uploader=... or ?name=...")
        try:
            manifest = self.request_store.remove_upload(id_, uploader, name)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        except StorageError as exc:
            return self.write_error_json(400, str(exc))
        manifest["link"] = _public_request_url(self, id_)
        self.write_json(manifest)


class RequestSeenHandler(_Base):
    @tornado.web.authenticated
    def post(self, id_):
        try:
            self.request_store.mark_seen(id_)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        self.write_json({"ok": True})


class ConnectionsHandler(_Base):
    @tornado.web.authenticated
    def get(self):
        self.write_json({"connections": self.connection_store.list()})

    @tornado.web.authenticated
    async def post(self):
        """Add a connection.

        Body: { "link": "https://host/.../public/share/<id>",
                "password": "..." (optional),
                "trust": "<SHA-256 fingerprint>" (optional) }
        We parse the link, probe whether the resource is password protected,
        verify a provided password against the peer's unlock endpoint, and
        persist a connection entry. A protected resource without (or with a
        wrong) password answers 401 `password_required` so the panel can
        prompt and retry. A certificate the system does not trust answers
        502 with its ``certificate`` details until the user trusts it in the
        panel's dialog; the connection then keeps it.
        """
        body = self.get_json_body() or {}
        link = (body.get("link") or "").strip()
        password = str(body.get("password") or "")
        if not link:
            return self.write_error_json(400, "Missing 'link'")
        try:
            parsed = _parse_share_link(link)
        except ValueError as exc:
            return self.write_error_json(400, str(exc))
        # Refuse to connect to ourselves - that would create a loop where
        # any save/upload routes back through the same server. On
        # JupyterHub two users share a host but live at different
        # `/user/<name>/` prefixes, so compare the full prefix (host +
        # base_url) not just the host.
        link_prefix = parsed["host"] + parsed.get("base_path", "/")
        if link_prefix in _own_link_prefixes(self):
            return self.write_error_json(
                400,
                "That link points to your own server - it's already in your panel.",
            )
        stored = next((c for c in self.connection_store.list() if c.get("link") == link), {})
        # Connect Again sends no password: the connection's own is tried first
        password = password or stored.get("password", "")
        certificate = await self._connect_certificate(link, str(body.get("trust") or ""), stored.get("certificate", ""))
        if certificate is None:
            return
        # Probe the peer: protected resources answer 401 on the bare manifest,
        # a removed one 404 and is refused with the sentence the polls give.
        # With a password given, verify it via the peer's unlock endpoint so a
        # wrong password is caught at connect time, not at first download.
        try:
            probe = await self._peer_fetch(link.rstrip("/") + "/manifest", certificate=certificate)
            if probe.code == 404:
                return self.write_error_json(*_peer_answer(probe.code))
            if probe.code == 401:
                if not password:
                    self.set_status(401)
                    return self.write_json({
                        "error": "password required",
                        "password_required": True,
                    })
                unlock = await self._peer_fetch(
                    link.rstrip("/") + "/unlock",
                    method="POST",
                    body=json.dumps({"password": password}),
                    headers={"Content-Type": "application/json"},
                    certificate=certificate,
                )
                if unlock.code == 429:
                    return self.write_error_json(
                        429, "too many password attempts - wait before retrying"
                    )
                if unlock.code != 200:
                    self.set_status(401)
                    return self.write_json({
                        "error": "wrong password",
                        "password_required": True,
                    })
                # keep the token this unlock earned: the panel polls the new
                # connection at once, inside the peer's cooldown
                token = _unlock_token(unlock.body)
                if token:
                    _PEER_TOKENS[(link.rstrip("/"), password)] = token
        except PeerUntrusted as exc:
            # the trusted certificate itself is refused (it expired, for example):
            # neither the dialog nor Connect Again can fix that, so name the certificate
            return self.write_error_json(502, f"{exc.host} presents a certificate that cannot be used ({exc.detail})")
        except PeerUnavailable as exc:
            return self.write_error_json(502, str(exc))
        entry = self.connection_store.add(
            kind=parsed["kind"],
            id_=parsed["id"],
            host=parsed["host"],
            name=parsed.get("name", ""),
            owner=parsed.get("owner", ""),
            link=link,
            password=password,
            certificate=certificate,
        )
        self.write_json(entry)


class ConnectionItemHandler(_Base):
    @tornado.web.authenticated
    def delete(self, key):
        self.connection_store.remove(key)
        self.write_json({"ok": True})


def _resolve_workspace_target_dir(workspace_root: str, target_dir: str, shares_dir_setting: str = "") -> Path:
    """Resolve target_dir relative to workspace, reject paths inside the shares dir.

    Accepts symlinked subdirectories - we only validate the user-supplied path
    syntactically (rejecting `..` and absolute paths) and check that the final
    location is neither the shares directory itself nor inside its shares/ or
    requests/ tree, where a saved `<name>-<id>` folder would sit among the
    stored content.
    """
    root = Path(workspace_root)
    if target_dir in ("", "."):
        resolved = root
    else:
        if not _is_safe_relative(target_dir):
            raise StorageError(f"Unsafe target_dir: {target_dir}")
        resolved = root / target_dir
    forbidden = resolve_shares_dir(workspace_root, shares_dir_setting)
    # compared after resolve(), so a symlink into the store is blocked too;
    # symlinked subtrees elsewhere are fine
    try:
        forbidden = forbidden.resolve()
        final = resolved.resolve()
        if final == forbidden or any(
            final.is_relative_to(forbidden / sub) for sub in (ShareStore.subdir, RequestStore.subdir)
        ):
            raise StorageError("Cannot save into the shares directory")
    except (OSError, ValueError):
        pass
    return resolved


# bytes copied from a zip member to disk at a time
_EXTRACT_CHUNK_BYTES = 1024 * 1024

# what an unreadable member of a peer's zip raises across the four standard
# codecs, member-name decoding, encryption, unknown methods and corrupt
# offsets and seeks in the central directory
_ZIP_UNREADABLE = (zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, UnicodeDecodeError, ValueError) + (
    (lzma.LZMAError,) if lzma else ()
)


def _download_limit(max_gb) -> int:
    """Bytes one download from a peer may carry: the request's ``max_gb``
    (the panel's setting), PEER_DOWNLOAD_DEFAULT_GB when the request names
    none - the CLI's pick-up, an older panel."""
    try:
        gb = float(max_gb)
    except (TypeError, ValueError):
        gb = 0.0
    return int((gb if gb > 0 else PEER_DOWNLOAD_DEFAULT_GB) * GB)


def _extract_zip_into(zip_file, dest_dir: Path, max_bytes: int) -> int:
    """Extract a zip archive (a path or a seekable binary file) into
    dest_dir, return the bytes written.

    Strips path components that escape the destination. Each member is
    copied to disk in chunks; passing ``max_bytes`` raises `SaveTooLarge`
    and leaves what was written for the caller to remove.
    """
    written = 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_file) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p and p not in (".", "..")]
            if not parts:
                continue
            target = dest_dir.joinpath(*parts).resolve()
            if not target.is_relative_to(dest_dir.resolve()):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                src = zf.open(info)
            except OSError as exc:
                # an end record pointing the central directory before the
                # archive's start: the spool's seek refuses (a BytesIO
                # raised ValueError) - the archive, not the workspace
                raise zipfile.BadZipFile(str(exc)) from None
            with src, open(target, "wb") as out:
                while True:
                    try:
                        chunk = src.read(_EXTRACT_CHUNK_BYTES)
                    except OSError as exc:
                        # a codec fault (corrupt bzip2), not the workspace
                        raise zipfile.BadZipFile(str(exc)) from None
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise SaveTooLarge()
                    out.write(chunk)
    return written


class ConnectionSaveHandler(_Base):
    """Download items from a connected share into the user's workspace."""

    def _failed(self, saved: list[str], code: int, message: str) -> None:
        """Remove what the save wrote, then answer with the error.

        DEF-PEER-53: a save that answers an error leaves nothing behind,
        whichever step failed.
        """
        for rel in saved:
            path = Path(self.workspace_root) / rel
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        return self.write_error_json(code, message)

    @tornado.web.authenticated
    async def post(self, key):
        try:
            conn = self.connection_store.get(key)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        if conn.get("kind") != "share":
            return self.write_error_json(400, "Connection is not a share")
        body = self.get_json_body() or {}
        target_dir = body.get("target_dir") or ""
        names = body.get("names")  # None or list[str] - None means "all"
        # "zip" keeps the whole share as the one archive the peer sends
        archive = str(body.get("archive") or "")
        if archive not in ("", "zip") or (archive and names is not None):
            return self.write_error_json(400, "'archive' must be 'zip' and only for the whole share")
        try:
            dest_root = _resolve_workspace_target_dir(self.workspace_root, target_dir, self.shares_dir)
        except StorageError as exc:
            return self.write_error_json(400, str(exc))

        # the persisted link carries the owner's `/user/<name>/` prefix on
        # JupyterHub, which this server cannot rebuild
        api_base = (conn.get("link") or "").rstrip("/")
        if not api_base:
            return self.write_error_json(400, "Connection has no link - disconnect it and connect again")

        saved: list[str] = []
        limit = _download_limit(body.get("max_gb"))
        # bytes this save may still write
        left = limit
        # Peer fetches can fail on TLS (self-signed) or connection - map those
        # to a clean 502 rather than an unhandled 500.
        try:
            # Protected peer? trade the stored password for an unlock token
            auth_headers = await self._peer_auth_headers(conn)
            # Resolve share name for the wrapping folder when saving all
            certificate = conn.get("certificate", "")
            manifest_resp = await self._peer_fetch(
                api_base + "/manifest", headers=auth_headers, certificate=certificate
            )
            if manifest_resp.code == 401 and auth_headers:
                # a kept token the peer no longer accepts: unlock once more
                auth_headers = await self._peer_auth_headers(conn, fresh=True)
                manifest_resp = await self._peer_fetch(
                    api_base + "/manifest", headers=auth_headers, certificate=certificate
                )
            if manifest_resp.code != 200:
                return self.write_error_json(*_peer_answer(manifest_resp.code))
            try:
                manifest = json.loads(manifest_resp.body)
            except ValueError:
                manifest = None
            if not isinstance(manifest, dict) or (
                "entries" in manifest
                and not (
                    isinstance(manifest["entries"], list)
                    and all(isinstance(e, dict) and isinstance(e.get("name"), str) for e in manifest["entries"])
                )
            ):
                return self._failed(saved, 502, "The peer did not send a readable manifest")
            # the slug comes from the peer - reduce it to one path component
            share_slug = _safe_name(str(manifest.get("slug") or conn["id"]))

            if names is None:
                # Save All - download zip, extract into <dest_root>/<share-slug>/
                with self._spool() as spool:
                    zip_resp = await self._peer_fetch(
                        api_base + "/download-all", spool=spool, max_bytes=left, headers=auth_headers,
                        certificate=certificate,
                    )
                    if zip_resp.code != 200:
                        return self._failed(saved, 502, f"Could not download share ({zip_resp.code})")
                    if archive:
                        # closed first: the check reads the file from disk,
                        # and Windows refuses to move a name still open
                        spool.close()
                        if not zipfile.is_zipfile(spool.name):
                            return self._failed(saved, 502, "The peer did not send a readable zip archive")
                        target = _resolve_unique_target(dest_root, f"{share_slug}.zip")
                        saved.append(str(target.relative_to(self.workspace_root)))
                        shutil.move(spool.name, target)
                        settle_mode(target)
                        return self.write_json({"ok": True, "saved": saved})
                    wrap_dir = _resolve_unique_target(dest_root, share_slug)
                    # `.` and `..` pass _safe_name - check where the folder
                    # really lands after resolve(), not how its path reads
                    if wrap_dir.resolve().parent != dest_root.resolve():
                        return self._failed(saved, 502, f"The peer sent an unsafe folder name: {share_slug}")
                    saved.append(str(wrap_dir.relative_to(self.workspace_root)))
                    _extract_zip_into(spool, wrap_dir, left)
            else:
                if not isinstance(names, list) or not names:
                    return self._failed(saved, 400, "'names' must be a non-empty list")
                # determine which entries are directories vs files
                entry_map = {e["name"]: e for e in manifest.get("entries", [])}
                for name in names:
                    if not name or "/" in name or "\\" in name or "\0" in name or name in (".", ".."):
                        return self._failed(saved, 400, f"Invalid name: {name}")
                    entry = entry_map.get(name)
                    if entry is None:
                        return self._failed(saved, 404, f"Not in share: {name}")
                    url = api_base + "/download/" + tornado.escape.url_escape(name)
                    with self._spool() as spool:
                        resp = await self._peer_fetch(
                            url, spool=spool, max_bytes=left, headers=auth_headers, certificate=certificate
                        )
                        if resp.code != 200:
                            return self._failed(saved, 502, f"Could not download {name} ({resp.code})")
                        if entry.get("type") == "directory":
                            wrap_dir = _resolve_unique_target(dest_root, name)
                            saved.append(str(wrap_dir.relative_to(self.workspace_root)))
                            left -= _extract_zip_into(spool, wrap_dir, left)
                        else:
                            # the fetch capped the file at `left` bytes
                            left -= spool.tell()
                            target = _resolve_unique_target(dest_root, name)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            saved.append(str(target.relative_to(self.workspace_root)))
                            # closed first: Windows refuses to move a name still open
                            spool.close()
                            shutil.move(spool.name, target)
                            settle_mode(target)
        except PeerUnavailable as exc:
            return self._failed(saved, 502, str(exc))
        except SaveTooLarge:
            return self._failed(
                saved, 502, f"The share is larger than {limit / GB:g} GB when unpacked"
            )
        except _ZIP_UNREADABLE:
            # DEF-PEER-52: the peer answered 200 with something that is not a
            # readable zip - an unreadable archive, an encrypted member, an
            # unknown compression or a corrupt stream - and
            # _extract_zip_into has already created the folder
            return self._failed(saved, 502, "The peer did not send a readable zip archive")
        except OSError:
            # the workspace refused the write (disk full, read-only) mid-save
            return self._failed(saved, 502, "Could not write the save to the workspace")

        self.write_json({"ok": True, "saved": saved})


def _peer_answer(code: int) -> tuple[int, str]:
    """The status and sentence the lab relays for a peer answer other than
    200: the two the panel can act on keep their status, the rest is the
    peer being unavailable."""
    if code == 401:
        return 401, PEER_PASSWORD_CHANGED
    if code == 404:
        return 404, "The owner has removed this share or request."
    return 502, f"Remote unavailable ({code})"


class _ConnectionPeerBase(_Base):
    """A connected peer's public endpoint, read by THIS server for the panel.

    The panel used to read a peer's manifest and files from the browser,
    straight from the peer's origin and without credentials. A lab page served
    with a Content-Security-Policy of ``default-src 'self'`` refuses that
    fetch before it leaves the page, and so does a deployment's CORS rule, so
    every connection read ``offline`` (DEF-PEER-72). Read here the request is
    same-origin for the browser, the stored password unlocks a protected peer
    as it does for a save, and the peer's origin never reaches the page.
    """

    async def _peer(self, key: str, suffix: str, **fetch):
        """The peer's 200 answer for ``<link>/<suffix>``, or None when the
        error was already written; ``fetch`` reaches `_peer_fetch`. A
        failure after a ``started`` future in ``fetch`` resolved is raised
        instead: the browser is receiving the body, no answer can be written.
        """
        try:
            conn = self.connection_store.get(key)
        except NotFoundError as exc:
            self.write_error_json(404, str(exc))
            return None
        link = (conn.get("link") or "").rstrip("/")
        if not link:
            self.write_error_json(400, "Connection has no link - disconnect it and connect again")
            return None
        try:
            certificate = conn.get("certificate", "")
            headers = await self._peer_auth_headers(conn)
            resp = await self._peer_fetch(link + "/" + suffix, headers=headers, certificate=certificate, **fetch)
            if resp.code == 401 and headers:
                # a kept token the peer no longer accepts: unlock once more
                headers = await self._peer_auth_headers(conn, fresh=True)
                resp = await self._peer_fetch(link + "/" + suffix, headers=headers, certificate=certificate, **fetch)
        except PeerUnavailable as exc:
            started = fetch.get("started")
            if started is not None and started.done():
                raise
            self.write_error_json(502, str(exc), exc.reason)
            return None
        if resp.code != 200:
            self.write_error_json(*_peer_answer(resp.code))
            return None
        return resp


class ConnectionManifestHandler(_ConnectionPeerBase):
    """api/connections/<key>/manifest - the peer's manifest, never stored by
    a cache: it is per caller and changes on every add or remove."""

    @tornado.web.authenticated
    async def get(self, key):
        resp = await self._peer(key, "manifest")
        if resp is None:
            return
        try:
            manifest = json.loads(resp.body)
        except ValueError:
            return self.write_error_json(502, "The peer answered with an unreadable manifest")
        self.set_header("Cache-Control", "no-store")
        self.write_json(manifest)


class ConnectionDownloadHandler(_ConnectionPeerBase):
    """api/connections/<key>/download?name=<entry>&max_gb=<limit> - one entry
    of a connected share (a folder arrives as the zip the peer builds),
    handed to the browser as an attachment. Spooled to disk like a save,
    under the same size and time limits, and relayed from the spool as the
    peer's body lands on it; a browser download could not carry the unlock
    header, and the query token the peer accepts would land in the page's
    history."""

    @tornado.web.authenticated
    async def get(self, key):
        name = self.get_argument("name", default="")
        if not name:
            return self.write_error_json(400, "Missing 'name'")
        max_bytes = _download_limit(self.get_argument("max_gb", default=""))
        started = asyncio.get_running_loop().create_future()
        with self._spool() as spool:
            fetch = asyncio.ensure_future(
                self._peer(
                    key, "download/" + quote(name, safe="/"), spool=spool, max_bytes=max_bytes, started=started
                )
            )
            await asyncio.wait({fetch, started}, return_when=asyncio.FIRST_COMPLETED)
            if not started.done():
                # the fetch ended before a 200 the limit allows: the answer
                # is already written, nothing reached the browser
                await fetch
                return
            headers = started.result()
            content_type = headers.get("Content-Type") or "application/octet-stream"
            # a folder arrives as the zip the peer builds; the browser takes
            # this header over the anchor's download attribute
            filename = name.rsplit("/", 1)[-1] + (".zip" if content_type == "application/zip" else "")
            self.set_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
            self.set_header("Cache-Control", "no-store")
            self.set_header("Content-Type", content_type)
            if "Content-Length" in headers:
                # the browser shows progress and detects a short body
                self.set_header("Content-Length", headers["Content-Length"])
            try:
                with open(spool.name, "rb") as tail:
                    while True:
                        chunk = tail.read(_EXTRACT_CHUNK_BYTES)
                        if chunk:
                            self.write(chunk)
                            await self.flush()
                        elif fetch.done():
                            break
                        else:
                            await asyncio.sleep(0.05)
            except StreamClosedError:
                # the browser gave up: a closed spool ends the fetch at the
                # peer's next chunk, and nothing else can be answered
                spool.close()
            try:
                await fetch
            except (PeerUnavailable, OSError, RelayAbandoned):
                # bytes are out, so no JSON answer can follow: the closed
                # connection is what marks the download failed
                self.request.connection.close()
                return
        # APIHandler.finish sets the Content-Type itself; hand it the peer's
        self.finish(set_content_type=content_type)


class ConnectionUploadHandler(_Base):
    """Upload items from the user's workspace to a connected request."""

    @tornado.web.authenticated
    async def post(self, key):
        try:
            conn = self.connection_store.get(key)
        except NotFoundError as exc:
            return self.write_error_json(404, str(exc))
        if conn.get("kind") != "request":
            return self.write_error_json(400, "Connection is not a request")
        body = self.get_json_body() or {}
        paths = body.get("paths") or []
        uploader = (body.get("uploader") or "anonymous").strip() or "anonymous"
        if not isinstance(paths, list) or not paths:
            return self.write_error_json(400, "'paths' must be a non-empty list")

        # the persisted link carries the owner's /user/<name>/ prefix on
        # JupyterHub, which this server cannot rebuild
        link = (conn.get("link") or "").rstrip("/")
        if not link:
            return self.write_error_json(400, "Connection has no link - disconnect it and connect again")
        upload_url = link + "/upload"
        upload_url += "?uploader=" + tornado.escape.url_escape(uploader)

        client = tornado.httpclient.AsyncHTTPClient()
        ws_root = Path(self.workspace_root).resolve()
        sent: list[str] = []

        try:
            # Protected peer? trade the stored password for an unlock token
            auth_headers = await self._peer_auth_headers(conn)
            # Server-to-server uploads have no browser cookie jar. Replay the
            # connection's persisted uploader identity as a Cookie header so
            # every upload (and every file of a folder batch) lands in ONE
            # pool on the peer; the hash the peer mints on the first upload
            # is captured from the response and persisted for next time.
            uploader_hash = conn.get("uploader_hash") or ""

            async def post_one(path: Path, filename: str) -> None:
                nonlocal uploader_hash
                headers = dict(auth_headers or {})
                if uploader_hash:
                    headers["Cookie"] = (
                        f"sf_uploader_{conn['id']}={uploader_hash}"
                    )
                resp_body = await _post_file(
                    client,
                    upload_url,
                    path,
                    filename,
                    validate_cert=self.verify_peer_tls,
                    headers=headers,
                    certificate=conn.get("certificate", ""),
                )
                if not uploader_hash:
                    try:
                        minted = (json.loads(resp_body) or {}).get("me", {}).get("hash", "")
                    except (ValueError, AttributeError):
                        minted = ""
                    if minted:
                        uploader_hash = minted
                        self.connection_store.set_uploader_hash(key, minted)

            for rel in paths:
                if not _is_safe_relative(rel):
                    return self.write_error_json(400, f"Unsafe path: {rel}")
                src = ws_root / rel
                if not src.exists():
                    return self.write_error_json(404, f"Not found: {rel}")
                if src.is_dir():
                    # walk and upload each file with the folder structure preserved in filename
                    for root, _dirs, files in os.walk(src):
                        for fname in files:
                            abs_path = os.path.join(root, fname)
                            rel_name = os.path.join(src.name, os.path.relpath(abs_path, src))
                            await post_one(Path(abs_path), rel_name.replace(os.sep, "/"))
                            sent.append(rel_name)
                else:
                    await post_one(src, src.name)
                    sent.append(src.name)
        except PeerUnavailable as exc:
            return self.write_error_json(502, str(exc))

        self.write_json({"ok": True, "uploaded": sent})


async def _post_file(
    client,
    url: str,
    path: Path,
    filename: str,
    validate_cert: bool = True,
    headers: dict | None = None,
    progress=None,
    certificate: str = "",
) -> bytes:
    """POST one file as the raw body under ``X-Filename`` - the wire shape
    the hub's fileshare service and PublicRequestUploadHandler take - read
    from ``path`` in chunks as it is sent, never held whole. Returns the
    response body; ``progress`` is called with each chunk's size as it goes.
    ``certificate`` is the one the user trusted for the connection, if any.

    An upload may take PEER_UPLOAD_SECONDS_PER_GB for every GB of the file,
    and at least for one. A timeout and a closed connection - which
    `raise_error=False` does NOT suppress - become a `PeerUnavailable` the
    handler maps to a 502.
    """
    size = path.stat().st_size
    request_timeout = PEER_UPLOAD_SECONDS_PER_GB * max(1.0, size / GB)

    async def produce(write) -> None:
        with open(path, "rb") as f:
            while chunk := f.read(_EXTRACT_CHUNK_BYTES):
                await write(chunk)
                if progress:
                    progress(len(chunk))

    try:
        resp = await client.fetch(
            url,
            method="POST",
            headers={
                "X-Filename": quote(filename),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
                **(headers or {}),
            },
            body_producer=produce,
            raise_error=False,
            connect_timeout=PEER_TIMEOUT_SECONDS,
            request_timeout=request_timeout,
            **_tls(validate_cert, certificate),
        )
    except ssl.SSLCertVerificationError as exc:
        raise PeerUntrusted(url, (exc.verify_message or str(exc)).rstrip(".")) from None
    except ssl.SSLError as exc:
        raise PeerUnavailable(f"The TLS connection to the peer failed ({exc})") from None
    except (OSError, ConnectionError):
        raise PeerUnavailable("Could not reach the peer") from None
    except HTTPTimeoutError as exc:
        # tornado says "Timeout during request" when the answer ran out of
        # time, otherwise the connection did not open in time
        waited = request_timeout if "during request" in str(exc) else PEER_TIMEOUT_SECONDS
        raise PeerUnavailable(f"The peer did not answer within {waited:g} s") from None
    except HTTPStreamClosedError:
        raise PeerUnavailable("The peer closed the connection before the upload finished") from None
    if resp.code == 401:
        raise PeerUnavailable(PEER_PASSWORD_CHANGED)
    if resp.code >= 400:
        raise PeerRefused(resp.code, resp.body or b"")
    return resp.body or b""


def _parse_share_link(link: str) -> dict[str, str]:
    """Extract kind/id/host/base_path from a public link URL.

    `base_path` is everything between the host and the extension namespace -
    on JupyterHub that is `/user/<name>/`, on a standalone Jupyter server
    it is `/`. Without it, two users on the same hub look identical when
    we compare hosts for self-connect detection.
    """
    parsed = urlparse(link)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Link must be an absolute URL")
    parts = [p for p in parsed.path.split("/") if p]
    # find marker 'public'
    try:
        idx = parts.index("public")
    except ValueError as exc:
        raise ValueError("Link is not a share/request URL") from exc
    if idx + 2 >= len(parts):
        raise ValueError("Link is malformed")
    kind = parts[idx + 1]
    id_ = parts[idx + 2]
    if kind not in ("share", "request"):
        raise ValueError(f"Unknown kind: {kind}")
    # The path part before EXTENSION_NAMESPACE is the JupyterLab base_url.
    try:
        ns_idx = parts.index(EXTENSION_NAMESPACE)
    except ValueError as exc:
        raise ValueError("Link is not a share/request URL") from exc
    base_path = "/" + "/".join(parts[:ns_idx])
    if not base_path.endswith("/"):
        base_path += "/"
    return {
        "kind": kind,
        "id": id_,
        "host": parsed.scheme + "://" + parsed.netloc,
        "base_path": base_path,
    }


# --------------------------------------------------------------------------- #
# Public (unauthenticated) endpoints
# --------------------------------------------------------------------------- #


class _UncachedPublicMixin:
    """Serve the public pages and manifests uncached.

    Applied to the two manifest endpoints and the two recipient-page
    endpoints. Three concrete reasons, all about a *stored* response being
    reused:

    1. Privacy. `PublicRequestManifestHandler` varies its body by cookie - it
       filters `uploaders` down to the caller's own pool. It carried an ETag,
       no `Cache-Control` and no `Vary: Cookie`, so any shared cache keyed on
       URL alone (a Cloudflare cache rule, a corporate proxy) could hand one
       uploader's file list to another. `no-store` closes that.
    2. Staleness. A manifest changes whenever a file is added or removed, and
       with no `Cache-Control` a browser is free to reuse a stored copy under
       heuristic freshness - showing a file list that silently omits the file
       the recipient was told to download.
    3. A stale gate. The recipient page bakes `password_required` into its
       HTML at render time; a cached page from before the owner set a password
       skips the prompt, so the manifest fetch 401s. (The page also recovers
       from that on its own now - see `loadShare` in `static/standalone.html`.)

    Not a reason: a `304` reaching JavaScript. A browser consumes the 304 its
    own cache solicited and resolves the fetch with the stored 200; only a
    request that sets `If-None-Match` itself sees a 304, and no client here
    does. Suppressing the ETag simply keeps this endpoint out of caches
    entirely, which is what both reasons above require.

    `no-store` forbids storing; `no-cache` and `max-age=0` are belt-and-braces
    for intermediaries that predate or ignore it (a Cloudflare tunnel sits in
    this path for public links). Do not reduce this to `must-revalidate`,
    which alone still permits serving a heuristically-fresh response without
    revalidating - reason 2 above.
    """

    def compute_etag(self):
        return None

    def set_default_headers(self):
        # Runs via RequestHandler.clear() on success AND error paths, so a 401
        # from the password gate or a 404 is uncacheable too - a cached 401
        # must not be replayed after the recipient unlocks.
        super().set_default_headers()
        self.set_header("Cache-Control", "no-store, no-cache, max-age=0")


class PublicSharePageHandler(_UncachedPublicMixin, _PublicBase):
    def get(self, id_):
        if not self.share_store.exists(id_):
            self.set_status(404)
            self.set_header("Content-Type", "text/html")
            self.finish("<h1>Share not found</h1>")
            return
        template_path = os.path.join(os.path.dirname(__file__), "static", "standalone.html")
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read()
        # the standalone JS reads kind/id from window.__SHARE_FILES_CONTEXT__
        base_url = self.settings.get("base_url", "/")
        ctx = json.dumps({
            "kind": "share",
            "id": id_,
            "api_base": url_path_join(base_url, EXTENSION_NAMESPACE, "public"),
            "password_required": bool(self.share_store.get_password(id_)),
        })
        html = html.replace("__CONTEXT__", ctx)
        self.set_header("Content-Type", "text/html")
        self.finish(html)


class PublicRequestPageHandler(_UncachedPublicMixin, _PublicBase):
    def get(self, id_):
        if not self.request_store.exists(id_):
            self.set_status(404)
            self.set_header("Content-Type", "text/html")
            self.finish("<h1>Request not found</h1>")
            return
        template_path = os.path.join(os.path.dirname(__file__), "static", "standalone.html")
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read()
        base_url = self.settings.get("base_url", "/")
        ctx = json.dumps({
            "kind": "request",
            "id": id_,
            "api_base": url_path_join(base_url, EXTENSION_NAMESPACE, "public"),
            "password_required": bool(self.request_store.get_password(id_)),
        })
        html = html.replace("__CONTEXT__", ctx)
        self.set_header("Content-Type", "text/html")
        self.finish(html)


class PublicShareManifestHandler(_UncachedPublicMixin, _PublicBase):
    def get(self, id_):
        if not _password_gate(self, self.share_store, id_):
            return
        try:
            manifest = self.share_store.get(id_)
        except NotFoundError:
            self.set_status(404)
            self.finish(json.dumps({"error": "not found"}))
            return
        _strip_owner_fields(manifest)
        manifest["link"] = _public_share_url(self, id_)
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(manifest))


class PublicRequestManifestHandler(_UncachedPublicMixin, _PublicBase):
    def get(self, id_):
        if not _password_gate(self, self.request_store, id_):
            return
        try:
            manifest = self.request_store.get(id_)
        except NotFoundError:
            self.set_status(404)
            self.finish(json.dumps({"error": "not found"}))
            return
        manifest["link"] = _public_request_url(self, id_)
        # never expose other people's uploads publicly - only the caller's
        # own pool, identified by the uploader cookie, is returned
        uploader_hash = _uploader_hash_from_cookie(self, id_)
        mine = [
            u for u in manifest.get("uploaders", [])
            if uploader_hash and u.get("hash") == uploader_hash
        ]
        manifest["uploaders"] = mine
        if mine:
            manifest["me"] = {"hash": uploader_hash, "name": mine[0].get("name")}
        _strip_owner_fields(manifest)
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(manifest))


class PublicShareDownloadHandler(_PublicBase):
    async def get(self, id_, sub_path):
        if not _password_gate(self, self.share_store, id_):
            return
        try:
            target = self.share_store.resolve_data_path(id_, sub_path)
        except (NotFoundError, StorageError):
            self.set_status(404)
            self.finish("Not found")
            return
        if target.is_dir():
            await self._serve_zip(target, target.name, target.name)
        else:
            await self._serve_file(target)

    async def _serve_file(self, path: Path):
        self.set_header("Content-Type", "application/octet-stream")
        self.set_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(path.name))
        self.set_header("Content-Length", str(path.stat().st_size))
        # flushed chunk by chunk: without the flush tornado keeps the whole
        # file in its write buffer until finish()
        with open(path, "rb") as f:
            while chunk := f.read(_EXTRACT_CHUNK_BYTES):
                self.write(chunk)
                await self.flush()
        self.finish()


class PublicShareDownloadAllHandler(_PublicBase):
    async def get(self, id_):
        if not _password_gate(self, self.share_store, id_):
            return
        try:
            data_dir = self.share_store.resolve_data_path(id_)
            manifest = self.share_store.get(id_)
        except NotFoundError:
            self.set_status(404)
            self.finish("Not found")
            return
        await self._serve_zip(data_dir, manifest.get("slug") or id_, "")


def _uploader_cookie_name(id_: str) -> str:
    return f"sf_uploader_{id_}"


def _uploader_hash_from_cookie(handler, id_: str) -> str:
    """Read and sanity-check the uploader identity cookie. '' when absent."""
    raw = handler.get_cookie(_uploader_cookie_name(id_), "") or ""
    # hashes are short base32 - reject anything else (forged / corrupted)
    if not re.fullmatch(r"[A-Z2-7]{4,16}", raw):
        return ""
    return raw


@tornado.web.stream_request_body
class PublicRequestUploadHandler(_PublicBase):
    """One file per POST, the raw body under ``X-Filename`` (percent-encoded
    UTF-8, '/' for folder uploads) - the wire shape the hub's fileshare
    service takes. The body is spooled to the store's ``tmp`` folder as it
    arrives and moved into the request once complete, so an upload is never
    held in memory and jupyter_server's 512 MiB body limit does not apply.
    """

    def initialize(self):
        self._spool = None

    def prepare(self):
        if self.request.method != "POST":
            return
        id_ = self.path_args[0]
        if not _password_gate(self, self.request_store, id_):
            return
        if not self.request_store.exists(id_):
            self.set_status(404)
            self.finish(json.dumps({"error": "not found"}))
            return
        # identity comes from the cookie, never from the client's parameters;
        # first upload mints a hash and sets the cookie on the response
        self._uploader_hash = _uploader_hash_from_cookie(self, id_)
        if not self._uploader_hash:
            self._uploader_hash = mint_uploader_hash()
            self.set_cookie(
                _uploader_cookie_name(id_),
                self._uploader_hash,
                httponly=True,
                expires_days=365,
                samesite="Lax",
            )
        try:
            self._filename = unquote(self.request.headers.get("X-Filename", ""), errors="strict")
        except UnicodeDecodeError:
            self._filename = ""
        if not self._filename:
            self.set_status(400)
            self.finish(json.dumps({"error": "no file"}))
            return
        tmp = resolve_shares_dir(self.workspace_root, self.shares_dir) / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(tmp).free
        length = self.request.headers.get("Content-Length", "")
        if length.isdigit() and int(length) > free:
            self.set_status(413)
            self.finish(json.dumps({"error": "Not enough space"}))
            return
        # tornado reads the body after prepare(): the limit set here replaces
        # the server's default for this request
        self.request.connection.set_max_body_size(free)
        self._spool = tempfile.NamedTemporaryFile(dir=tmp, prefix="upload-", suffix=".part", delete=False)

    def _refuse_store(self):
        # answered now: tornado closes the connection under the rest of the
        # body, as prepare()'s 413 does, so the recipient's upload stops
        self.set_status(500)
        self.finish(json.dumps({"error": "Could not store the upload"}))

    def data_received(self, chunk):
        try:
            self._spool.write(chunk)
        except OSError:
            self._refuse_store()

    def post(self, id_):
        try:
            self._spool.close()
        except OSError:
            # the disk refused the last buffered bytes
            return self._refuse_store()
        uploader_name = self.get_argument("uploader", default="anonymous")
        try:
            self.request_store.add_upload(
                id_, self._uploader_hash, uploader_name, self._filename, Path(self._spool.name)
            )
        except StorageError as exc:
            self.set_status(400)
            self.finish(json.dumps({"error": str(exc)}))
            return
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps({
            "ok": True,
            "count": 1,
            "me": {"hash": self._uploader_hash, "name": uploader_name},
        }))

    def _drop_spool(self):
        # a failed upload, or one whose client dropped mid-body, leaves
        # nothing in tmp
        if self._spool is not None:
            try:
                self._spool.close()
            except OSError:
                pass  # the disk refused the buffered tail; the file goes anyway
            Path(self._spool.name).unlink(missing_ok=True)

    def on_finish(self):
        self._drop_spool()

    def on_connection_close(self):
        # the base marks the body future closed so the pending post() unwinds
        super().on_connection_close()
        self._drop_spool()

    def delete(self, id_):
        """Remove one of the caller's own uploads - identity from the cookie only."""
        if not _password_gate(self, self.request_store, id_):
            return
        uploader_hash = _uploader_hash_from_cookie(self, id_)
        if not uploader_hash:
            self.set_status(403)
            self.finish(json.dumps({"error": "no uploader identity"}))
            return
        name = self.get_argument("name", default="")
        if not name:
            self.set_status(400)
            self.finish(json.dumps({"error": "Missing ?name=..."}))
            return
        try:
            self.request_store.remove_upload(id_, uploader_hash, name)
        except NotFoundError:
            self.set_status(404)
            self.finish(json.dumps({"error": "not found"}))
            return
        except StorageError as exc:
            self.set_status(400)
            self.finish(json.dumps({"error": str(exc)}))
            return
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps({"ok": True}))


# --------------------------------------------------------------------------- #
# Route registration
# --------------------------------------------------------------------------- #


def setup_route_handlers(web_app, config: ShareFilesConfig | None = None):
    host_pattern = ".*$"
    base_url = web_app.settings["base_url"]
    ns = EXTENSION_NAMESPACE
    # store config in settings so handlers can read it
    web_app.settings["share_files_config"] = config or ShareFilesConfig()

    if hub_mode():
        # Hub-managed lab: the panel works through the hub fileshare API and
        # nothing unauthenticated is mounted - no public/*, no static/*.
        from .hub_routes import hub_handlers

        web_app.add_handlers(host_pattern, hub_handlers(base_url, ns))
        return

    static_path = os.path.join(os.path.dirname(__file__), "static")

    handlers = [
        # api/info
        (url_path_join(base_url, ns, "api", "info"), InfoHandler),
        # api/link-check
        (url_path_join(base_url, ns, "api", "link-check"), LinkCheckHandler),
        # api/tunnel
        (url_path_join(base_url, ns, "api", "tunnel"), TunnelHandler),
        (url_path_join(base_url, ns, "api", "tunnel", "setup"), TunnelSetupHandler),
        (url_path_join(base_url, ns, "api", "tunnel", "reset"), TunnelResetHandler),
        # api/generate-password
        (url_path_join(base_url, ns, "api", "generate-password"), GeneratePasswordHandler),
        # api/<shares|requests>/<id>/password
        (url_path_join(base_url, ns, "api", r"(shares|requests)", r"([A-Z2-7]{6,16})", "password"), PasswordHandler),
        # api/<shares|requests>/<id>/save
        (url_path_join(base_url, ns, "api", r"(shares|requests)", r"([A-Z2-7]{6,16})", "save"), RecordSaveHandler),
        # api/shares
        (url_path_join(base_url, ns, "api", "shares"), SharesListHandler),
        (url_path_join(base_url, ns, "api", "shares", r"([A-Z2-7]{6,16})"), ShareItemHandler),
        (url_path_join(base_url, ns, "api", "shares", r"([A-Z2-7]{6,16})", "items"), ShareItemsHandler),
        # api/requests
        (url_path_join(base_url, ns, "api", "requests"), RequestsListHandler),
        (url_path_join(base_url, ns, "api", "requests", r"([A-Z2-7]{6,16})"), RequestItemHandler),
        (url_path_join(base_url, ns, "api", "requests", r"([A-Z2-7]{6,16})", "uploads"), RequestUploadsHandler),
        (url_path_join(base_url, ns, "api", "requests", r"([A-Z2-7]{6,16})", "seen"), RequestSeenHandler),
        # api/connections
        (url_path_join(base_url, ns, "api", "connections"), ConnectionsHandler),
        (url_path_join(base_url, ns, "api", "connections", r"([^/]+)", "save"), ConnectionSaveHandler),
        (url_path_join(base_url, ns, "api", "connections", r"([^/]+)", "upload"), ConnectionUploadHandler),
        (url_path_join(base_url, ns, "api", "connections", r"([^/]+)", "manifest"), ConnectionManifestHandler),
        (url_path_join(base_url, ns, "api", "connections", r"([^/]+)", "download"), ConnectionDownloadHandler),
        (url_path_join(base_url, ns, "api", "connections", r"([^/]+)"), ConnectionItemHandler),
        # public unlock (password attempt; rate limited)
        (url_path_join(base_url, ns, "public", r"(share|request)", r"([A-Z2-7]{6,16})", "unlock"), PublicUnlockHandler),
        # public/share
        (url_path_join(base_url, ns, "public", "share", r"([A-Z2-7]{6,16})"), PublicSharePageHandler),
        (url_path_join(base_url, ns, "public", "share", r"([A-Z2-7]{6,16})", "manifest"), PublicShareManifestHandler),
        (url_path_join(base_url, ns, "public", "share", r"([A-Z2-7]{6,16})", "download-all"), PublicShareDownloadAllHandler),
        (url_path_join(base_url, ns, "public", "share", r"([A-Z2-7]{6,16})", "download", r"(.+)"), PublicShareDownloadHandler),
        # public/request
        (url_path_join(base_url, ns, "public", "request", r"([A-Z2-7]{6,16})"), PublicRequestPageHandler),
        (url_path_join(base_url, ns, "public", "request", r"([A-Z2-7]{6,16})", "manifest"), PublicRequestManifestHandler),
        (url_path_join(base_url, ns, "public", "request", r"([A-Z2-7]{6,16})", "upload"), PublicRequestUploadHandler),
        # static assets used by the standalone page
        (url_path_join(base_url, ns, "static", "(.*)"), StaticFileHandler, {"path": static_path}),
    ]

    web_app.add_handlers(host_pattern, handlers)
