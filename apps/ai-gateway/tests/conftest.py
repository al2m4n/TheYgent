"""Fixtures for the fast suite: the real app, no network."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.settings import (
    ENV_CONTROL_PLANE_URL,
    ENV_INFERENCE_PLANE_URL,
    GatewaySettings,
)


@pytest.fixture(autouse=True)
def _no_gateway_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """`make test` exports the developer's .env; every test starts from a gateway environment
    of its own making."""
    for name in list(os.environ):
        if name.startswith("THEYGENT_AI_GATEWAY_") or name in (
            ENV_CONTROL_PLANE_URL,
            ENV_INFERENCE_PLANE_URL,
        ):
            monkeypatch.delenv(name)


@pytest.fixture
def settings(tmp_path: Path) -> GatewaySettings:
    return GatewaySettings(state_dir=tmp_path / "state", gateway_id="gw-test")


@pytest.fixture
def app(settings: GatewaySettings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
