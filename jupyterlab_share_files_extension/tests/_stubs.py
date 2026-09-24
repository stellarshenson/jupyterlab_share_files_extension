"""A route handler with no Tornado server behind it: a test calls one of its
methods and reads the answer from plain attributes - ``status`` and
``payload`` - instead of from a response."""

from __future__ import annotations

import types

from jupyterlab_share_files_extension.config import ShareFilesConfig


def stub_handler(cls, workspace, *, request=None, config=None, body=None):
    """A handler of ``cls`` over ``workspace``: the JSON body is ``body``,
    ``write_json`` and ``write_error_json`` land in ``payload`` and
    ``status``."""
    handler = object.__new__(cls)
    handler.request = request or types.SimpleNamespace(
        method="POST", headers={}, protocol="http", host="lab.local:8888"
    )
    # RequestHandler.settings is a read-only view of application.settings
    handler.application = types.SimpleNamespace(settings={
        "share_files_config": config or ShareFilesConfig(),
        "base_url": "/",
        "server_root_dir": str(workspace),
    })
    handler._current_user = "tester"  # satisfies @tornado.web.authenticated
    handler.get_json_body = lambda: body
    handler.status = 200
    handler.payload = None
    handler.set_status = lambda code: setattr(handler, "status", code)
    handler.set_header = lambda name, value: None
    handler.write_json = lambda p: setattr(handler, "payload", p)

    def _write_error(code, message, reason=""):
        handler.status = code
        handler.payload = {"error": message, "reason": reason} if reason else {"error": message}

    handler.write_error_json = _write_error
    return handler
