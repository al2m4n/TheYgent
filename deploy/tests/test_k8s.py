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
    "theygent/ai-gateway:dev",
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
    assert set(_by_kind(k8s_docs, "StatefulSet")) == {"postgres", "ai-gateway"}


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
    needs_probes = {"control-plane", "inference-plane", "interface", "postgres", "ai-gateway"}
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


def test_hpas_target_existing_workloads(k8s_docs: list[dict]) -> None:
    hpas = _by_kind(k8s_docs, "HorizontalPodAutoscaler")
    # The scalable processes stay scalable — removing an HPA must be an explicit change.
    assert {"control-plane", "worker", "ai-gateway"} <= set(hpas)
    for name, hpa in hpas.items():
        ref = hpa["spec"]["scaleTargetRef"]
        assert ref["kind"] in ("Deployment", "StatefulSet"), f"HPA {name}: {ref['kind']}"
        assert ref["name"] in _by_kind(k8s_docs, ref["kind"]), (
            f"HPA {name}: targets missing {ref['kind']} {ref['name']!r}"
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


def _gateway_env(k8s_docs: list[dict]) -> dict[str, dict]:
    gateway = _by_kind(k8s_docs, "StatefulSet")["ai-gateway"]
    (container,) = pod_spec_of(gateway)["containers"]
    return {e["name"]: e for e in container.get("env", [])}


def test_ai_gateway_secrets_come_from_its_secret(k8s_docs: list[dict]) -> None:
    """The service key is required (the pod waits until an admin has minted it and created
    the Secret); the org key is optional. Neither is ever a literal in a manifest."""
    env = _gateway_env(k8s_docs)
    service = env["THEYGENT_AI_GATEWAY_SERVICE_KEY"]["valueFrom"]["secretKeyRef"]
    assert service["name"] == "theygent-ai-gateway" and not service.get("optional", False)
    org = env["THEYGENT_AI_GATEWAY_ORG_API_KEY"]["valueFrom"]["secretKeyRef"]
    assert org["name"] == "theygent-ai-gateway" and org.get("optional") is True
    for name in ("THEYGENT_AI_GATEWAY_SERVICE_KEY", "THEYGENT_AI_GATEWAY_ORG_API_KEY"):
        assert "value" not in env[name], f"ai-gateway: {name} must come from a Secret"
    assert env["THEYGENT_CONTROL_PLANE_URL"]["value"] == "http://control-plane:8080"
    # The local route follows the shared ConfigMap, like every other service's inference URL.
    ref = env["THEYGENT_INFERENCE_PLANE_URL"]["valueFrom"]["configMapKeyRef"]
    assert (ref["name"], ref["key"]) == ("theygent-config", "THEYGENT_INFERENCE_PLANE_URL")


def test_a_scalable_ai_gateway_never_stores_an_org_key(k8s_docs: list[dict]) -> None:
    """Each replica keeps its own state, so a key set from the AI Gateway page would reach one
    of them; with more than one replica possible, only the Secret's key may apply."""
    hpa = _by_kind(k8s_docs, "HorizontalPodAutoscaler")["ai-gateway"]
    assert hpa["spec"]["scaleTargetRef"]["kind"] == "StatefulSet"
    if hpa["spec"]["maxReplicas"] > 1:
        assert _gateway_env(k8s_docs)["THEYGENT_AI_GATEWAY_STORE_ORG_KEY"]["value"] == "0"


def test_ai_gateway_replicas_keep_their_own_state(k8s_docs: list[dict]) -> None:
    """Per-replica volumes keep the records not yet exported and the last known config/keys
    across restarts; the image's uid must own them; streams get time to finish on a stop."""
    gateway = _by_kind(k8s_docs, "StatefulSet")["ai-gateway"]
    claims = {t["metadata"]["name"] for t in gateway["spec"]["volumeClaimTemplates"]}
    (container,) = pod_spec_of(gateway)["containers"]
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    assert mounts.get("data") == "/data" and "data" in claims
    sc = pod_spec_of(gateway)["securityContext"]
    assert sc.get("fsGroup") == 10001 and sc.get("runAsUser") == 10001
    assert pod_spec_of(gateway)["terminationGracePeriodSeconds"] >= 60
    services = _by_kind(k8s_docs, "Service")
    assert services["ai-gateway"]["spec"]["ports"][0]["port"] == 8082
    assert services[gateway["spec"]["serviceName"]]["spec"].get("clusterIP") == "None"


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
