"""The service skeleton: it boots, reports health and readiness, exposes nothing extra, and
manages one upstream client for its lifetime."""

from __future__ import annotations

import importlib
import sys

import httpx
import pytest
from fastapi.testclient import TestClient
from theygent_ai_gateway import __main__ as entrypoint
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.settings import ENV_HOST, ENV_PORT, GatewaySettings


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_the_gateway(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "ready": True,
        "gatewayId": "gw-test",
        "authMode": "personal",
        "mode": "observer",  # standalone: nobody to configure it, so it observes from the start
        "configVersion": None,
        "upstreamUrl": "https://api.anthropic.com",
        "controlPlaneUrl": None,
    }


def test_readyz_is_not_ready_outside_the_lifespan(settings: GatewaySettings) -> None:
    # Without the lifespan there is no upstream client, so the gateway must not claim readiness.
    client = TestClient(create_app(settings))
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["ready"] is False


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_generated_docs_routes_are_off(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_the_upstream_client_uses_the_injected_transport(settings: GatewaySettings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    app = create_app(settings, upstream_transport=httpx.MockTransport(handler))
    with TestClient(app):
        upstream: httpx.AsyncClient = app.state.upstream
        assert str(upstream.base_url).rstrip("/") == settings.upstream_url
        assert upstream.follow_redirects is False
        assert upstream.timeout.read is None
    assert app.state.upstream is None  # closed with the app


def test_asgi_module_builds_an_app_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_PORT, "")  # empty means unset
    sys.modules.pop("theygent_ai_gateway.asgi", None)
    module = importlib.import_module("theygent_ai_gateway.asgi")
    assert module.app.state.settings.port == 8082


def test_main_exits_loudly_on_bad_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_HOST, "0.0.0.0")  # personal mode on a public address
    with pytest.raises(SystemExit) as exit_info:
        entrypoint.main()
    assert "loopback" in str(exit_info.value)


def test_main_serves_on_the_configured_address(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_run(app: object, *, host: str, port: int) -> None:
        calls.update(app=app, host=host, port=port)

    monkeypatch.setenv(ENV_PORT, "9123")
    monkeypatch.setattr(entrypoint.uvicorn, "run", fake_run)
    entrypoint.main()
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 9123
