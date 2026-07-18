"""Kubernetes manifest contract guards (deploy/k8s)."""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest
import yaml
from contract_utils import (
    EXTERNAL_ENV_ALLOWLIST,
    K8S_DIR,
    REPO,
    all_containers,
    env_names_used,
    k8s_manifest_paths,
    pod_spec_of,
)

THEYGENT_IMAGES = {
    "theygent/control-plane:dev",
    "theygent/inference-plane:dev",
    "theygent/worker:dev",
    "theygent/interface:dev",
}


def _by_kind(docs: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def test_kustomization_lists_every_manifest() -> None:
    kustomization = yaml.safe_load((K8S_DIR / "kustomization.yaml").read_text())
    listed = set(kustomization["resources"])
    # Both yaml spellings — a .yml manifest must not silently escape the suite.
    present = {p.name for p in k8s_manifest_paths()}
    assert listed == present, (
        f"kustomization.yaml and deploy/k8s drifted apart — listed-not-present: "
        f"{listed - present}, present-not-listed: {present - listed}"
    )


def test_everything_lives_in_the_theygent_namespace(k8s_docs: list[dict]) -> None:
    for doc in k8s_docs:
        if doc["kind"] == "Namespace":
            assert doc["metadata"]["name"] == "theygent"
        else:
            assert doc["metadata"].get("namespace") == "theygent", (
                f"{doc['kind']}/{doc['metadata']['name']} missing namespace: theygent"
            )


def test_expected_workloads(k8s_docs: list[dict]) -> None:
    assert set(_by_kind(k8s_docs, "Deployment")) == {
        "control-plane",
        "inference-plane",
        "worker",
        "interface",
    }
    assert set(_by_kind(k8s_docs, "StatefulSet")) == {"postgres"}


def test_images_are_the_compose_built_tags(k8s_docs: list[dict], compose: dict) -> None:
    """k8s must consume exactly the images `docker compose build` produces."""
    compose_images = {svc["image"] for svc in compose["services"].values() if svc.get("build")}
    assert compose_images == THEYGENT_IMAGES
    for kind in ("Deployment", "StatefulSet"):
        for name, doc in _by_kind(k8s_docs, kind).items():
            for c in all_containers(doc):
                image = c["image"]
                if image.startswith("theygent/"):
                    assert image in THEYGENT_IMAGES, f"{name}: unknown image {image}"
                    assert c.get("imagePullPolicy") == "IfNotPresent", (
                        f"{name}/{c['name']}: locally-loaded images need IfNotPresent"
                    )
                else:
                    assert "pgvector" in image, f"{name}: unexpected external image {image}"


def test_workloads_declare_resources(k8s_docs: list[dict]) -> None:
    """initContainers included: an unbounded migrate/wait container still counts against
    the pod's effective request and can wedge scheduling on a tight node."""
    for kind in ("Deployment", "StatefulSet"):
        for name, doc in _by_kind(k8s_docs, kind).items():
            for c in all_containers(doc):
                res = c.get("resources", {})
                assert res.get("requests") and res.get("limits"), (
                    f"{name}/{c['name']}: requests+limits required (HPA needs requests)"
                )


def test_http_services_have_probes(k8s_docs: list[dict]) -> None:
    # The worker is a queue consumer with no HTTP surface — exempt by design.
    needs_probes = {"control-plane", "inference-plane", "interface", "postgres"}
    for kind in ("Deployment", "StatefulSet"):
        for name, doc in _by_kind(k8s_docs, kind).items():
            if name not in needs_probes:
                continue
            for c in pod_spec_of(doc)["containers"]:
                assert c.get("livenessProbe") and c.get("readinessProbe"), (
                    f"{name}/{c['name']}: liveness+readiness probes required"
                )


def test_services_select_existing_workloads(k8s_docs: list[dict]) -> None:
    workload_labels = []
    for kind in ("Deployment", "StatefulSet"):
        for doc in _by_kind(k8s_docs, kind).values():
            workload_labels.append(doc["spec"]["template"]["metadata"]["labels"])
    for name, svc in _by_kind(k8s_docs, "Service").items():
        selector = svc["spec"]["selector"]
        assert any(
            all(labels.get(k) == v for k, v in selector.items()) for labels in workload_labels
        ), f"Service {name}: selector {selector} matches no workload pod template"


def test_hpas_target_existing_deployments(k8s_docs: list[dict]) -> None:
    deployments = set(_by_kind(k8s_docs, "Deployment"))
    hpas = _by_kind(k8s_docs, "HorizontalPodAutoscaler")
    # The scalable planes stay scalable — removing an HPA must be an explicit change.
    assert {"control-plane", "worker"} <= set(hpas)
    for name, hpa in hpas.items():
        ref = hpa["spec"]["scaleTargetRef"]
        assert ref["kind"] == "Deployment" and ref["name"] in deployments, (
            f"HPA {name}: targets missing deployment {ref['name']!r}"
        )
        assert hpa["spec"]["minReplicas"] >= 1


def test_control_plane_migrates_before_starting(k8s_docs: list[dict]) -> None:
    cp = _by_kind(k8s_docs, "Deployment")["control-plane"]
    inits = pod_spec_of(cp).get("initContainers", [])
    assert any(
        c.get("command", [None])[0] == "alembic" and "upgrade" in c.get("command", [])
        for c in inits
    ), "control-plane must run alembic migrations in an initContainer"


def test_durable_mode_is_on_for_horizontal_scale(k8s_docs: list[dict]) -> None:
    """The in-process cron dispatcher is single-instance by design; scaling API replicas
    is only safe with schedules on DBOS (THEYGENT_DURABLE=1). If this flips to 0, drop the
    control-plane HPA in the same change."""
    config = _by_kind(k8s_docs, "ConfigMap")["theygent-config"]
    assert config["data"]["THEYGENT_DURABLE"] == "1"
    # …and the value must actually REACH the pods: the ConfigMap alone is inert unless the
    # control-plane (and its peers) envFrom it. Guards the wiring, not just the key.
    for name in ("control-plane", "worker", "inference-plane"):
        doc = _by_kind(k8s_docs, "Deployment")[name]
        env_from = [
            ref.get("configMapRef", {}).get("name")
            for c in pod_spec_of(doc)["containers"]
            for ref in c.get("envFrom", [])
        ]
        assert "theygent-config" in env_from, (
            f"{name}: must envFrom the theygent-config ConfigMap or its keys never apply"
        )


def test_postgres_uses_pg18_volume_layout(k8s_docs: list[dict]) -> None:
    pg = _by_kind(k8s_docs, "StatefulSet")["postgres"]
    mounts = [m["mountPath"] for c in pod_spec_of(pg)["containers"] for m in c["volumeMounts"]]
    assert "/var/lib/postgresql" in mounts


def test_pvc_writers_pin_fsgroup_to_the_image_uid(k8s_docs: list[dict]) -> None:
    """Fresh PVCs on block-storage classes mount root-owned; the images run as uid 10001,
    so every PVC-mounting pod must set fsGroup/runAsUser to match or it cannot write /data
    (minikube's permissive hostpath provisioner masks this — real clusters do not).
    The 10001 constant is pinned in the three Python Dockerfiles; keep them in lockstep."""
    for name, doc in _by_kind(k8s_docs, "Deployment").items():
        spec = pod_spec_of(doc)
        mounts_pvc = any(v.get("persistentVolumeClaim") for v in spec.get("volumes", []))
        if not mounts_pvc:
            continue
        sc = spec.get("securityContext", {})
        assert sc.get("fsGroup") == 10001 and sc.get("runAsUser") == 10001, (
            f"{name}: mounts a PVC but securityContext does not pin fsGroup/runAsUser=10001"
        )


def test_pvc_claims_referenced_by_pods_exist(k8s_docs: list[dict]) -> None:
    claims = set(_by_kind(k8s_docs, "PersistentVolumeClaim"))
    for kind in ("Deployment",):
        for name, doc in _by_kind(k8s_docs, kind).items():
            for vol in pod_spec_of(doc).get("volumes", []):
                pvc = vol.get("persistentVolumeClaim")
                if pvc:
                    assert pvc["claimName"] in claims, (
                        f"{name}: volume references missing PVC {pvc['claimName']!r}"
                    )


def test_every_env_name_is_one_the_code_reads(
    k8s_docs: list[dict], known_env_names: set[str]
) -> None:
    for kind in ("Deployment", "StatefulSet"):
        for name, doc in _by_kind(k8s_docs, kind).items():
            for c in all_containers(doc):
                for e in c.get("env", []):
                    env_name = e["name"]
                    if env_name in EXTERNAL_ENV_ALLOWLIST:
                        continue
                    assert env_name in known_env_names, (
                        f"{name}/{c['name']}: env {env_name!r} is read nowhere in the code"
                    )
    for name, cm in _by_kind(k8s_docs, "ConfigMap").items():
        for env_name in env_names_used(cm.get("data", {})):
            if env_name not in EXTERNAL_ENV_ALLOWLIST:
                assert env_name in known_env_names, (
                    f"ConfigMap {name}: {env_name!r} is read nowhere in the code"
                )


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not available")
def test_kustomize_renders() -> None:
    proc = subprocess.run(
        ["kubectl", "kustomize", str(K8S_DIR)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
    assert proc.returncode == 0
