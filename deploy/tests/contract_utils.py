"""Shared constants/helpers for the containerization contract tests.

Deliberately NOT in conftest.py: tests import from here by name, and a module named
``conftest`` collides across suites when several are collected in one pytest run.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

DOCKERFILES = {
    "control-plane": REPO / "apps/control-plane/Dockerfile",
    "inference-plane": REPO / "apps/inference-plane/Dockerfile",
    "worker": REPO / "apps/worker/Dockerfile",
    "interface": REPO / "apps/interface/Dockerfile",
}

COMPOSE_FILE = REPO / "docker-compose.yml"
COMPOSE_HOST_INFERENCE = REPO / "docker-compose.host-inference.yml"
K8S_DIR = REPO / "deploy/k8s"

# Env names that configure third-party layers (compose interpolation, Vite, Postgres,
# DBOS) rather than being read by first-party source — the env-contract scan whitelists
# them explicitly so a typo in anything else still fails.
EXTERNAL_ENV_ALLOWLIST = {
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "PGDATA",
    "HF_HOME",
    "LD_LIBRARY_PATH",
    "PATH",
    "DBOS_SYSTEM_DATABASE_URL",
}


def k8s_manifest_paths() -> list[Path]:
    """Every manifest file in deploy/k8s, BOTH yaml spellings (a .yml must not silently
    escape the suite)."""
    return sorted(
        p
        for p in (*K8S_DIR.glob("*.yaml"), *K8S_DIR.glob("*.yml"))
        if p.name not in ("kustomization.yaml", "kustomization.yml")
    )


def scan_known_env_names() -> set[str]:
    """Every THEYGENT_*/VITE_* env name appearing in FIRST-PARTY SOURCE (apps/*/src,
    packages/*/src — never tests or docs, which would keep stale names alive in the
    oracle). Whole-file scan rather than environ-call lines only: the codebase reads
    several vars through named constants (e.g. a SECRET_KEY_ENV constant), where the
    name and the ``os.environ`` call sit on different lines."""
    names: set[str] = {"DATABASE_URL"}
    pattern = re.compile(r"\b(THEYGENT_[A-Z0-9_]+|VITE_[A-Z0-9_]+)\b")
    suffixes = {".py", ".ts", ".tsx"}
    for src_root in (*REPO.glob("apps/*/src"), *REPO.glob("packages/*/src")):
        for path in src_root.rglob("*"):
            if path.suffix in suffixes and "node_modules" not in path.parts:
                names.update(pattern.findall(path.read_text(errors="ignore")))
    return names


def env_names_used(mapping: dict | list | None) -> set[str]:
    """Env NAMES from a compose ``environment`` block (map or list form)."""
    if mapping is None:
        return set()
    if isinstance(mapping, dict):
        return set(mapping)
    return {entry.split("=", 1)[0] for entry in mapping}


def pod_spec_of(doc: dict) -> dict:
    """The pod spec of a Deployment/StatefulSet manifest."""
    return doc["spec"]["template"]["spec"]


def all_containers(doc: dict) -> list[dict]:
    spec = pod_spec_of(doc)
    return list(spec.get("initContainers", [])) + list(spec.get("containers", []))
