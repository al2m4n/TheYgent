"""``requireFinishReason`` on the reachable registration payload (registration.py).

A reachable binding may declare that its upstream ends every chat stream with a
``finish_reason``, so the inference plane reports a stream that ends without one as cut off. The
guards:

* it is ADDITIVE and off by default — a payload without the key parses with it ``False``, so an
  existing registration keeps its unchecked streams;
* it exists on the reachable arm only — a managed engine is always checked, so the key is
  rejected there like any unknown key;
* it round-trips through the registry-persistence wire shape (``model_dump(by_alias=True)``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from theygent_ir import ReachableBinding, parse_registration

_REACHABLE = {
    "binding": "openai-compatible",
    "baseUrl": "https://api.example.com/v1",
    "model": "provider-model",
}


def test_off_by_default() -> None:
    b = parse_registration(_REACHABLE)
    assert isinstance(b, ReachableBinding)
    assert b.require_finish_reason is False


def test_declared_over_the_wire_in_camel_case() -> None:
    b = parse_registration({**_REACHABLE, "requireFinishReason": True})
    assert isinstance(b, ReachableBinding)
    assert b.require_finish_reason is True
    assert b.model_dump(by_alias=True)["requireFinishReason"] is True


def test_round_trips_through_wire_dump() -> None:
    original = parse_registration({**_REACHABLE, "requireFinishReason": True})
    assert parse_registration(original.model_dump(by_alias=True)) == original


def test_not_a_managed_binding_field() -> None:
    with pytest.raises(ValidationError):
        parse_registration(
            {
                "binding": "llamacpp",
                "source": "local-path",
                "model": "/w.gguf",
                "requireFinishReason": True,
            }
        )


def test_must_be_a_boolean() -> None:
    with pytest.raises(ValidationError):
        parse_registration({**_REACHABLE, "requireFinishReason": "sometimes"})
