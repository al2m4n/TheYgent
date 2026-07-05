"""A tiny real HTTP server for the http-tool tests.

Like ``FakeInference``, this is a REAL server on an ephemeral port (the codebase tests against real
servers, never mocks — [[prefer-what-real-servers-return]]). It records each request (method, path,
headers, body) so a test can assert the connection injected the auth header server-side (the secret
reached THE WIRE but never the IR), and returns a configurable status + JSON body so a test can
exercise the ok/err contract and ``responseMap``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.body: Any = {"ok": True, "data": {"value": 42}}


def _make_handler(rec: _Recorder) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:  # silence the default stderr logging
            pass

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                parsed_body = json.loads(raw) if raw else None
            except ValueError:
                parsed_body = raw.decode("utf-8", "replace")
            rec.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": parsed_body,
                }
            )
            payload = json.dumps(rec.body).encode("utf-8")
            self.send_response(rec.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_PATCH = _handle
        do_DELETE = _handle

    return Handler


class FakeHttp:
    """A recording HTTP server. ``url`` is its base; ``requests`` holds what it received;
    ``status`` / ``body`` configure the response. Use via the :func:`fake_http` context manager."""

    def __init__(self) -> None:
        self._rec = _Recorder()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self._rec))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._rec.requests

    def set_response(self, *, status: int = 200, body: Any = None) -> None:
        self._rec.status = status
        if body is not None:
            self._rec.body = body


@contextmanager
def fake_http() -> Iterator[FakeHttp]:
    server = FakeHttp()
    server.start()
    try:
        yield server
    finally:
        server.stop()
