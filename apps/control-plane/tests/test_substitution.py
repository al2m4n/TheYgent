"""Fast suite for the substitution token language (m9.md §1 + m10.md §1.2).

Before M9 the ``llm`` node spoke ``$input`` (whole-value-only) while ``tool``/``router`` spoke
``$in``/``$in.field`` — two look-alike languages that don't compose, and a wrong token passed
through as LITERAL TEXT so a run completed green with nonsense (finding F1.1). M9 unified them onto
``$in``/``$in.field`` and made an unknown token a LOUD failure. M10 made in-ports the addressable
unit: the segment after ``$in.`` is a **port name**, so M9's single-value drilling now lives at
``$in.in.<field>`` (the file+question composition lives in ``test_multi_input.py``).

These prove the per-llm-node behaviours, now port-addressed:
  * ``$in`` in an llm node == the default in-port's whole value (parity with the old ``$input``);
  * ``$in.in.<field>`` drills into an object in-port value in an llm node;
  * the OLD ``$in.<field>`` form is a loud migration error (port-first, names a did-you-mean hint);
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


def test_dollar_in_port_field_drills_into_object(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # M10 port-addressed drilling: the default in-port `in` carries an object, and
    # $in.in.<field> selects a field of it (M9's $in.field drilling now lives at $in.in.field).
    ir = llm_over_object_ir("Summarize:\n\n$in.in.body", {"body": "war and peace", "meta": 1})
    body = _run(client, ir)
    assert body["status"] == "completed"
    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "Summarize:\n\nwar and peace"}
    ]


def test_old_dollar_in_field_form_is_a_loud_migration_error(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # M10 migration loudness (m10.md §5): the OLD M9 spelling `$in.body` now reads as "in-port
    # named body", which doesn't exist on a single-`in`-port node → loud error naming the ports,
    # not a silent green run with garbage. The fix is the named hint: $in.in.body.
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
    # A $in.<port>.<field> drill into a field that doesn't exist is a loud error, and the message
    # lists what WAS available on that port's value (M10: drilling lives at $in.in.<field>).
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
