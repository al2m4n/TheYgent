"""Fast suite for the substitution token language.

``$in``/``$in.field`` is the ONE template token language across every substituting node
(``llm``/``tool``/``mcp_tool``/``router``), and an unknown token is a LOUD failure — a wrong token
must never pass through as LITERAL TEXT and complete green with nonsense. In-ports are the
addressable unit: the segment after ``$in.`` is a **port name**, resolved port-first, so
single-value drilling lives at ``$in.in.<field>`` (the file+question composition lives in
``test_multi_input.py``).

These prove the per-llm-node behaviours:
  * ``$in`` in an llm node == the default in-port's whole value;
  * ``$in.in.<field>`` drills into an object in-port value in an llm node;
  * the legacy single-port ``$in.<field>`` form is a loud error with a did-you-mean hint;
  * an unknown token (``$nope`` or the legacy ``$input``) → precise error, no green run;
  * ``$$`` escapes a literal ``$``.
"""

from __future__ import annotations

from _fake_inference import FakeInference
from _ir import llm_ir, llm_over_object_ir
from fastapi.testclient import TestClient


def _run(client: TestClient, ir: dict, *, input_: str = "the run input") -> dict:
    return client.post("/graphs/runs", json={"ir": ir, "input": input_, "stream": False}).json()


def test_dollar_in_is_whole_in_port_value(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # $in resolves to the node's whole in-port value (the run input).
    body = _run(client, llm_ir("Summarize: $in"), input_="name three EU capitals")
    assert body["status"] == "completed"
    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "Summarize: name three EU capitals"}
    ]


def test_dollar_in_port_field_drills_into_object(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # Port-addressed drilling: the default in-port `in` carries an object, and
    # $in.in.<field> selects a field of it (single-port field drilling lives at $in.in.field).
    ir = llm_over_object_ir("Summarize:\n\n$in.in.body", {"body": "war and peace", "meta": 1})
    body = _run(client, ir)
    assert body["status"] == "completed"
    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "Summarize:\n\nwar and peace"}
    ]


def test_old_dollar_in_field_form_is_a_loud_migration_error(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # Migration loudness: the legacy single-port spelling `$in.body` reads as "in-port
    # named body", which doesn't exist on a single-`in`-port node → loud error naming the ports,
    # not a silent green run with garbage. The message carries the named hint: $in.in.body.
    ir = llm_over_object_ir("Summarize: $in.body", {"body": "war and peace"})
    body = _run(client, ir)
    assert body["error"]["code"] == "template_error"
    msg = body["error"]["message"]
    assert "no in-port named 'body'" in msg
    assert "did you mean $in.in.body?" in msg  # the migration hint
    assert fake_inference.captured["model"] is None  # nothing reached the wire


def test_unknown_token_is_a_loud_error_not_a_green_run(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # A wrong token does NOT pass through as literal text and complete green.
    # It fails with a precise message naming the node and (here, a string in-port) the situation.
    body = _run(client, llm_ir("Summarize: $nope"))
    assert "status" not in body or body.get("status") != "completed"
    assert body["error"]["code"] == "template_error"
    assert "n_llm" in body["error"]["message"]
    assert "$nope" in body["error"]["message"]
    # Nothing reached the inference wire — the render failed before open_stream.
    assert fake_inference.captured["model"] is None


def test_removed_dollar_input_token_now_errors(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # $input is not a token — it's an unknown root, named explicitly so a stale IR is legible.
    body = _run(client, llm_ir("Summarize: $input"))
    assert body["error"]["code"] == "template_error"
    assert "$input" in body["error"]["message"]
    assert fake_inference.captured["model"] is None


def test_missing_field_names_available_fields(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # A $in.<port>.<field> drill into a field that doesn't exist is a loud error, and the message
    # lists what WAS available on that port's value (drilling lives at $in.in.<field>).
    ir = llm_over_object_ir("$in.in.nope", {"body": "x", "title": "y"})
    body = _run(client, ir)
    assert body["error"]["code"] == "template_error"
    msg = body["error"]["message"]
    assert "$in.in.nope" in msg
    assert "body" in msg and "title" in msg  # available fields surfaced


def test_double_dollar_is_a_literal_dollar(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # $$ escapes a literal $, so legitimate text isn't caught by the token rejector.
    body = _run(client, llm_ir("Pay $$5 for $in"), input_="the book")
    assert body["status"] == "completed"
    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "Pay $5 for the book"}
    ]
