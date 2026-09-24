"""Renaming a share (ACC-EDIT-189 to ACC-EDIT-192): the sidecar rewrite and
the owner route, in the stub-handler style of test_password.py."""

from __future__ import annotations

import os
import time
import zipfile

import pytest

from jupyterlab_share_files_extension import routes
from jupyterlab_share_files_extension.storage import NotFoundError, ShareStore
from jupyterlab_share_files_extension.tests._stubs import stub_handler


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "hello.txt").write_text("hi")
    return tmp_path


def _share(workspace, name="Quarter Report", password=""):
    store = ShareStore(str(workspace))
    return store, store.create(name, ["hello.txt"], password=password)


def test_a_rename_keeps_the_id_the_password_and_the_files(workspace):
    store, share = _share(workspace, password="open-sesame")
    renamed = store.rename(share["id"], "Year End")
    assert renamed["id"] == share["id"]
    assert renamed["name"] == "Year End"
    assert store.get_password(share["id"]) == "open-sesame"
    assert [e["name"] for e in renamed["entries"]] == ["hello.txt"]
    assert [s["name"] for s in store.list()] == ["Year End"]


def test_a_rename_moves_nothing_on_disk(workspace):
    store, share = _share(workspace)
    before = sorted(p.name for p in store.root.iterdir())
    path = store.get(share["id"])["path"]
    store.rename(share["id"], "Year End")
    assert sorted(p.name for p in store.root.iterdir()) == before
    assert store.get(share["id"])["path"] == path
    assert store.get(share["id"])["slug"] == "Year-End"


def test_a_rename_keeps_the_creation_time(workspace):
    store, share = _share(workspace)
    created = share["created_at"]
    manifest = store._manifest_path_for(share["id"])
    os.utime(manifest, (created - 100, created - 100))
    time.sleep(0.01)
    assert store.rename(share["id"], "Year End")["created_at"] == created - 100


def test_a_renamed_share_downloads_under_its_new_name(workspace):
    store, share = _share(workspace)
    store.rename(share["id"], "Year End")
    target = workspace / "out"
    target.mkdir()
    assert store.save_out(share["id"], target) == "out/Year-End"
    assert store.save_out(share["id"], target, "zip") == "out/Year-End.zip"
    with zipfile.ZipFile(target / "Year-End.zip") as zf:
        assert zf.namelist() == ["hello.txt"]


def test_a_rename_to_the_same_slug_changes_the_name_only(workspace):
    store, share = _share(workspace, name="Report")
    assert store.rename(share["id"], "Report!")["name"] == "Report!"
    assert store.get(share["id"])["slug"] == "Report"


def test_a_rename_of_an_unknown_share_is_not_found(workspace):
    store = ShareStore(str(workspace))
    with pytest.raises(NotFoundError):
        store.rename("ABCDEFGH", "Anything")


def test_the_route_renames_and_keeps_the_link(workspace):
    store, share = _share(workspace)
    handler = stub_handler(routes.ShareNameHandler, workspace, body={"name": "Year End"})
    before = routes._public_share_url(handler, share["id"])
    handler.put(share["id"])
    assert handler.status == 200
    assert handler.payload["name"] == "Year End"
    assert handler.payload["id"] == share["id"]
    assert handler.payload["link"] == before


@pytest.mark.parametrize("name", ["", "   ", None])
def test_the_route_refuses_an_empty_name(workspace, name):
    store, share = _share(workspace)
    handler = stub_handler(routes.ShareNameHandler, workspace, body={"name": name})
    handler.put(share["id"])
    assert handler.status == 400
    assert handler.payload == {"error": "Missing 'name'"}
    assert store.get(share["id"])["name"] == "Quarter Report"


def test_the_route_answers_404_for_an_unknown_share(workspace):
    handler = stub_handler(routes.ShareNameHandler, workspace, body={"name": "X"})
    handler.put("ABCDEFGH")
    assert handler.status == 404
