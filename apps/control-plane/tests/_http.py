"""A real, threaded local HTTP server for the ``http_fetch`` tool test.

Same posture as ``_fake_inference`` (everything real, ephemeral port) but plainer: a fixed
body + status on GET, so the tool makes a genuine outbound HTTP call across a real socket
rather than a mock. Lets the fast suite prove real-async tool execution deterministically,
without depending on the public internet.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast


class _FixedServer(HTTPServer):
    body: bytes = b"hello from test server"
    status: int = 200


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # http.server's required handler method name
        server = cast(_FixedServer, self.server)
        self.send_response(server.status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(server.body)

    def log_message(self, format: str, *args: object) -> None:  # silence per-request logging
        return


class ThreadedHTTP:
    """Context manager yielding a base URL that returns a fixed body/status on any GET."""

    def __init__(self, body: str = "hello from test server", status: int = 200) -> None:
        self._body = body.encode()
        self._status = status

    def __enter__(self) -> str:
        self._server = _FixedServer(("127.0.0.1", 0), _Handler)
        self._server.body = self._body
        self._server.status = self._status
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[:2]
        host = host if isinstance(host, str) else host.decode()
        return f"http://{host}:{port}"

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
