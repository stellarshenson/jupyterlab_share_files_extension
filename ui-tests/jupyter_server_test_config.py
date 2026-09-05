"""Server configuration for integration tests.

!! Never use this configuration in production because it
opens the server to the world and provide access to JupyterLab
JavaScript objects through the global window variable.
"""
import os
import tempfile

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

# Match the port playwright.config.js waits on. `or`, not a get() default: an
# exported-but-empty JUPYTER_TEST_PORT would make int("") raise while Playwright
# waited happily on 8888.
c.ServerApp.port = int(os.environ.get("JUPYTER_TEST_PORT") or "8888")

# Serve the working tree's labextension build (`jlpm build`) ahead of any
# installed copy. Federated extensions resolve first-path-wins and their
# static files are looked up by DIRECTORY name, so `labextensions/` holds a
# symlink named after the package pointing at the build output. Galata's own
# helper extension (set above, as a string) stays on the list, and on the
# same class - a LabApp setting would shadow it.
c.LabServerApp.extra_labextensions_path = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "labextensions"),
    c.LabServerApp.extra_labextensions_path,
]

# Delete outright: galata removes each test's folder through the contents
# API and a cut-paste test deletes its original, and a temp root has no trash
# the server may write to. Set on the concrete classes - a site config that
# pins them to True on the subclass outranks a FileContentsManager setting.
c.FileContentsManager.delete_to_trash = False
c.AsyncFileContentsManager.delete_to_trash = False
c.AsyncLargeFileManager.delete_to_trash = False

# Uncomment to set server log level to debug level
# c.ServerApp.log_level = "DEBUG"
