"""Inference-plane registration payload — theygent-stack-9.1.md §9.1.3.

The wire format is camelCase (the control-plane speaks it); internally we use
snake_case via Pydantic aliases. The `binding` enum (theygent-graph-schema.md
§8.4) is load-bearing: ``mlx | vllm | llamacpp`` are the engines theygent
manages the lifecycle of, ``openai-compatible`` is a reachable passthrough.
``source`` (where the weights come from) is orthogonal to ``binding`` (which
engine serves them) — never conflate them.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from pydantic.alias_generators import to_camel

ManagedBindingName = Literal["mlx", "vllm", "llamacpp"]
ReachableBindingName = Literal["openai-compatible"]
SourceName = Literal["hf", "local-path", "url"]

#: The frozen modality vocabulary (M18 §1.2 / theygent-stack-9.1.md §9.1.2). The bench's tester
#: panels are keyed on these — a new modality is a new key + a panel registered against it, never a
#: hardcoded ``if vision … else if stt …`` tree. ``vision`` is a sub-capability of ``chat`` (M18
#: §1.3): a vision model reports ``["chat", "vision"]`` and runs on ``/v1/chat/completions``.
#: ``images.generation`` and ``rerank`` are RESERVED/DEFERRED (named, not implemented).
Modality = Literal["chat", "vision", "embeddings", "audio.transcription", "audio.speech"]
MODALITIES: frozenset[str] = frozenset(
    {"chat", "vision", "embeddings", "audio.transcription", "audio.speech"}
)

#: Engines whose lifecycle the inference plane owns (spawn / warm / evict).
MANAGED_BINDINGS: frozenset[str] = frozenset({"mlx", "vllm", "llamacpp"})
#: Every legal `binding` value (§8.4). Used to reject engine names on /v1/*.
BINDING_NAMES: frozenset[str] = MANAGED_BINDINGS | {"openai-compatible"}


class _Wire(BaseModel):
    """Base: camelCase over the wire, snake_case in code, reject unknown keys."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        protected_namespaces=(),
    )


class Lifecycle(_Wire):
    keep_warm: bool = False
    idle_timeout_sec: int = 900
    priority: int = 0


class ManagedBinding(_Wire):
    """A lifecycle-managed local engine (mlx / vllm / llamacpp)."""

    binding: ManagedBindingName
    source: SourceName
    model: str
    params: dict[str, Any] = Field(default_factory=dict)
    lifecycle: Lifecycle = Field(default_factory=Lifecycle)
    fallback: str | None = None


class ReachableBinding(_Wire):
    """A reachable passthrough: a URL + credential reference, never managed.

    ``credential_ref`` resolves in the user's trust domain only and must never
    cross a cloud boundary (theygent-stack.md §10).
    """

    binding: ReachableBindingName
    base_url: str
    model: str
    credential_ref: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


RegistrationPayload = Annotated[
    ManagedBinding | ReachableBinding,
    Field(discriminator="binding"),
]

_ADAPTER: TypeAdapter[RegistrationPayload] = TypeAdapter(RegistrationPayload)


def parse_registration(data: Any) -> ManagedBinding | ReachableBinding:
    """Validate a raw registration payload into the right binding model."""

    return _ADAPTER.validate_python(data)


class Capabilities(_Wire):
    """What a bound model actually supports — §9.1.2.

    The compiler queries this *before* a run to validate that a node's model
    supports the tool-calling / structured output / context the graph needs.
    """

    tool_calling: bool = False
    structured_output: bool = False
    vision: bool = False
    #: The model emits a hidden chain-of-thought (a "reasoning"/"thinking" model). Detected
    #: best-effort from the chat template (a ``<think>`` section / ``enable_thinking`` switch),
    #: so it rides the ``approximate`` flag like the other inferred fields.
    reasoning: bool = False
    max_context: int | None = None
    #: What the bench routes its tester panels on (M18 §1.2 / §9.1.2). DERIVED, not a new probe:
    #: left at its default it is filled from the flags — ``["chat"]`` (+ ``"vision"`` when the
    #: ``vision`` flag is set), the right answer for every chat/VLM engine theygent manages today.
    #: A non-chat engine (an embeddings server, a Whisper STT, a TTS) declares its modalities
    #: explicitly (e.g. ``modalities=["audio.transcription"]``) and the derivation leaves it alone.
    #: So it rides the ``approximate`` flag like the other inferred fields.
    modalities: list[Modality] = Field(default_factory=list)
    #: True when some fields are conservative/inferred rather than probed from the
    #: engine (e.g. MLX exposes no capability endpoint — same posture as the M1
    #: chat_template_caps follow-up). A consumer should treat such a report as
    #: best-effort, not authoritative.
    approximate: bool = False

    @model_validator(mode="after")
    def _derive_modalities(self) -> Capabilities:
        # Empty (the default) → derive from the capability flags: every managed engine today is a
        # chat engine, with ``vision`` as a sub-capability. A non-chat engine passes ``modalities``
        # explicitly, which is preserved. ``extra="forbid"`` + the ``Modality`` Literal reject an
        # unknown key/value loudly (the §4 "unknown modality is rejected" guard).
        if not self.modalities:
            mods: list[Modality] = ["chat"]
            if self.vision:
                mods.append("vision")
            self.modalities = mods
        return self
