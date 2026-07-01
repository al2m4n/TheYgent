"""Fixtures for the fast suite: everything real except the model weights."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from _fake_upstream import FakeUpstreamLauncher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.clock import ManualClock


@pytest.fixture
def launcher() -> FakeUpstreamLauncher:
    return FakeUpstreamLauncher()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def app(launcher: FakeUpstreamLauncher, clock: ManualClock) -> FastAPI:
    # Reaper disabled so idle eviction is driven deterministically via manager.reap().
    return create_app(launcher=launcher, clock=clock, max_resident=2, enable_reaper=False)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
