"""Fixtures for the containerization contract tests.

These tests guard the SHAPE of the Dockerfiles, docker-compose files, and Kubernetes
manifests against repo drift (renamed env vars, moved paths, new workspace members) —
they never need a Docker daemon or a cluster. The few checks that shell out to
``docker``/``kubectl`` skip cleanly when the binary is absent.

Pure helpers/constants live in ``contract_utils`` (imported by name from the tests);
only pytest fixtures belong here.
"""

from __future__ import annotations

import pytest
import yaml
from contract_utils import (
    COMPOSE_FILE,
    COMPOSE_HOST_INFERENCE,
    k8s_manifest_paths,
    scan_known_env_names,
)


@pytest.fixture(scope="session")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text())


@pytest.fixture(scope="session")
def compose_host_inference() -> dict:
    return yaml.safe_load(COMPOSE_HOST_INFERENCE.read_text())


@pytest.fixture(scope="session")
def k8s_docs() -> list[dict]:
    docs: list[dict] = []
    for path in k8s_manifest_paths():
        for doc in yaml.safe_load_all(path.read_text()):
            if doc:
                docs.append(doc)
    return docs


@pytest.fixture(scope="session")
def known_env_names() -> set[str]:
    """Every THEYGENT_*/VITE_*/DATABASE_URL env name first-party source actually reads.

    Deploy files may only set names from this set — so a rename in code (the historical
    THEYGENT_INFERENCE_* → THEYGENT_INFERENCE_PLANE_* kind) fails here instead of silently
    configuring nothing.
    """
    return scan_known_env_names()
