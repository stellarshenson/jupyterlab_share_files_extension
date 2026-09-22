"""Server configuration for integration tests.

!! Never use this configuration in production because it
opens the server to the world and provide access to JupyterLab
JavaScript objects through the global window variable.
"""
import os
import shutil
import sys
import tempfile

from jupyter_core.paths import jupyter_path
from jupyterlab.galata import configure_jupyter_server

# The test root lives beside this file, on the same filesystem as the home
# directory: a site config may force deletes into the trash, and the OS
# trash is not writable for a root under /tmp. Ignored by git.
_GALATA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".galata-root")
os.makedirs(_GALATA_ROOT, exist_ok=True)
os.environ.setdefault(
    "JUPYTERLAB_GALATA_ROOT_DIR", tempfile.mkdtemp(prefix="run-", dir=_GALATA_ROOT)
)

configure_jupyter_server(c)

# No network during a test: the extension manager fetches PyPI metadata for
# every installed extension, which showed up in the run log as dozens of
# outbound https calls while tests were waiting on the panel.
c.LabApp.extension_manager = "readonly"

# Match the port playwright.config.js waits on. `or`, not a get() default: an
# exported-but-empty JUPYTER_TEST_PORT would make int("") raise while Playwright
# waited happily on 8888.
c.ServerApp.port = int(os.environ.get("JUPYTER_TEST_PORT") or "8888")

# Serve the working tree's labextension build (`jlpm build`) ahead of any
# installed copy. Federated extensions resolve first-path-wins and their
# static files are looked up by DIRECTORY name, so the build is copied into
# a directory named after the package. A copy, not a symlink: tornado 6.5.10
# refuses a static file whose real path leaves the served directory, and
# jupyter_server's FileFindHandler does not turn on its follow_dir_symlinks
# for this route. Without a build (CI installs the wheel) the installed copy
# is what gets copied in: the search path below serves this one directory and
# nothing else, so an extension that is only installed has to be brought here
# or the suites run against a lab that does not carry it at all. Galata's own
# helper extension (set above, as a string) stays on the list, and on the same
# class - a LabApp setting would shadow it.
_BUILD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "jupyterlab_share_files_extension",
    "labextension",
)
_SOURCES = [_BUILD] + [
    os.path.join(d, "jupyterlab_share_files_extension") for d in jupyter_path("labextensions")
]
_SOURCE = next((p for p in _SOURCES if os.path.isfile(os.path.join(p, "package.json"))), None)
# Say which copy is under test: the two candidates are different builds - the
# working tree is ahead of the installed wheel - so a run after a clean
# exercises a different panel than a run after `jlpm build`, and the run log
# is where that has to be readable.
# stderr, not stdout: playwright's webServer ignores a server's stdout and
# pipes its stderr, so stdout would put this line nowhere a run log keeps
print(f"labextension served from: {_SOURCE}", file=sys.stderr)
_LABEXTENSIONS = tempfile.mkdtemp(prefix="labextensions-", dir=_GALATA_ROOT)
if _SOURCE:
    shutil.copytree(_SOURCE, os.path.join(_LABEXTENSIONS, "jupyterlab_share_files_extension"))
c.LabServerApp.extra_labextensions_path = [
    _LABEXTENSIONS,
    c.LabServerApp.extra_labextensions_path,
]

# ...and serve NOTHING else. The image these suites run in carries about forty
# other labextensions; their frontends all load into the test page and several
# hold a stream open to this server. Chromium allows six connections per
# origin, so the panel's first `api/info` sat queued behind them - measured at
# 16.4 s from the page while the server answered every request in under 7 ms,
# which is what made a clicked Refresh outlast the panel's own wait and fail
# tests for the image rather than the code (DEF-TESTS-102). Restricting the
# search path to this build plus galata's helper keeps the page to the two
# extensions the suites are about.
import jupyterlab.galata  # noqa: E402

c.LabServerApp.labextensions_path = [
    _LABEXTENSIONS,
    os.path.dirname(os.path.abspath(jupyterlab.galata.__file__)),
]

# Delete outright: galata removes each test's folder through the contents
# API and a cut-paste test deletes its original, and a temp root has no trash
# the server may write to. Set on the concrete classes - a site config that
# pins them to True on the subclass outranks a FileContentsManager setting.
c.FileContentsManager.delete_to_trash = False
c.AsyncFileContentsManager.delete_to_trash = False
c.AsyncLargeFileManager.delete_to_trash = False

# The Content-Security-Policy galaxalab's /galaxalab/etc/jupyter/jupyter_lab_config.py
# puts on every lab page, verbatim. The panel must work under it: a connected
# peer on another origin is read through this server, never straight from the
# browser (DEF-PEER-72).
c.ServerApp.tornado_settings = {
    "headers": {
        "Content-Security-Policy": (
            "frame-ancestors 'self'; default-src 'self' 'unsafe-inline' 'unsafe-eval' "
            "data: blob:; img-src * data: blob: 'unsafe-inline' 'unsafe-eval';"
        )
    }
}

# Uncomment to set server log level to debug level
# c.ServerApp.log_level = "DEBUG"
