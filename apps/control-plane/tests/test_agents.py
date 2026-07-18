"""Fast suite for the agent registry — save → version → invoke-by-reference, over a
real (fake-model) inference plane and a REAL ephemeral Postgres applied through REAL Alembic
migrations. No SQLite, no create_all.

The agent registry makes an agent a saved, named, immutable, content-addressed thing you invoke by
reference instead of re-pasting the whole IR every run. So these prove: it persists + survives a
restart; the registry's hash IS the walker's hash; the hash is view-stripped + key-order
stable; versions are immutable; an invoke-by-reference run is behaviourally identical to
/graphs/runs and the Run row carries the agent's coordinate; pinned invokes run exactly that
content; and session memory is unchanged through /agents/*.
"""

from __future__ import annotations

import copy
import json

from _fake_inference import FULL_MESSAGE, FakeInference
from _ir import trivial_ir
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from theygent_ir import content_hash, parse_document
from ulid import ULID


def _agent_ir(version: str = "0.1.0", prompt: str = "$in") -> dict:
    """The trivial IR, re-versioned + re-prompted so a test can mint distinct versions/content."""
    doc = trivial_ir()
    doc["version"] = version
    doc["nodes"][1]["config"]["messages"][0]["content"] = prompt
    return doc


def _second_app(fake: FakeInference, pg_url: str) -> TestClient:
    """A fresh app/TestClient on the SAME Postgres — simulates a control-plane restart (new
    sessionmaker, same durable state) without tearing the DB down."""
    return TestClient(create_app(inference_base_url=fake.v1_url, database_url=pg_url))


def _parse_sse(text: str) -> list[tuple[str | None, str]]:
    events: list[tuple[str | None, str]] = []
    event: str | None = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            events.append((event, line[len("data:") :].strip()))
            event = None
    return events


# ── create + read ────────────────────────────────────────────────────────────


def test_create_agent_returns_detail_with_first_version(client: TestClient) -> None:
    ir = _agent_ir()
    resp = client.post("/agents", json={"ir": ir, "name": "my-triage"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == ir["id"]
    assert body["name"] == "my-triage"
    assert len(body["versions"]) == 1
    v = body["versions"][0]
    assert v["version"] == "0.1.0"
    assert v["content_hash"].startswith("sha256:")
    assert v["seq"] == 1


def test_create_agent_name_falls_back_to_ir_name(client: TestClient) -> None:
    resp = client.post("/agents", json={"ir": _agent_ir()})
    assert resp.status_code == 201
    assert resp.json()["name"] == "trivial-llm"  # the IR's own name


def test_create_agent_invalid_ir_rejected(client: TestClient) -> None:
    bad = _agent_ir()
    bad["nodes"][1]["kind"] = "boundary"  # llm must be activity
    resp = client.post("/agents", json={"ir": bad})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_ir"


def test_create_agent_duplicate_id_conflicts(client: TestClient) -> None:
    client.post("/agents", json={"ir": _agent_ir()})
    resp = client.post("/agents", json={"ir": _agent_ir()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "agent_exists"


def test_list_agents_newest_first(client: TestClient) -> None:
    for i in range(3):
        ir = _agent_ir()
        ir["id"] = f"agt_list_{i}"
        client.post("/agents", json={"ir": ir, "name": f"agent-{i}"})
    rows = client.get("/agents").json()["agents"]
    assert [r["id"] for r in rows] == ["agt_list_2", "agt_list_1", "agt_list_0"]
    assert rows[0]["latest_version"] == "0.1.0"
    assert rows[0]["version_count"] == 1
    assert rows[0]["latest_content_hash"].startswith("sha256:")


def test_get_unknown_agent_404(client: TestClient) -> None:
    resp = client.get("/agents/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "agent_not_found"


def test_get_stored_version_returns_ir(client: TestClient) -> None:
    ir = _agent_ir()
    client.post("/agents", json={"ir": ir})
    resp = client.get(f"/agents/{ir['id']}/versions/0.1.0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == "0.1.0"
    assert body["content_hash"].startswith("sha256:")
    # The stored IR is the canonical, view-stripped document, self-describing (hash set).
    assert body["ir"]["id"] == ir["id"]
    assert body["ir"]["contentHash"] == body["content_hash"]
    assert "view" not in body["ir"]  # view is stripped from the hashed document


def test_get_unknown_version_404(client: TestClient) -> None:
    ir = _agent_ir()
    client.post("/agents", json={"ir": ir})
    resp = client.get(f"/agents/{ir['id']}/versions/9.9.9")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "agent_version_not_found"


# ── persistence (survives restart) ─────────────────────────────────────────────


def test_persist_and_survive_restart(fake_inference: FakeInference, pg_url: str) -> None:
    # Create an agent + version through one app; a SECOND app on the same DB still lists it with the
    # same content + hash — the registry promise (an agent is a durable object, not a paste-in).
    ir = _agent_ir()
    with _second_app(fake_inference, pg_url) as a:
        created = a.post("/agents", json={"ir": ir, "name": "durable"}).json()
        stored_hash = created["versions"][0]["content_hash"]
        stored_ir = a.get(f"/agents/{ir['id']}/versions/0.1.0").json()["ir"]

    with _second_app(fake_inference, pg_url) as b:  # "restart": fresh app, same Postgres
        listed = b.get("/agents").json()["agents"]
        assert [r["id"] for r in listed] == [ir["id"]]
        assert listed[0]["latest_content_hash"] == stored_hash
        again = b.get(f"/agents/{ir['id']}/versions/0.1.0").json()
        assert again["content_hash"] == stored_hash
        assert again["ir"] == stored_ir  # byte-identical content across the restart


# ── hash agreement + determinism ─────────────────────────────────────────────────


def test_registry_hash_equals_the_walker_function(client: TestClient) -> None:
    # The registry's stored content_hash == the IR package's content_hash for
    # the same IR — asserted against the ONE function directly, not a re-implementation. If these
    # ever disagree it's a bug, not config (the walker and registry must agree byte-for-byte).
    ir = _agent_ir()
    created = client.post("/agents", json={"ir": ir}).json()
    assert created["versions"][0]["content_hash"] == content_hash(parse_document(ir))


def test_stored_ir_rehash_is_a_fixpoint(client: TestClient) -> None:
    # The pinning soundness guarantee: the registry stores the CANONICAL,
    # default-filled, view-stripped IR, so reloading the stored ir and re-hashing it
    # reproduces the stored content_hash byte-for-byte. If this drifted, every hash-pinned deploy
    # (which re-resolves the stored ir on each fire) would silently fail to reproduce its pin.
    ir = _agent_ir()
    created = client.post("/agents", json={"ir": ir}).json()
    stored_hash = created["versions"][0]["content_hash"]
    stored = client.get(f"/agents/{ir['id']}/versions/0.1.0").json()
    # store -> reload -> re-hash == the stored hash (the fixpoint). Uses the ONE hash function.
    assert content_hash(parse_document(stored["ir"])) == stored_hash
    # And the stored ir is already the canonical form (no author-raw drift): re-publishing the
    # reloaded stored ir under the same (id, version) re-canonicalizes to the SAME hash, so it's an
    # idempotent 200 — not a VersionConflict. If the stored form weren't canonical, the re-hash
    # would differ and this would 409.
    resp = client.post(f"/agents/{ir['id']}/versions", json={"ir": stored["ir"]})
    assert resp.status_code == 200
    assert resp.json()["versions"][0]["content_hash"] == stored_hash


def test_hash_ignores_key_order_and_view_idempotent_republish(client: TestClient) -> None:
    # The same IR with a reordered/spaced shape AND an added view block is the SAME version:
    # re-publishing it under the same coordinate is idempotent (200), never a conflict, never a new
    # version, same hash. Dragging a node must not mint a version.
    ir = _agent_ir()
    h1 = client.post("/agents", json={"ir": ir}).json()["versions"][0]["content_hash"]

    dragged = copy.deepcopy(ir)
    dragged["view"] = {
        "nodes": {"n_llm": {"position": {"x": 999, "y": 17}}},
        "viewport": {"zoom": 2},
    }
    resp = client.post(f"/agents/{ir['id']}/versions", json={"ir": dragged})
    assert resp.status_code == 200  # idempotent re-publish of identical content
    detail = resp.json()
    assert len(detail["versions"]) == 1  # still ONE version
    assert detail["versions"][0]["content_hash"] == h1  # unchanged


def test_real_content_change_is_a_new_version_with_new_hash(client: TestClient) -> None:
    ir = _agent_ir(version="0.1.0", prompt="$in")
    h1 = client.post("/agents", json={"ir": ir}).json()["versions"][0]["content_hash"]
    changed = _agent_ir(version="0.2.0", prompt="totally different: $in")
    resp = client.post(f"/agents/{ir['id']}/versions", json={"ir": changed})
    assert resp.status_code == 201
    detail = resp.json()
    # Two versions, newest first (ordered by seq, not time), different hashes.
    assert [v["version"] for v in detail["versions"]] == ["0.2.0", "0.1.0"]
    assert [v["seq"] for v in detail["versions"]] == [2, 1]
    assert detail["versions"][0]["content_hash"] != h1


# ── immutability guard ───────────────────────────────────────────────────────────


def test_immutability_guard_rejects_different_content_under_existing_version(
    client: TestClient,
) -> None:
    ir = _agent_ir(version="0.1.0", prompt="$in")
    client.post("/agents", json={"ir": ir})
    drift = _agent_ir(version="0.1.0", prompt="SILENTLY DIFFERENT: $in")  # same coord, new content
    resp = client.post(f"/agents/{ir['id']}/versions", json={"ir": drift})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "version_conflict"


def test_add_version_id_mismatch_rejected(client: TestClient) -> None:
    ir = _agent_ir()
    client.post("/agents", json={"ir": ir})
    other = _agent_ir(version="0.2.0")
    other["id"] = "agt_someone_else"  # IR carries a different id than the URL
    resp = client.post(f"/agents/{ir['id']}/versions", json={"ir": other})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "agent_id_mismatch"


def test_add_version_to_unknown_agent_404(client: TestClient) -> None:
    ir = _agent_ir()
    resp = client.post(f"/agents/{ir['id']}/versions", json={"ir": ir})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "agent_not_found"


# ── invoke-by-reference ──────────────────────────────────────────────────────────


def test_invoke_by_reference_same_sse_shape_and_run_coordinate(
    client: TestClient, fake_inference: FakeInference
) -> None:
    ir = _agent_ir()
    client.post("/agents", json={"ir": ir})
    with client.stream(
        "POST", f"/agents/{ir['id']}/runs", json={"input": "name three EU capitals", "stream": True}
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse("".join(resp.iter_text()))

    # Identical SSE shape to /graphs/runs: run(streaming) → deltas → run(completed) → [DONE].
    assert events[0][0] == "run"
    opening = json.loads(events[0][1])
    run_id = opening["runId"]
    assert opening["status"] == "streaming"
    deltas = [json.loads(d)["delta"] for ev, d in events if ev == "delta"]
    assert "".join(deltas) == FULL_MESSAGE
    assert json.loads(events[-2][1])["status"] == "completed"
    assert events[-1][1] == "[DONE]"

    # The IR was sourced from the registry, never pasted — and the Run row carries the AGENT's
    # coordinate (graph_id == agent id, graph_version, contentHash), exactly as /graphs/runs does.
    got = client.get(f"/runs/{run_id}").json()
    assert got["status"] == "completed"
    assert got["graph_id"] == ir["id"]
    assert got["graph_version"] == "0.1.0"
    assert got["content_hash"] == content_hash(parse_document(ir))


def test_invoke_by_reference_non_stream(client: TestClient, fake_inference: FakeInference) -> None:
    ir = _agent_ir()
    client.post("/agents", json={"ir": ir})
    resp = client.post(f"/agents/{ir['id']}/runs", json={"input": "hi", "stream": False})
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert resp.json()["output"] == FULL_MESSAGE
    # $in was substituted from the registry-sourced IR (the input reached the wire).
    assert fake_inference.captured["messages"] == [{"role": "user", "content": "hi"}]


def test_invoke_unknown_agent_404(client: TestClient) -> None:
    resp = client.post("/agents/nope/runs", json={"input": "x", "stream": False})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "agent_not_found"


def test_invoke_unknown_pinned_version_404(client: TestClient) -> None:
    ir = _agent_ir()
    client.post("/agents", json={"ir": ir})
    resp = client.post(
        f"/agents/{ir['id']}/runs", json={"input": "x", "version": "9.9.9", "stream": False}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "agent_version_not_found"


def test_invoke_unknown_pinned_content_hash_404(client: TestClient) -> None:
    # A dangling hash pin (a typo'd / stale contentHash) is a clean 404 — NOT a 500: triggers pin
    # and re-resolve, so a stale pin must fail honestly, never hang/crash. Distinct
    # code path from version-pinning (get_version_by_hash), so tested distinctly.
    ir = _agent_ir()
    client.post("/agents", json={"ir": ir})
    resp = client.post(
        f"/agents/{ir['id']}/runs",
        json={"input": "x", "content_hash": "sha256:deadbeef", "stream": False},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "agent_version_not_found"


# ── pinned invoke runs EXACTLY that content ──────────────────────────────────────


def test_pinned_version_runs_that_content_even_when_newer_exists(
    client: TestClient, fake_inference: FakeInference
) -> None:
    ir = _agent_ir(version="0.1.0", prompt="VERSION_A: $in")
    client.post("/agents", json={"ir": ir})
    newer = _agent_ir(version="0.2.0", prompt="VERSION_B: $in")
    client.post(f"/agents/{ir['id']}/versions", json={"ir": newer})

    # Default invoke → latest (0.2.0 → VERSION_B prompt).
    client.post(f"/agents/{ir['id']}/runs", json={"input": "q", "stream": False})
    assert fake_inference.captured["messages"] == [{"role": "user", "content": "VERSION_B: q"}]

    # Pinned to 0.1.0 → runs VERSION_A's content even though 0.2.0 exists.
    r = client.post(
        f"/agents/{ir['id']}/runs", json={"input": "q", "version": "0.1.0", "stream": False}
    )
    assert r.status_code == 200
    assert fake_inference.captured["messages"] == [{"role": "user", "content": "VERSION_A: q"}]
    assert client.get(f"/runs/{r.json()['runId']}").json()["graph_version"] == "0.1.0"


def test_pinned_content_hash_runs_that_content(
    client: TestClient, fake_inference: FakeInference
) -> None:
    ir_a = _agent_ir(version="0.1.0", prompt="VERSION_A: $in")
    client.post("/agents", json={"ir": ir_a})
    hash_a = content_hash(parse_document(ir_a))
    client.post(
        f"/agents/{ir_a['id']}/versions",
        json={"ir": _agent_ir(version="0.2.0", prompt="VERSION_B: $in")},
    )
    r = client.post(
        f"/agents/{ir_a['id']}/runs",
        json={"input": "q", "content_hash": hash_a, "stream": False},
    )
    assert r.status_code == 200
    assert fake_inference.captured["messages"] == [{"role": "user", "content": "VERSION_A: q"}]
    assert client.get(f"/runs/{r.json()['runId']}").json()["content_hash"] == hash_a


def test_latest_is_highest_seq_not_highest_semver(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # "latest" = most recently PUBLISHED (highest seq), NOT highest semver (order by the
    # explicit seq column, never by a parsed version/timestamp). So a backport published after a
    # higher semver becomes latest. This matters because the cockpit's default invoke uses
    # latest. Trigger-based deploys mandate pinning, so this only governs the convenience default.
    ir = _agent_ir(version="0.3.0", prompt="HIGH_SEMVER: $in")
    client.post("/agents", json={"ir": ir})
    backport = _agent_ir(version="0.2.5", prompt="BACKPORT: $in")  # published AFTER 0.3.0 → seq 2
    detail = client.post(f"/agents/{ir['id']}/versions", json={"ir": backport}).json()
    # versions are seq-ordered newest-first: the backport (seq 2) leads, not the higher semver.
    assert [v["version"] for v in detail["versions"]] == ["0.2.5", "0.3.0"]

    # Default (unpinned) invoke runs the highest-seq version — the backport — not 0.3.0.
    r = client.post(f"/agents/{ir['id']}/runs", json={"input": "q", "stream": False})
    assert fake_inference.captured["messages"] == [{"role": "user", "content": "BACKPORT: q"}]
    assert client.get(f"/runs/{r.json()['runId']}").json()["graph_version"] == "0.2.5"


def test_exact_identical_republish_is_idempotent_200(client: TestClient) -> None:
    # A CI/redeploy resubmitting an UNCHANGED agent (byte-identical id+version+content) is an
    # idempotent 200, not a 409 — so unattended redeploys don't error on a no-op.
    ir = _agent_ir()
    client.post("/agents", json={"ir": ir})
    resp = client.post(f"/agents/{ir['id']}/versions", json={"ir": copy.deepcopy(ir)})
    assert resp.status_code == 200
    assert len(resp.json()["versions"]) == 1


# ── session memory unchanged through /agents/* ───────────────────────────────────


def test_session_memory_replays_through_agent_invoke(
    client: TestClient, fake_inference: FakeInference
) -> None:
    ir = _agent_ir()
    client.post("/agents", json={"ir": ir})
    session_id = str(ULID())

    def ask(text: str) -> dict:
        r = client.post(
            f"/agents/{ir['id']}/runs",
            json={"input": text, "session_id": session_id, "stream": False},
        )
        assert r.status_code == 200, r.text
        return r.json()

    assert ask("first")["status"] == "completed"
    assert ask("second")["status"] == "completed"
    # Turn 2 replays turn 1 (session memory), verified through the /agents/* path.
    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": FULL_MESSAGE},
        {"role": "user", "content": "second"},
    ]


def test_existing_run_surfaces_unchanged(client: TestClient) -> None:
    # The agent registry is purely additive: /graphs/runs still works and is untouched
    # by the registry.
    resp = client.post("/graphs/runs", json={"ir": trivial_ir(), "input": "x", "stream": False})
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
