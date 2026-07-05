"""Bench store fast suite — real testcontainers Postgres, real Alembic schema.

The load-bearing guards:
* **Pinning:** a model ``bench_run`` records logical_id+model_ref+binding + a params digest;
  two runs differing only in ``temperature`` are TWO distinct results. An agent run records
  agent_id+version+contentHash.
* **Metrics-only default:** with capture off, no raw output is persisted — only metrics
  + digests. Capture-on stores a LOCAL reference (``capture_ref``), never a blob.
* **Compare:** GET /bench/compare aligns two results' metrics + an outputs-match flag.
* **Preset round-trip:** save / list / delete; a preset stores literal values + a modality,
  never a run/agent link.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

# ── pinning ──────────────────────────────────────────────────────────────────


def test_model_run_pins_logical_model_binding_and_params(client: TestClient) -> None:
    r = client.post(
        "/bench/runs",
        json={
            "target_kind": "model",
            "modality": "chat",
            "logical_id": "triage-fast",
            "model_ref": "mlx-community/Qwen2.5-7B-Instruct-4bit",
            "binding": "mlx",
            "params": {"temperature": 0.2, "maxTokens": 256},
            "metrics": {"ttftMs": 120, "tokensPerSec": 48.5, "totalMs": 900},
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["logical_id"] == "triage-fast"
    assert body["binding"] == "mlx"
    assert body["model_ref"].endswith("Qwen2.5-7B-Instruct-4bit")
    assert body["params_digest"].startswith("sha256:")


def test_runs_differing_only_in_temperature_are_distinct(client: TestClient) -> None:
    def record(temp: float) -> dict:
        return client.post(
            "/bench/runs",
            json={
                "target_kind": "model",
                "modality": "chat",
                "logical_id": "triage-fast",
                "binding": "mlx",
                "params": {"temperature": temp},
                "metrics": {"totalMs": 100},
            },
        ).json()

    a = record(0.2)
    b = record(0.9)
    # Different params → different digests → two distinct benchmarks (the tuning point).
    assert a["params_digest"] != b["params_digest"]
    assert a["id"] != b["id"]


def test_identical_params_get_identical_digest(client: TestClient) -> None:
    def digest(params: dict) -> str:
        return client.post(
            "/bench/runs",
            json={
                "target_kind": "model",
                "modality": "chat",
                "logical_id": "m",
                "binding": "mlx",
                "params": params,
                "metrics": {},
            },
        ).json()["params_digest"]

    # Canonical (sorted-key) digest — key order does not change identity.
    assert digest({"temperature": 0.2, "topP": 0.9}) == digest({"topP": 0.9, "temperature": 0.2})


def test_agent_run_pins_content_hash(client: TestClient) -> None:
    r = client.post(
        "/bench/runs",
        json={
            "target_kind": "agent",
            "modality": "agent",
            "agent_id": "summarizer",
            "version": "1.0.0",
            "content_hash": "sha256:abc123",
            "metrics": {"totalMs": 1500, "cost": 0.0},
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["agent_id"] == "summarizer"
    assert body["content_hash"] == "sha256:abc123"
    # An agent run is pinned by content hash, not a params digest (params live in the hashed IR).
    assert body["params_digest"] is None


# ── metrics-only default vs opt-in local capture ─────────────────────────────


def test_capture_off_persists_no_raw_output(client: TestClient) -> None:
    r = client.post(
        "/bench/runs",
        json={
            "target_kind": "model",
            "modality": "chat",
            "logical_id": "m",
            "binding": "mlx",
            "params": {},
            "metrics": {"totalMs": 100},
            "output": "the model said something private",
            # capture omitted → defaults to False
        },
    )
    body = r.json()
    # The digest is stored (cheap content identity for the diff) but NO raw output / reference.
    assert body["output_digest"].startswith("sha256:")
    assert body["capture_ref"] is None


def test_capture_on_stores_a_local_reference_not_a_blob(client: TestClient) -> None:
    r = client.post(
        "/bench/runs",
        json={
            "target_kind": "model",
            "modality": "chat",
            "logical_id": "m",
            "binding": "mlx",
            "params": {},
            "metrics": {"totalMs": 100},
            "output": "captured locally",
            "capture": True,
            "run_id": "run-xyz",
        },
    )
    body = r.json()
    ref = body["capture_ref"]
    assert ref is not None and ref.startswith("local://")
    # The reference is a pointer in the user's trust domain — never the raw bytes in this table.
    assert "captured locally" not in ref


# ── compare ──────────────────────────────────────────────────────────────────


def test_compare_two_runs_aligns_metrics_and_diffs_outputs(client: TestClient) -> None:
    # The binding-swap headline: same input, two bindings, side-by-side metrics + output match.
    def record(binding: str, total: int, out: str) -> str:
        return client.post(
            "/bench/runs",
            json={
                "target_kind": "model",
                "modality": "chat",
                "logical_id": "triage",
                "binding": binding,
                "params": {"temperature": 0.0},
                "metrics": {"totalMs": total, "cost": 0.0 if binding == "mlx" else 0.002},
                "output": out,
            },
        ).json()["id"]

    a = record("mlx", 800, "blue")
    b = record("openai-compatible", 350, "blue")
    r = client.get("/bench/compare", params={"a": a, "b": b})
    assert r.status_code == 200
    body = r.json()
    assert body["a"]["binding"] == "mlx"
    assert body["b"]["binding"] == "openai-compatible"
    assert body["metric_deltas"]["totalMs"] == 350 - 800
    assert body["outputs_match"] is True


def test_compare_unknown_run_is_404(client: TestClient) -> None:
    one = client.post(
        "/bench/runs",
        json={"target_kind": "model", "modality": "chat", "logical_id": "m", "metrics": {}},
    ).json()["id"]
    r = client.get("/bench/compare", params={"a": one, "b": "nope"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "bench_run_not_found"


# ── suites + cases ───────────────────────────────────────────────────────────


def test_suite_round_trip_with_cases(client: TestClient) -> None:
    create = client.post(
        "/bench/suites",
        json={
            "name": "summarizer regression",
            "target_kind": "agent",
            "agent_id": "summarizer",
            "version": "1.0.0",
            "cases": [
                {"input": "long text A", "expected": "A", "assertion": "contains"},
                {
                    "input": {"q": "x"},
                    "assertion": "json-path-equals",
                    "assertion_config": {"path": "$.ok"},
                },
            ],
        },
    )
    assert create.status_code == 201
    suite_id = create.json()["id"]

    got = client.get(f"/bench/suites/{suite_id}").json()
    assert got["name"] == "summarizer regression"
    assert len(got["cases"]) == 2
    # Order preserved by seq; object inputs round-trip through JSONB.
    assert got["cases"][0]["assertion"] == "contains"
    assert got["cases"][1]["input"] == {"q": "x"}

    listed = client.get("/bench/suites").json()["suites"]
    assert any(s["id"] == suite_id for s in listed)


def test_suite_runs_link_by_suite_and_case(client: TestClient) -> None:
    suite = client.post(
        "/bench/suites",
        json={
            "name": "s",
            "target_kind": "model",
            "logical_id": "m",
            "cases": [{"input": "x", "assertion": "exact"}],
        },
    ).json()
    case_id = client.get(f"/bench/suites/{suite['id']}").json()["cases"][0]["id"]
    client.post(
        "/bench/runs",
        json={
            "target_kind": "model",
            "modality": "chat",
            "logical_id": "m",
            "metrics": {},
            "suite_id": suite["id"],
            "case_id": case_id,
            "assertion": "exact",
            "assertion_passed": True,
        },
    )
    by_suite = client.get("/bench/runs", params={"suite_id": suite["id"]}).json()["runs"]
    assert len(by_suite) == 1
    assert by_suite[0]["assertion_passed"] is True
    by_case = client.get("/bench/runs", params={"case_id": case_id}).json()["runs"]
    assert len(by_case) == 1


# ── presets ──────────────────────────────────────────────────────────────────


def test_preset_round_trip(client: TestClient) -> None:
    create = client.post(
        "/bench/presets",
        json={
            "name": "fast-deterministic",
            "modality": "chat",
            "logical_id": "triage-fast",
            "params": {"temperature": 0.0, "topP": 1.0, "maxTokens": 512},
        },
    )
    assert create.status_code == 201
    preset = create.json()
    # A preset stores literal VALUES + a modality — no run/agent link.
    assert preset["params"]["temperature"] == 0.0
    assert preset["modality"] == "chat"
    assert "run_id" not in preset and "agent_id" not in preset

    listed = client.get("/bench/presets", params={"modality": "chat"}).json()["presets"]
    assert any(p["id"] == preset["id"] for p in listed)
    # filtered out by a non-matching modality
    assert client.get("/bench/presets", params={"modality": "audio.speech"}).json()["presets"] == []

    assert client.delete(f"/bench/presets/{preset['id']}").status_code == 204
    assert client.get("/bench/presets").json()["presets"] == []


def test_delete_unknown_preset_is_404(client: TestClient) -> None:
    r = client.delete("/bench/presets/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "preset_not_found"


# ── listing shape ────────────────────────────────────────────────────────────


def test_bench_runs_list_newest_first(client: TestClient) -> None:
    ids = []
    for i in range(3):
        ids.append(
            client.post(
                "/bench/runs",
                json={
                    "target_kind": "model",
                    "modality": "chat",
                    "logical_id": f"m{i}",
                    "metrics": {},
                },
            ).json()["id"]
        )
    runs = client.get("/bench/runs").json()["runs"]
    assert [r["id"] for r in runs[:3]] == list(reversed(ids))
