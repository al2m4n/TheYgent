"""The ``modality`` axis on the inference-plane registration payload (registration.py).

``modality`` is the THIRD orthogonal axis (alongside ``binding`` = engine and ``source`` = weights):
it names the *task* a managed model serves and the manager dispatches the spawn on it. The guards:

* it is ADDITIVE + backward-compatible — a payload without a ``modality`` key parses as ``chat``;
* the frozen ``Modality`` vocabulary is enforced (an unknown value fails loudly);
* it lands ONLY on ``ManagedBinding`` — a reachable binding rejects it (``extra="forbid"``);
* it is NOT a new ``binding`` value — the binding enum is untouched;
* it round-trips through the registry-persistence wire shape (``model_dump(by_alias=True)``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from theygent_ir import ManagedBinding, parse_registration

# ── backward compatibility: a payload without modality defaults to "chat" ─────


def test_managed_binding_defaults_modality_to_chat() -> None:
    # The exact shape an older registry.json row / registration call carries (no modality key).
    b = parse_registration({"binding": "mlx", "source": "hf", "model": "m"})
    assert isinstance(b, ManagedBinding)
    assert b.modality == "chat"


def test_explicit_chat_equals_omitted() -> None:
    # A binding that writes modality="chat" is identical to one that omits it (the default).
    omitted = parse_registration({"binding": "llamacpp", "source": "local-path", "model": "m"})
    explicit = parse_registration(
        {"binding": "llamacpp", "source": "local-path", "model": "m", "modality": "chat"}
    )
    assert omitted == explicit


# ── the new modalities parse ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "modality", ["chat", "vision", "embeddings", "audio.transcription", "audio.speech"]
)
def test_every_frozen_modality_parses(modality: str) -> None:
    b = parse_registration({"binding": "mlx", "source": "hf", "model": "m", "modality": modality})
    assert isinstance(b, ManagedBinding)
    assert b.modality == modality


# ── the frozen vocabulary is enforced ────────────────────────────────────────


def test_unknown_modality_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_registration({"binding": "mlx", "source": "hf", "model": "m", "modality": "video"})


def test_modality_is_not_a_binding_value() -> None:
    # The binding enum stays frozen: an engine/modality name like "mlx-vlm" is not a binding.
    with pytest.raises(ValidationError):
        parse_registration({"binding": "mlx-vlm", "source": "hf", "model": "m"})


# ── modality lands ONLY on the managed arm ───────────────────────────────────


def test_reachable_binding_rejects_modality() -> None:
    # A reachable openai-compatible upstream self-describes modality over its URL — extra=forbid.
    with pytest.raises(ValidationError):
        parse_registration(
            {
                "binding": "openai-compatible",
                "baseUrl": "https://api.example.com/v1",
                "model": "gpt-x",
                "modality": "vision",
            }
        )


# ── the registry-persistence round-trip (registry.json wire shape) ───────────


def test_modality_round_trips_through_wire_dump() -> None:
    # Registry._persist writes model_dump(by_alias=True); _load re-parses it. The modality must
    # survive that round-trip so an installed VLM/embeddings model spawns the right server on boot.
    original = parse_registration(
        {"binding": "mlx", "source": "local-path", "model": "/w", "modality": "vision"}
    )
    reparsed = parse_registration(original.model_dump(by_alias=True))
    assert reparsed == original
    assert isinstance(reparsed, ManagedBinding)
    assert reparsed.modality == "vision"
