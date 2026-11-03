"""A standalone add lands in the folder it targets (ACC-EDIT-232): the store
and the owner route, in the stub-handler style of test_share_rename.py.
The folder is the workspace path the panel holds for a folder of the share."""

from __future__ import annotations

import os
import re

import pytest

from jupyterlab_share_files_extension import routes
from jupyterlab_share_files_extension.storage import ShareStore, StorageError
from jupyterlab_share_files_extension.tests._stubs import stub_handler


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "docs" / "deep").mkdir(parents=True)
    (tmp_path / "docs" / "keep.txt").write_text("k")
    (tmp_path / "new.txt").write_text("n")
    (tmp_path / "elsewhere").mkdir()
    return tmp_path


def _share(workspace):
    """A share holding the folder docs/ with docs/deep/ inside it: the store,
    its id and the share's own folder on disk and as a workspace path."""
    store = ShareStore(str(workspace))
    share = store.create("Folders", ["docs"])
    path = share["path"]
    return store, share["id"], workspace / path, path


def test_an_add_into_a_folder_lands_in_it(workspace):
    store, id_, content, path = _share(workspace)
    manifest = store.add_items(id_, ["new.txt"], f"{path}/docs")
    assert (content / "docs" / "new.txt").read_text() == "n"
    assert [e["name"] for e in manifest["entries"]] == ["docs"]


def test_an_add_into_a_nested_folder_lands_in_it(workspace):
    store, id_, content, path = _share(workspace)
    store.add_items(id_, ["new.txt"], f"{path}/docs/deep")
    assert (content / "docs" / "deep" / "new.txt").exists()
    assert not (content / "docs" / "new.txt").exists()


def test_an_add_with_no_folder_lands_at_the_top(workspace):
    store, id_, content, _path = _share(workspace)
    manifest = store.add_items(id_, ["new.txt"])
    assert (content / "new.txt").exists()
    assert sorted(e["name"] for e in manifest["entries"]) == ["docs", "new.txt"]


def test_an_add_into_a_folder_keeps_the_copy_rules(workspace):
    store, id_, content, path = _share(workspace)
    (workspace / ".ipynb_checkpoints").mkdir()
    store.add_items(id_, ["new.txt"], f"{path}/docs")
    store.add_items(id_, ["new.txt"], f"{path}/docs")
    assert (content / "docs" / "new-2.txt").exists()
    with pytest.raises(StorageError, match="on the excluded list"):
        store.add_items(id_, [".ipynb_checkpoints"], f"{path}/docs")


@pytest.mark.parametrize(
    "folder",
    [
        "{path}/nope",
        "{path}/docs/keep.txt",
        "..",
        "{path}/..",
        "docs",
        "elsewhere",
        "{root}/elsewhere",
        "/",
        "{path}/docs/out",
        "{other}",
    ],
    ids=[
        "missing",
        "a file",
        "dotdot",
        "above the share",
        "the source folder",
        "a workspace folder",
        "absolute",
        "root",
        "a link that leads out",
        "another share",
    ],
)
def test_a_folder_that_is_not_a_folder_of_the_share_is_refused(workspace, folder):
    store, id_, content, path = _share(workspace)
    os.symlink(workspace / "elsewhere", content / "docs" / "out")
    (workspace / "other.txt").write_text("o")
    other = store.create("Other", ["other.txt"])["path"]
    folder = folder.format(path=path, root=workspace, other=other)
    before = sorted(p.relative_to(workspace) for p in workspace.rglob("*"))
    with pytest.raises(StorageError, match=re.escape(f"This share has no folder {folder}")):
        store.add_items(id_, ["new.txt"], folder)
    assert sorted(p.relative_to(workspace) for p in workspace.rglob("*")) == before


@pytest.mark.parametrize(
    "source, folder",
    [
        ("{path}/docs", "{path}/docs"),
        ("{path}/docs", "{path}/docs/deep"),
        ("{top}", ""),
    ],
    ids=[
        "the folder itself",
        "a folder that holds the target",
        "a folder that holds the share",
    ],
)
def test_a_folder_added_into_itself_is_refused(workspace, source, folder):
    # a cut pasted into its own folder lost its only copy, and a copy into a
    # folder inside the source walked its own copy without end
    store, id_, _content, path = _share(workspace)
    source = source.format(path=path, top=path.split("/")[0])
    folder = folder.format(path=path)
    name = source.split("/")[-1]
    before = sorted(p.relative_to(workspace) for p in workspace.rglob("*"))
    with pytest.raises(
        StorageError,
        match=re.escape(f"Cannot add {name} into itself or into a folder inside it"),
    ):
        store.add_items(id_, [source], folder)
    assert sorted(p.relative_to(workspace) for p in workspace.rglob("*")) == before


def test_the_route_adds_into_the_folder(workspace):
    _store, id_, content, path = _share(workspace)
    handler = stub_handler(
        routes.ShareItemsHandler,
        workspace,
        body={"paths": ["new.txt"], "folder": f"{path}/docs"},
    )
    handler.post(id_)
    assert handler.status == 200
    assert (content / "docs" / "new.txt").exists()
    assert handler.payload["link"]


@pytest.mark.parametrize("body", [{}, {"folder": ""}, {"folder": None}])
def test_the_route_with_no_folder_adds_at_the_top(workspace, body):
    _store, id_, content, _path = _share(workspace)
    handler = stub_handler(
        routes.ShareItemsHandler, workspace, body={"paths": ["new.txt"], **body}
    )
    handler.post(id_)
    assert handler.status == 200
    assert (content / "new.txt").exists()


def test_the_route_refuses_a_folder_outside_the_share_in_words(workspace):
    _store, id_, content, _path = _share(workspace)
    handler = stub_handler(
        routes.ShareItemsHandler,
        workspace,
        body={"paths": ["new.txt"], "folder": "docs"},
    )
    handler.post(id_)
    assert handler.status == 400
    assert handler.payload == {"error": "This share has no folder docs"}
    assert not (workspace / "docs" / "new.txt").exists()
    assert not (content / "new.txt").exists()


@pytest.mark.parametrize("folder", [["docs"], 5, {"path": "docs"}])
def test_the_route_refuses_a_folder_that_is_not_a_string(workspace, folder):
    _store, id_, content, _path = _share(workspace)
    handler = stub_handler(
        routes.ShareItemsHandler,
        workspace,
        body={"paths": ["new.txt"], "folder": folder},
    )
    handler.post(id_)
    assert handler.status == 400
    assert handler.payload == {"error": "'folder' must be a string"}
    assert not (content / "new.txt").exists()
