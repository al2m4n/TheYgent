"""Fast suite for M9 Part A — the ONE substitution token language (m9.md §1).

Before M9 the ``llm`` node spoke ``$input`` (whole-value-only) while ``tool``/``router`` spoke
``$in``/``$in.field`` — two look-alike languages that don't compose, and a wrong token passed
through as LITERAL TEXT so a run completed green with nonsense (finding F1.1). M9 unifies on
``$in``/``$in.field`` everywhere and makes an unknown token a LOUD failure, never a silent literal.

These prove the four behaviours the milestone calls out:
  * ``$in`` in an llm node == the whole in-port value (exact parity with the old ``$input``);
  * ``$in.field`` drills into an object in-port value in an llm node;
  * an unknown token (``$nope`` or the now-removed ``$input``) → precise error, no green run;
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
    # Parity with the old $input: $in resolves to the node's whole in-port value (the run input).
    body = _run(client, llm_ir("Summarize: $in"), input_="name three EU capitals")
    assert body["status"] == "completed"
    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "Summarize: name three EU capitals"}
    ]


def test_dollar_in_field_drills_into_object(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # The new power M9 adds to the llm node: when the in-port value is an object, $in.field selects
    # a field of it (the same field-aware resolver tool/router already had).
    ir = llm_over_object_ir("Summarize:\n\n$in.body", {"body": "war and peace", "meta": 1})
    body = _run(client, ir)
    assert body["status"] == "completed"
    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "Summarize:\n\nwar and peace"}
    ]


def test_unknown_token_is_a_loud_error_not_a_green_run(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # The F1.1 class fix: a wrong token does NOT pass through as literal text and complete green.
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
    # $input is no longer a token — it's an unknown root, named explicitly so a stale IR is legible.
    body = _run(client, llm_ir("Summarize: $input"))
    assert body["error"]["code"] == "template_error"
    assert "$input" in body["error"]["message"]
    assert fake_inference.captured["model"] is None


def test_missing_field_names_available_fields(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # A $in.field that doesn't exist is a loud error too, and the message lists what WAS available.
    ir = llm_over_object_ir("$in.nope", {"body": "x", "title": "y"})
    body = _run(client, ir)
    assert body["error"]["code"] == "template_error"
    msg = body["error"]["message"]
    assert "$in.nope" in msg
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
