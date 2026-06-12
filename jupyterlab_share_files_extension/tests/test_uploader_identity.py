"""Unit tests for the per-uploader identity cookie helpers.

The identity hash is server-issued and travels only in the
`sf_uploader_<id>` cookie - these tests pin the validation rules that stop
a forged cookie value from reaching the storage layer as a path component.
"""

from __future__ import annotations

from jupyterlab_share_files_extension import routes
from jupyterlab_share_files_extension.storage import mint_uploader_hash


class _FakeHandler:
    def __init__(self, cookies=None):
        self._cookies = cookies or {}

    def get_cookie(self, name, default=""):
        return self._cookies.get(name, default)


def test_cookie_name_is_scoped_per_request():
    assert routes._uploader_cookie_name("ABC123") == "sf_uploader_ABC123"


def test_minted_hash_is_short_base32():
    h = mint_uploader_hash()
    assert len(h) == 6
    assert routes._uploader_hash_from_cookie(
        _FakeHandler({"sf_uploader_ID1": h}), "ID1"
    ) == h


def test_missing_cookie_yields_empty():
    assert routes._uploader_hash_from_cookie(_FakeHandler(), "ID1") == ""


def test_forged_cookie_values_rejected():
    bad = ["../etc", "a/b", "hash with space", "x" * 40, "lower", ""]
    for value in bad:
        handler = _FakeHandler({"sf_uploader_ID1": value})
        assert routes._uploader_hash_from_cookie(handler, "ID1") == ""


def test_cookie_for_other_request_not_used():
    handler = _FakeHandler({"sf_uploader_OTHER": "HASH01"})
    assert routes._uploader_hash_from_cookie(handler, "ID1") == ""
