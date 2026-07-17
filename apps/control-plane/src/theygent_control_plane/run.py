"""The ``Run`` domain entity — the API/logic shape, free of storage coupling.

``Run`` is a clean Pydantic entity that maps 1:1 to a table. ``Run`` stays the **domain**
shape and is *not* the ORM row: it is mapped to/from ``RunRow`` in ``store.py``.
``session_id`` is the one additive field (optional; ``None`` = a one-shot run).

The earlier in-memory run registry is gone: persistence now lives in the Postgres-backed
``RunStore`` (``store.py``), so runs survive a restart and are shared state across
horizontally-scaled control-plane instances.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from ulid import ULID

# ``waiting`` is the additive durable-wait status — a run paused at a ``human`` node,
# checkpointed on ``DBOS.recv``. It is NOT ``failed`` and is deliberately EXCLUDED from the
# reconciliation sweep (which touches only ``created``/``streaming``), so a run can wait
# indefinitely across a worker restart without being reconciled to ``failed``.
RunStatus = Literal["created", "streaming", "waiting", "completed", "failed"]


def now() -> datetime:
    return datetime.now(UTC)


def new_ulid() -> str:
    return str(ULID())


def new_connection_id() -> str:
    """A fresh connection id (``con_<ulid>``) — the stable logical reference the IR's
    ``tools`` block points at. The ``con_`` prefix makes the id recognizable in IR/config/logs."""
    return f"con_{ULID()}"


def new_draft_id() -> str:
    """A fresh draft id (``drf_<ulid>``). The ``drf_`` prefix makes the id recognizable in
    URLs/logs, mirroring ``con_<ulid>`` — and keeps a draft id visibly distinct from the
    agent id it may eventually publish as."""
    return f"drf_{ULID()}"


class Run(BaseModel):
    """A single request, from creation to a terminal status.

    ``model`` is the **logical** model id forwarded to inference (never an engine name).
    ``session_id`` is ``None`` for a one-shot run. The status lifecycle is
    ``created`` -> ``streaming`` -> ``completed`` | ``failed``.

    Three nullable graph fields are populated for a ``/graphs/runs`` run and ``None`` for a
    plain ``/runs`` run: ``graph_id`` + ``graph_version`` are the IR's registry coordinate,
    ``content_hash`` its content-addressed identity.
    """

    id: str = Field(default_factory=new_ulid)
    session_id: str | None = None
    status: RunStatus = "created"
    model: str
    graph_id: str | None = None
    graph_version: str | None = None
    content_hash: str | None = None
    # Which trigger fired this run (None = interactive) — giving an unattended run
    # lineage back to the schedule/webhook that fired it.
    trigger_id: str | None = None
    # The run's final output, persisted on success so GET /runs/{id} can return it for a
    # session-less run too (not only the live SSE stream). None until a terminal output is reached.
    output: str | None = None
    # When a run is ``waiting`` at a ``human`` node, the id of that node — the bookkeeping
    # the waiting status needs. ``POST /runs/{id}/resume`` reads it to validate the awaited
    # input against the node's declared schema and to deliver it. NULL when not waiting.
    awaiting_node: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    # The real terminal-completion instant (None until a real-time terminal transition; stays
    # None for a reconcile-swept zombie). duration = completed_at - created_at; run intervals
    # give system-wide concurrency.
    completed_at: datetime | None = None
    error: str | None = None


# ── Read models for the cockpit list views ────────────────────────────────────
# Read-only projections the list endpoints return; like ``Run`` they are domain shapes the
# store maps rows onto, never ORM rows. They add no write path — the cockpit only
# reads existing persisted state.


class SessionSummary(BaseModel):
    """One row of the sessions list — aggregates over a session's messages."""

    id: str
    created_at: datetime
    last_activity: datetime
    message_count: int
    # First user message (session message at ``position == 0``); ``None`` for an empty session.
    preview: str | None = None
    # Opaque client-owned JSONB (kind/model/title …) — stored and returned verbatim,
    # never interpreted by the control plane.
    metadata: dict[str, Any] | None = None


class SessionMessage(BaseModel):
    id: str
    # None = a client-appended turn (persisted chat history with no run behind it);
    # populated when a run wrote the pair.
    run_id: str | None = None
    role: str  # user | assistant
    content: str
    position: int
    created_at: datetime


class SessionDetail(BaseModel):
    """A session with its messages in ``position`` order (session detail view)."""

    id: str
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] | None = None
    messages: list[SessionMessage] = Field(default_factory=list)


# ── Agent registry domain entities ───────────────────────────────────────────
# Like ``Run``, these are domain shapes the store maps rows onto — never ORM rows. The
# registry stores the canonical IR document; it invents no "agent format". The IR is the
# source of truth for ``id`` (the agent id) and ``version`` (semver), so a stored agent and
# the Run it produces agree byte-for-byte on ``graph_id``/``graph_version``/``hash``.
# ``AgentVersion`` is the lean per-version metadata (no IR payload — it lists fast);
# ``StoredVersion`` carries the full IR + view, returned for a single version and used to resolve
# an invoke-by-reference run.


class AgentVersion(BaseModel):
    """One immutable version's metadata — no IR payload (the list/detail views show coordinates,
    not the document). ``content_hash`` is the content-addressed key; ``seq`` is the
    monotonic-per-agent ordering, newest first in the listings."""

    version: str
    content_hash: str
    seq: int
    created_at: datetime


class AgentSummary(BaseModel):
    """One row of the agents list — newest agent first. Carries the latest version coordinate
    + a count so the cockpit row reads "name, latest version, hash, count" without a second
    call (client-side composition; no aggregating endpoint)."""

    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    latest_version: str | None = None
    latest_content_hash: str | None = None
    version_count: int = 0


class AgentDetail(BaseModel):
    """An agent and its versions, newest first (GET /agents/{id})."""

    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    versions: list[AgentVersion] = Field(default_factory=list)


class StoredVersion(BaseModel):
    """A resolved version with its full IR (+ view) — returned by GET /agents/{id}/versions/{ver}
    and the value an invoke-by-reference run resolves to before walking it. The ``ir`` is the
    canonical, view-stripped document; ``view`` is the stored-but-never-hashed layout."""

    agent_id: str
    version: str
    content_hash: str
    seq: int
    created_at: datetime
    ir: dict[str, Any]
    view: dict[str, Any] | None = None


class AgentDraft(BaseModel):
    """A mutable, autosaved, possibly-invalid agent graph — the editor's work-in-progress,
    the deliberate opposite of ``StoredVersion`` (immutable, content-addressed, validated).
    The ``ir`` is never validated or hashed; ``view`` is the layout, split off exactly like the
    registry's ir/view. ``agent_id`` is the registry agent this draft edits (``None`` for a
    never-published graph; a plain breadcrumb, never an enforced link). ``owner_id`` is the
    deferred per-user scoping slot (``None`` until identity lands). ``name``/``node_count`` are
    derived list-view labels, re-derived from the ir on every write."""

    id: str = Field(default_factory=new_draft_id)
    agent_id: str | None = None
    owner_id: str | None = None
    name: str
    node_count: int = 0
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    ir: dict[str, Any]
    view: dict[str, Any] | None = None


# ── Trigger domain entity — the deploy primitive ──────────────────────────────
# The frozen trigger *contract*: what fires which saved, pinned agent, on what condition. Like
# ``Run``/``Agent*`` it is a domain shape the store maps ``TriggerRow`` onto, never the ORM
# row. The dispatcher that reads it and fires runs is a reversible in-process detail — this
# shape is the hard-to-reverse seam it must not reshape.

TriggerKind = Literal["http", "schedule", "webhook"]


class Trigger(BaseModel):
    """A persisted, non-interactive entry point for a saved agent. A trigger always pins exactly
    one of ``version`` / ``content_hash`` — an unattended deploy runs an immutable artifact.
    ``kind`` is the firing mechanism; ``config`` carries the kind-specific knobs (a cron
    expression, a webhook signing secret, an optional input template). ``last_fired_at`` is
    dispatcher bookkeeping, not part of the frozen contract."""

    id: str = Field(default_factory=new_ulid)
    agent_id: str
    version: str | None = None
    content_hash: str | None = None
    kind: TriggerKind
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    last_fired_at: datetime | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)

    def public_dump(self) -> dict[str, Any]:
        """The wire shape, with the webhook ``secret`` redacted (sovereignty: a signing secret is
        the user's credential — list its presence, never echo its value, mirroring how
        ``_mcp_view`` lists ``env`` keys without values)."""
        data = self.model_dump(mode="json")
        config = dict(data.get("config") or {})
        if "secret" in config:
            config["secret"] = "***"
        data["config"] = config
        return data


# ── Connection domain entity — the tool/MCP auth seam ────────────────────────────────────────────
# A connection is a server-side auth + config binding the IR references by id from its ``tools``
# block. Like ``Run``/``Trigger`` it is a domain shape the store maps ``ConnectionRow`` onto, not
# the ORM row. The secret material lives behind ``secret_ref`` in the encrypted ``secret`` table
# (``secrets.py``) — never in ``config`` and never on the wire. ``config`` is NON-SECRET only.

ConnectionKind = Literal["http_auth", "mcp_server"]

#: Defensive redaction: keys that must never appear on the wire even if a caller wrongly nested a
#: secret in ``config`` (secrets belong behind ``secret_ref``). Mirrors Trigger ``secret`` →
#: ``***``.
_SECRETISH_CONFIG_KEYS = frozenset(
    {"secret", "password", "token", "apiKey", "api_key", "authToken", "clientSecret"}
)


class Connection(BaseModel):
    """A persisted tool/MCP auth + config binding. ``kind`` selects the shape: ``http_auth``
    (an http tool's base url / static headers / auth scheme) or ``mcp_server`` (an MCP server
    by stdio or HTTP). ``config`` carries ONLY non-secret material; the secret material is
    encrypted behind ``secret_ref`` (``secrets.py``). The IR references this by ``id``; rotating
    the secret keeps every agent's ``contentHash`` stable (the id is hashed content, not the
    secret)."""

    id: str = Field(default_factory=new_connection_id)
    name: str
    kind: ConnectionKind
    config: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None
    enabled: bool = True
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)

    def public_dump(self) -> dict[str, Any]:
        """The wire shape (redaction): the ``secret_ref`` is internal plumbing and the secret
        itself is never stored here, so the wire exposes only whether a secret is SET
        (``hasSecret``), never the ref or value — mirroring how ``Trigger.public_dump`` redacts to
        ``***`` and ``_mcp_view`` lists env keys without values. Any secret-ish key wrongly nested
        in ``config`` is also redacted defensively (secrets belong behind ``secret_ref``).

        A generated MCP server's ``config.spec`` (a whole parsed OpenAPI document, possibly
        megabytes) is elided to a small summary — list responses must stay light, and the wire
        never needs the document back (changing the spec is a re-upload, not an edit)."""
        data = self.model_dump(mode="json")
        data["hasSecret"] = self.secret_ref is not None
        data.pop("secret_ref", None)
        config = dict(data.get("config") or {})
        for key in list(config):
            if key in _SECRETISH_CONFIG_KEYS:
                config[key] = "***"
        spec = config.get("spec")
        if isinstance(spec, dict):
            info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
            paths = spec.get("paths") if isinstance(spec.get("paths"), dict) else {}
            config["spec"] = {
                "title": info.get("title"),
                "version": info.get("version"),
                "pathCount": len(paths),
            }
        data["config"] = config
        return data


# ── Bench domain entities — the deferred test/benchmark layer ────────────────────────────────────
# Like Run/Agent*/Trigger these are domain shapes the store maps the ``bench_*`` rows onto,
# never the ORM rows. A benchmark is honest only if pinned to exactly what ran; a preset is a
# literal param snippet, never a live reference. The bench adds NO execution path — these record
# what the bench produced (model tests hit the inference data plane in the user's domain directly;
# agent tests reuse the agent invoke path), so nothing inference-shaped lives in these entities.

BenchTargetKind = Literal["model", "agent"]
AssertionKind = Literal["exact", "contains", "regex", "json-path-equals", "llm-judge"]


class BenchCase(BaseModel):
    """One golden case: an authored ``input``, an optional ``expected``, and the ``assertion``
    to score it by. ``assertion_config`` carries the kind-specific knob (regex / json path /
    llm-judge rubric + model)."""

    id: str = Field(default_factory=new_ulid)
    input: Any = None
    expected: Any = None
    assertion: AssertionKind = "contains"
    assertion_config: dict[str, Any] = Field(default_factory=dict)
    seq: int = 0
    created_at: datetime = Field(default_factory=now)


class BenchSuite(BaseModel):
    """A saved suite of golden cases pinned to one target. A model suite sets
    ``logical_id``/``binding``; an agent suite sets ``agent_id`` + a version/contentHash pin."""

    id: str = Field(default_factory=new_ulid)
    name: str
    target_kind: BenchTargetKind
    modality: str | None = None
    logical_id: str | None = None
    binding: str | None = None
    agent_id: str | None = None
    version: str | None = None
    content_hash: str | None = None
    cases: list[BenchCase] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class BenchRun(BaseModel):
    """One recorded benchmark result — metrics pinned to exactly what ran. A MODEL pin sets
    ``logical_id`` + ``model_ref`` + ``binding`` + ``params_digest``; an AGENT pin sets
    ``agent_id`` + ``version`` + ``content_hash``. ``metrics`` is the numbers; ``output_digest``
    is a content identity for the compare diff WITHOUT the raw output; ``capture_ref`` is the
    opt-in LOCAL reference to raw I/O (never a blob — user-controlled storage only)."""

    id: str = Field(default_factory=new_ulid)
    target_kind: BenchTargetKind
    modality: str
    logical_id: str | None = None
    model_ref: str | None = None
    binding: str | None = None
    params: dict[str, Any] | None = None
    params_digest: str | None = None
    agent_id: str | None = None
    version: str | None = None
    content_hash: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    output_digest: str | None = None
    capture_ref: str | None = None
    suite_id: str | None = None
    case_id: str | None = None
    assertion: str | None = None
    assertion_passed: bool | None = None
    run_id: str | None = None
    label: str | None = None
    created_at: datetime = Field(default_factory=now)


class BenchPreset(BaseModel):
    """A named, modality-scoped, LITERAL param set — values only, no link to a run or agent.
    "Apply preset" copies these literals into ``models[binding].params`` client-side (the IR
    stores the *values*, never the preset name — storing a name instead of values would cause
    contentHash drift when a preset changes)."""

    id: str = Field(default_factory=new_ulid)
    name: str
    modality: str
    logical_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
