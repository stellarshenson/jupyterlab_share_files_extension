"""Public manifests must be served uncached - they are per-caller and volatile.

A request manifest varies by cookie (it shows only the caller's own uploads)
yet carried an ETag, no `Cache-Control` and no `Vary: Cookie`, so a shared
cache keyed on URL alone could serve one uploader's file list to another. And
any manifest changes as files are added or removed, so a browser reusing a
stored copy under heuristic freshness shows a stale list. The manifest
handlers therefore suppress the ETag and send `Cache-Control: no-store`.

Note this is NOT about a 304 reaching JavaScript: a browser consumes the 304
its own cache solicited and resolves the fetch with the stored 200 (verified
in a real browser). Only a request that sets `If-None-Match` itself sees a
304, and no client here does.

The end-to-end HTTP assertions live in `ui-tests/tests/manifest-cache.spec.ts`
(the route tests here are skipped - pytest-jupyter fixture timeout), so these
cover the handler contract directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import tornado.web

from jupyterlab_share_files_extension.routes import (
    PublicRequestManifestHandler,
    PublicRequestPageHandler,
    PublicShareManifestHandler,
    PublicSharePageHandler,
    _PublicBase,
    _UncachedPublicMixin,
)

MANIFEST_HANDLERS = (PublicShareManifestHandler, PublicRequestManifestHandler)
# the recipient pages bake `password_required` into their HTML, so a cached
# page skips the prompt after the owner sets a password
PAGE_HANDLERS = (PublicSharePageHandler, PublicRequestPageHandler)
UNCACHED_HANDLERS = MANIFEST_HANDLERS + PAGE_HANDLERS


def _headers_of(handler_cls) -> dict[str, str]:
    """Collect the default headers a handler class sets, without a live server."""
    recorded: dict[str, str] = {}

    class _Probe(handler_cls):  # type: ignore[misc, valid-type]
        def __init__(self):  # noqa: D107 - deliberately skip RequestHandler.__init__
            pass

        def set_header(self, name, value):
            recorded[name] = value

    _Probe().set_default_headers()
    return recorded


def test_manifest_handlers_disable_etag():
    """No ETag means no validator for any cache to store the response against."""
    for cls in UNCACHED_HANDLERS:
        assert cls.compute_etag(object()) is None


def test_manifest_handlers_send_no_store():
    for cls in UNCACHED_HANDLERS:
        headers = _headers_of(cls)
        assert "no-store" in headers.get("Cache-Control", "")


def test_manifest_handlers_keep_cors_headers():
    """The no-cache mixin must not drop the CORS headers peers rely on."""
    for cls in MANIFEST_HANDLERS:
        headers = _headers_of(cls)
        assert headers.get("Access-Control-Allow-Origin") == "*"
        assert "X-Share-Token" in headers.get("Access-Control-Allow-Headers", "")


def test_mixin_precedes_public_base_in_mro():
    """The mixin only overrides if it comes first - guard against a reorder."""
    for cls in UNCACHED_HANDLERS:
        mro = cls.__mro__
        assert mro.index(_UncachedPublicMixin) < mro.index(_PublicBase)


def test_standalone_page_fetches_manifests_uncached():
    """The recipient page must not read a manifest from the browser cache.

    A browser that cached a manifest before this fix shipped still holds that
    entry and may reuse it under heuristic freshness, showing a stale file
    list. Every manifest fetch goes through `manifestInit()`, which sets
    `cache: 'no-store'`.
    """
    page = Path(__file__).resolve().parents[1] / "static" / "standalone.html"
    source = page.read_text(encoding="utf-8")
    assert "cache: 'no-store'" in source, "manifestInit() must disable the cache"
    # no manifest fetch may bypass the helper
    bare = re.findall(r"fetch\(\s*api\('/manifest'\)\s*,\s*\{", source)
    assert not bare, f"manifest fetch not using manifestInit(): {bare}"
    assert source.count("fetch(api('/manifest'), manifestInit())") >= 3


def test_standalone_page_recovers_from_a_401():
    """A 401 must show the password prompt, not a dead end.

    `password_required` is baked into the page HTML at render time, so a page
    loaded before the owner set a password skips the prompt and the manifest
    fetch 401s. Both loaders must fall back to the gate instead of rendering
    "unavailable" with no way forward.
    """
    page = Path(__file__).resolve().parents[1] / "static" / "standalone.html"
    source = page.read_text(encoding="utf-8")
    assert source.count("r.status === 401") >= 2, "loadShare and loadRequest both need it"
    assert source.count("renderPasswordGate();\n        return;") >= 2


def test_non_manifest_public_handlers_keep_default_caching():
    """Downloads still benefit from ETag revalidation - do not blanket-disable.

    A browser handles a 304 on a plain download correctly (it serves its cached
    copy), so only the polled JSON manifests opt out.
    """
    assert not issubclass(_PublicBase, _UncachedPublicMixin)
    assert _PublicBase.compute_etag is tornado.web.RequestHandler.compute_etag
