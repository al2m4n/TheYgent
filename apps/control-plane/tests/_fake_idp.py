"""A tiny threaded identity provider for the OIDC callback tests.

Serves the two endpoints the control plane's server-side flow actually calls — ``POST
/token`` (the code exchange) and ``GET /userinfo`` — plus a discovery document, so a test
provider can be configured either with explicit endpoints or an ``issuer_url``. Real HTTP on
an ephemeral port (the in-process shortcut would bypass the httpx layer under test).
``claims`` is mutable per test; ``captured`` records the last token-exchange form so tests
can assert PKCE/client_secret actually crossed the wire.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs


class FakeIdp:
    def __init__(self) -> None:
        # A well-behaved OIDC provider's userinfo — includes email_verified:true (Google/Okta
        # do). Tests that exercise the lenient/plain-OAuth2 path override claims to drop it.
        self.claims: dict[str, Any] = {
            "sub": "sub-123",
            "email": "casey@example.com",
            "email_verified": True,
            "name": "Casey Example",
            "preferred_username": "casey",
            "picture": "https://cdn.example.com/casey.png",
        }
        self.captured: dict[str, Any] = {}
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:  # keep test output quiet
                pass

            def _json(self, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path.startswith("/.well-known/openid-configuration"):
                    self._json(
                        {
                            "authorization_endpoint": f"{idp.url}/authorize",
                            "token_endpoint": f"{idp.url}/token",
                            "userinfo_endpoint": f"{idp.url}/userinfo",
                        }
                    )
                elif self.path.startswith("/userinfo"):
                    idp.captured["userinfo_auth"] = self.headers.get("Authorization")
                    self._json(idp.claims)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self) -> None:
                if self.path.startswith("/token"):
                    length = int(self.headers.get("Content-Length") or 0)
                    form = parse_qs(self.rfile.read(length).decode())
                    idp.captured["token_form"] = {k: v[0] for k, v in form.items()}
                    self._json({"access_token": "at-fake-123", "token_type": "Bearer"})
                else:
                    self.send_response(404)
                    self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeIdp:
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
