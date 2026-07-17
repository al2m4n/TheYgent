"""docker-compose contract guards — the containerized way of running the stack."""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest
from contract_utils import (
    COMPOSE_FILE,
    COMPOSE_HOST_INFERENCE,
    EXTERNAL_ENV_ALLOWLIST,
    REPO,
    env_names_used,
)

EXPECTED_SERVICES = {
    "postgres",
    "migrate",
    "inference-plane",
    "control-plane",
    "worker",
    "interface",
}


def test_expected_services(compose: dict) -> None:
    assert set(compose["services"]) == EXPECTED_SERVICES


def test_build_contexts_and_dockerfiles_exist(compose: dict) -> None:
    for name, svc in compose["services"].items():
        build = svc.get("build")
        if not build:
            continue
        context = REPO / build["context"]
        assert context.is_dir(), f"{name}: build context {build['context']!r} missing"
        assert (context / build["dockerfile"]).is_file(), (
            f"{name}: dockerfile {build['dockerfile']!r} missing"
        )


def test_postgres_has_pgvector_and_pg18_volume_layout(compose: dict) -> None:
    pg = compose["services"]["postgres"]
    assert "pgvector" in pg["image"], "the RAG migrations need pgvector in the image"
    mounts = [v.split(":")[1] for v in pg["volumes"]]
    assert "/var/lib/postgresql" in mounts, (
        "postgres 18 images keep the data dir under /var/lib/postgresql — mounting the "
        "old …/data path silently loses data on container replacement"
    )


def test_migrate_runs_alembic_and_gates_the_app_services(compose: dict) -> None:
    services = compose["services"]
    assert services["migrate"]["command"][0] == "alembic"
    for dependent in ("control-plane", "worker"):
        cond = services[dependent]["depends_on"]["migrate"]["condition"]
        assert cond == "service_completed_successfully"


def test_long_running_services_restart(compose: dict) -> None:
    """No supervisor exists in this topology (unlike k8s) — a crash/daemon restart must not
    leave the stack down. The one-shot migrate must NOT restart."""
    for name, svc in compose["services"].items():
        if name == "migrate":
            assert svc.get("restart", "no") == "no"
        else:
            assert svc.get("restart") == "unless-stopped", f"{name}: needs restart policy"


def test_ports_mirror_the_make_stack(compose: dict) -> None:
    """Same host ports as `make up`, so the SPA's baked-in localhost URLs and the CORS
    defaults hold in both ways of running the stack."""
    services = compose["services"]
    assert "8080:8080" in services["control-plane"]["ports"]
    assert "8081:8081" in services["inference-plane"]["ports"]
    assert "5174:80" in services["interface"]["ports"]
    # The compose Postgres publishes a host port for debugging; an exact-match assertion so
    # remapping it (e.g. onto :5432 to also back the bare-metal stack) is a deliberate,
    # test-acknowledged change rather than silent drift.
    pg_ports = services["postgres"]["ports"]
    assert pg_ports in (["5433:5432"], ["127.0.0.1:5432:5432"]), pg_ports


def test_control_plane_wiring(compose: dict) -> None:
    build_args = compose["services"]["control-plane"]["build"].get("args", {})
    assert "WITH_JS_RENDER" in build_args, (
        "the JS-render bake must be reachable via `WITH_JS_RENDER=1 docker compose build`"
    )
    env = compose["services"]["control-plane"]["environment"]
    assert env["DATABASE_URL"].startswith("postgresql+asyncpg://")
    assert "@postgres:5432/" in env["DATABASE_URL"]
    assert env["THEYGENT_INFERENCE_PLANE_URL"] == "http://inference-plane:8081/v1", (
        "the data-plane root must target the inference-plane service and include /v1"
    )


def test_worker_is_profiled_and_wired_like_the_control_plane(compose: dict) -> None:
    worker = compose["services"]["worker"]
    assert "worker" in worker.get("profiles", []), (
        "the worker stays opt-in (profile) to mirror the make stack's default"
    )
    assert (
        worker["environment"]["DATABASE_URL"]
        == (compose["services"]["control-plane"]["environment"]["DATABASE_URL"])
    )


def test_artifact_volume_is_shared_between_api_and_worker(compose: dict) -> None:
    """Durable runs on the worker write artifacts the API must serve — same volume."""

    def data_mounts(name: str) -> set[str]:
        return {v for v in compose["services"][name].get("volumes", []) if ":/data" in v}

    assert data_mounts("control-plane") == data_mounts("worker") != set()


def test_every_env_name_is_one_the_code_reads(compose: dict, known_env_names: set[str]) -> None:
    """A THEYGENT_* var set in compose but read by nothing is a rename gone unnoticed."""
    for name, svc in compose["services"].items():
        for env_name in env_names_used(svc.get("environment")):
            if env_name in EXTERNAL_ENV_ALLOWLIST:
                continue
            assert env_name in known_env_names, (
                f"{name}: env {env_name!r} is not read anywhere in apps/ or packages/"
            )


def test_host_inference_overlay(compose_host_inference: dict) -> None:
    services = compose_host_inference["services"]
    assert services["inference-plane"].get("profiles"), (
        "the overlay must park the containerized inference plane under a profile"
    )
    for name in ("control-plane", "worker"):
        env = services[name]["environment"]
        assert env["THEYGENT_INFERENCE_PLANE_URL"] == "http://host.docker.internal:8081/v1"
        assert "host.docker.internal:host-gateway" in services[name]["extra_hosts"]


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
@pytest.mark.parametrize(
    "files",
    [
        [COMPOSE_FILE],
        [COMPOSE_FILE, COMPOSE_HOST_INFERENCE],
    ],
    ids=["default", "host-inference"],
)
def test_compose_config_validates(files: list) -> None:
    cmd = ["docker", "compose"]
    for f in files:
        cmd += ["-f", str(f)]
    cmd += ["--profile", "worker", "config", "--quiet"]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
    assert proc.returncode == 0
