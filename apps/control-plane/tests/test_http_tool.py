"""M19 §2.3 — the generic http tool with connection-injected auth (the workhorse).

The load-bearing guards (m19.md §4):
* template substitution (``$in.field``) reaches the wire; the connection injects auth SERVER-SIDE
  (verified at the real receiving server) and the auth value is **absent from the IR**;
* a non-2xx is a normal ``ok`` value, a transport failure binds ``err`` (no hang);
* GraphQL is just an http POST with a ``{query, variables}`` body;
* rotating the connection's secret does **not** change a saved agent's ``contentHash`` (§1.1), and
  the saved IR carries no secret.
"""

from __future__ import annotations

from typing import Any

import pytest
from _fake_http import fake_http
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app


@pytest.fixture
def client(fake_inference, pg_url: str) -> TestClient:  # type: ignore[no-untyped-def]
    key = Fernet.generate_key().decode()
    app = create_app(
        inference_base_url=fake_inference.v1_url, database_url=pg_url, secret_key=[key]
    )
    with TestClient(app) as test_client:
        yield test_client


def _http_agent(
    base_url: str,
    connection_id: str,
    *,
    method: str = "GET",
    url_path: str = "/v1/thing",
    headers: dict[str, str] | None = None,
    body: Any = None,
    response_map: str | None = None,
    out_handle: str = "ok",
) -> dict[str, Any]:
    """input -> tool(http) -> output. ``out_handle`` picks which tool handle feeds the output (ok or
    err), so a test can read the tool result or the error message as the run output."""
    config: dict[str, Any] = {
        "tool": "api",
        "method": method,
        "urlTemplate": base_url + url_path,
    }
    if headers is not None:
        config["headers"] = headers
    if body is not None:
        config["bodyTemplate"] = body
    if response_map is not None:
        config["responseMap"] = response_map
    return {
        "schemaVersion": "1.0",
        "id": "agt_http",
        "name": "http-tool-agent",
        "version": "0.1.0",
        "models": {},
        "tools": {"api": {"kind": "http", "connection": connection_id}},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
            },
            {
                "id": "n_tool",
                "type": "tool",
                "kind": "activity",
                "config": config,
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "ok", "type": "any"}, {"id": "err", "type": "error"}],
                },
            },
            {
                "id": "n_out",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n_in",
                "sourceHandle": "out",
                "target": "n_tool",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e2",
                "source": "n_tool",
                "sourceHandle": out_handle,
                "target": "n_out",
                "targetHandle": "in",
                "channel": "data",
            },
        ],
    }


def _bearer_connection(client: TestClient, secret: str = "tok-SECRET-123") -> str:
    return client.post(
        "/connections",
        json={
            "name": "api",
            "kind": "http_auth",
            "config": {"auth": {"type": "bearer"}},
            "secret": secret,
        },
    ).json()["id"]


def _run(client: TestClient, ir: dict[str, Any], input_value: Any) -> dict[str, Any]:
    resp = client.post("/graphs/runs", json={"ir": ir, "input": input_value, "stream": False})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_http_tool_injects_auth_and_substitutes(client: TestClient) -> None:
    with fake_http() as server:
        con = _bearer_connection(client, "tok-SECRET-123")
        ir = _http_agent(
            server.url,
            con,
            method="GET",
            url_path="/v1/forecast?city=$in.in.city",
            headers={"X-Trace": "$in.in.trace"},
        )
        out = _run(client, ir, {"city": "berlin", "trace": "abc"})
        assert out["status"] == "completed"

        # The connection injected the bearer auth SERVER-SIDE — the receiving server saw it.
        assert len(server.requests) == 1
        req = server.requests[0]
        assert req["headers"]["authorization"] == "Bearer tok-SECRET-123"
        # $in.field substitution reached the wire (url query + header).
        assert "city=berlin" in req["path"]
        assert req["headers"]["x-trace"] == "abc"

    # The secret value NEVER appears in the IR the client sent (auth is server-side only — §1.1).
    import json as _json

    assert "tok-SECRET-123" not in _json.dumps(ir)


def test_http_tool_response_map_shapes_output(client: TestClient) -> None:
    with fake_http() as server:
        server.set_response(body={"data": {"value": 99}, "other": 1})
        con = _bearer_connection(client)
        ir = _http_agent(server.url, con, response_map="$.data.value")
        out = _run(client, ir, {})
        assert out["output"] == "99"  # mapped to data.value, serialized


def test_http_tool_graphql_body(client: TestClient) -> None:
    with fake_http() as server:
        con = _bearer_connection(client)
        ir = _http_agent(
            server.url,
            con,
            method="POST",
            body={"query": "query($c:String){f(c:$c)}", "variables": {"c": "$in.in.city"}},
        )
        _run(client, ir, {"city": "berlin"})
        body = server.requests[0]["body"]
        assert body["query"].startswith("query(")
        assert body["variables"] == {"c": "berlin"}  # the $in ref kept its (string) value


def test_http_tool_idempotency_key_reaches_the_wire(client: TestClient) -> None:
    # §2.5: an idempotencyKey (templated, {runId}/{nodeId} + $in) becomes an Idempotency-Key header
    # so a partial-failure re-run is a provider no-op. Here we prove it reaches the wire.
    with fake_http() as server:
        con = _bearer_connection(client)
        ir = _http_agent(server.url, con, method="POST")
        ir["nodes"][1]["config"]["idempotencyKey"] = "order-{nodeId}-$in.in.orderId"
        _run(client, ir, {"orderId": "A123"})
        key = server.requests[0]["headers"].get("idempotency-key")
        assert key is not None
        assert "n_tool" in key  # {nodeId} expanded
        assert "A123" in key  # $in.in.orderId expanded


def test_http_tool_non_2xx_is_ok_value(client: TestClient) -> None:
    with fake_http() as server:
        server.set_response(status=404, body={"error": "nope"})
        con = _bearer_connection(client)
        ir = _http_agent(server.url, con)
        out = _run(client, ir, {})
        # A non-2xx is a normal return value bound to ``ok`` (m6.md §4), not an err.
        assert out["status"] == "completed"
        assert '"status": 404' in out["output"]


def test_http_tool_transport_failure_binds_err(client: TestClient) -> None:
    con = _bearer_connection(client)
    # Point at a closed port → transport failure → the ``err`` handle (wired to output).
    ir = _http_agent("http://127.0.0.1:9", con, out_handle="err")
    out = _run(client, ir, {})
    assert out["status"] == "completed"
    assert "http tool" in out["output"]


def test_http_tool_missing_connection_binds_err(client: TestClient) -> None:
    with fake_http() as server:
        ir = _http_agent(server.url, "con_does_not_exist", out_handle="err")
        out = _run(client, ir, {})
        assert out["status"] == "completed"
        assert "connection not found" in out["output"]
        assert server.requests == []  # no call made — auth never resolved


def test_rotating_secret_keeps_agent_hash_and_ir_has_no_secret(client: TestClient) -> None:
    # The M19 §1.1 / §4 key guard: a saved agent referencing a connection has a stable contentHash;
    # rotating the connection's secret does NOT move it, and the saved IR carries no secret.
    with fake_http() as server:
        con = _bearer_connection(client, "v1-secret")
        ir = _http_agent(server.url, con)
        created = client.post("/agents", json={"ir": ir, "name": "http-agent"})
        assert created.status_code == 201, created.text
        hash_before = created.json()["versions"][0]["content_hash"]

        # The stored IR carries the connection id, never the secret.
        stored = client.get(f"/agents/{ir['id']}/versions/{ir['version']}").json()
        import json as _json

        assert "v1-secret" not in _json.dumps(stored["ir"])
        assert con in _json.dumps(stored["ir"])

        # Rotate the secret in place → the agent's contentHash is unchanged (immutability holds).
        client.patch(f"/connections/{con}", json={"secret": "v2-secret"})
        again = client.get(f"/agents/{ir['id']}/versions/{ir['version']}").json()
        assert again["content_hash"] == hash_before
