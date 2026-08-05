"""Server configuration for integration tests.

!! Never use this configuration in production because it
opens the server to the world and provide access to JupyterLab
JavaScript objects through the global window variable.
"""
import os

from jupyterlab.galata import configure_jupyter_server

configure_jupyter_server(c)

# Match the port playwright.config.js waits on. `or`, not a get() default: an
# exported-but-empty JUPYTER_TEST_PORT would make int("") raise while Playwright
# waited happily on 8888.
c.ServerApp.port = int(os.environ.get("JUPYTER_TEST_PORT") or "8888")

# Uncomment to set server log level to debug level
# c.ServerApp.log_level = "DEBUG"
