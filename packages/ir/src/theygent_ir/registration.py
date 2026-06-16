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

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.alias_generators import to_camel

ManagedBindingName = Literal["mlx", "vllm", "llamacpp"]
ReachableBindingName = Literal["openai-compatible"]
SourceName = Literal["hf", "local-path", "url"]

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
    max_context: int | None = None
    #: True when some fields are conservative/inferred rather than probed from the
    #: engine (e.g. MLX exposes no capability endpoint — same posture as the M1
    #: chat_template_caps follow-up). A consumer should treat such a report as
    #: best-effort, not authoritative.
    approximate: bool = False
