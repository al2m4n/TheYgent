"""Fixtures for the fast suite: control-plane against a real (fake-model) inference plane."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from _fake_inference import FakeInference
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app


@pytest.fixture
def fake_inference() -> Iterator[FakeInference]:
    with FakeInference() as server:
        yield server


@pytest.fixture
def client(fake_inference: FakeInference) -> Iterator[TestClient]:
    app = create_app(inference_base_url=fake_inference.v1_url)
    with TestClient(app) as test_client:
        yield test_client
