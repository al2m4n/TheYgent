"""Bundle build (export) + bundle apply (import) — the control-plane transfer surface.

The bundle is the snake_case envelope ``POST /export`` returns and ``POST /import`` consumes
(``format_version`` 1; IR documents inside stay their stored camelCase form, verbatim). Reads here
are DB-level on purpose: the public wire is lossy exactly where a faithful transfer needs fidelity
(``run.params`` appears on no API response; ``GET /connections`` elides ``config.spec``). Every
write preserves the source row's ids and timestamps, because ids are load-bearing across tables —
span PKs embed ``run_id``, agent IR carries its own ``id``, connection/rag ids are hashed IR
content — so a restored install correlates exactly like the original.

Secret hygiene is the hard invariant: no secret value, ciphertext, ``secret_ref``, webhook signing
secret, or legacy mcp env/header VALUE ever enters a bundle (keys only), and import recreates
secret-bearing resources without credentials, reporting what the user must re-enter
(``needs_secret`` / ``needs_oauth`` / ``needs_env``).

Apply semantics: id-preserving, idempotent (a re-import skips everything), skip-on-exists, one
transaction per entity so one bad entity never aborts the rest, loud per-entity reporting via the
flat ``warnings`` list. This module never commits — the caller passes its ``tx()`` factory (one
transaction per logical operation) and a read session for the export build.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from theygent_ir import GraphValidationError, content_hash, parse_document, validate_graph

from theygent_control_plane.mcp import McpServerConfig
from theygent_control_plane.models import (
    AgentDraftRow,
    AgentIoPolicyRow,
    AgentRow,
    AgentVersionRow,
    ChatSessionRow,
    ConnectionRow,
    McpServerRow,
    MessageRow,
    NodeIoRow,
    RagSourceRow,
    RunRow,
    SpanRow,
    TriggerRow,
)
from theygent_control_plane.observability.store import AgentIoPolicyStore
from theygent_control_plane.run import _SECRETISH_CONFIG_KEYS, Trigger
from theygent_control_plane.store import (
    AgentStore,
    TriggerStore,
    VersionConflict,
    _derive_draft_meta,
)

logger = logging.getLogger("theygent.control_plane.transfer")

FORMAT_VERSION = 1

#: The valid ``include`` values POST /export accepts. ``agents`` also carries drafts; ``traces``
#: carries spans + node_io + artifact_refs and implies ``runs`` (a trace without its run rows is
#: unreadable); ``mcp`` carries connections + the legacy name-keyed mcp_server registry.
INCLUDE_SECTIONS = frozenset({"agents", "runs", "traces", "sessions", "mcp", "rag"})

#: A transaction factory (the route's ``tx()``) — the apply path opens ONE per entity.
TxFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_CAPTURE_LEVELS = frozenset({"off", "metadata", "full"})
_TRIGGER_KINDS = frozenset({"http", "schedule", "webhook"})


def resolve_include(values: Any) -> set[str] | None:
    """Validate + normalize the export selection. ``None`` = invalid (empty, non-list, or an
    unknown value — the route maps it to 400 ``invalid_include``). ``traces`` implies ``runs``:
    the server adds them, so a traces-only request still yields a readable bundle."""
    if not isinstance(values, list) or not values:
        return None
    out: set[str] = set()
    for value in values:
        if not isinstance(value, str) or value not in INCLUDE_SECTIONS:
            return None
        out.add(value)
    if "traces" in out:
        out.add("runs")
    return out


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def _ts(value: Any) -> datetime:
    """Parse a bundle timestamp. Raises ``ValueError`` on anything unparseable — the per-entity
    guard turns that into a loud warning instead of a silent wrong instant."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        raise ValueError(f"not a timestamp: {value!r}")
    return datetime.fromisoformat(value)


def _opt_ts(value: Any) -> datetime | None:
    return None if value is None else _ts(value)


def _req_str(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing required field {key!r}")
    return value


def _opt_str(entry: dict[str, Any], key: str) -> str | None:
    value = entry.get(key)
    return value if isinstance(value, str) and value else None


def _strip_secretish(config: Any) -> dict[str, Any]:
    """Deep-strip credential material from a connection config, at every depth. Secret-ish keys
    are dropped outright (the API rejects them at create/patch, so this is a defensive net for
    the hard invariant: secrets never in a bundle, never re-persisted from one). ``env`` /
    ``headers`` NAME=value maps keep their KEYS with string values blanked to ``""`` — an
    mcp_server connection legitimately stores credential values there (the stdio subprocess env,
    the http/sse request headers), and the surviving keys let the import report ``needs_env``
    so the user knows exactly what to re-enter."""
    if not isinstance(config, dict):
        return {}
    sanitized = _sanitize_config_node(config)
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_config_node(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in _SECRETISH_CONFIG_KEYS:
                continue
            if key in ("env", "headers") and isinstance(item, dict):
                # Keys survive so the shape imports cleanly; every string value is credential
                # material and is blanked (never dropped to ``***`` — a real-looking value
                # re-imported would silently "work" until the server rejects it).
                out[key] = {
                    k: ("" if isinstance(v, str) else _sanitize_config_node(v))
                    for k, v in item.items()
                }
            else:
                out[key] = _sanitize_config_node(item)
        return out
    if isinstance(value, list):
        return [_sanitize_config_node(item) for item in value]
    return value


def _needs_env_reentry(config: Any) -> bool:
    """Whether a (sanitized) connection config carries an ``env``/``headers`` map with blanked
    values — the signal the import report echoes as ``needs_env`` (the user re-enters them)."""
    if isinstance(config, dict):
        for key, item in config.items():
            if (
                key in ("env", "headers")
                and isinstance(item, dict)
                and any(isinstance(v, str) and not v for v in item.values())
            ):
                return True
            if _needs_env_reentry(item):
                return True
        return False
    if isinstance(config, list):
        return any(_needs_env_reentry(item) for item in config)
    return False


def _collect_artifact_refs(value: Any, out: dict[str, str | None]) -> None:
    """Walk a payload for ``{"ref": "art_...", ...}`` shapes — the only portable artifact
    reference form (urls/paths are machine-specific and stay behind)."""
    if isinstance(value, dict):
        ref = value.get("ref")
        if isinstance(ref, str) and ref.startswith("art_"):
            content_type = value.get("contentType")
            out.setdefault(ref, content_type if isinstance(content_type, str) else None)
        for nested in value.values():
            _collect_artifact_refs(nested, out)
    elif isinstance(value, list):
        for nested in value:
            _collect_artifact_refs(nested, out)


# ── export (bundle build) ────────────────────────────────────────────────────────────────────────


async def build_export_bundle(session: AsyncSession, *, include: set[str]) -> dict[str, Any]:
    """Build the control-plane bundle for a validated ``include`` set. Read-only; only requested
    sections are present. Ordering is deterministic ((created_at, id) ascending; versions by seq,
    messages by position, spans by (run_id, seq)) so a re-export of identical state is
    byte-comparable."""
    bundle: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
    }
    if "agents" in include:
        bundle["agents"] = await _export_agents(session)
        bundle["drafts"] = await _export_drafts(session)
    if "runs" in include:
        bundle["runs"] = await _export_runs(session)
    if "traces" in include:
        spans, node_io, artifact_refs = await _export_traces(session)
        bundle["spans"] = spans
        bundle["node_io"] = node_io
        bundle["artifact_refs"] = artifact_refs
    if "sessions" in include:
        bundle["sessions"] = await _export_sessions(session)
    if "mcp" in include:
        bundle["connections"] = await _export_connections(session)
        bundle["mcp_servers"] = await _export_mcp_servers(session)
    if "rag" in include:
        bundle["rag_sources"] = await _export_rag_sources(session)
    return bundle


async def _export_agents(session: AsyncSession) -> list[dict[str, Any]]:
    agents = (
        (await session.execute(select(AgentRow).order_by(AgentRow.created_at, AgentRow.id)))
        .scalars()
        .all()
    )
    out: list[dict[str, Any]] = []
    for agent in agents:
        # Versions in seq order (oldest first) so an import re-allocates seq in the same order
        # and "latest" stays the same version on the target.
        versions = (
            (
                await session.execute(
                    select(AgentVersionRow)
                    .where(AgentVersionRow.agent_id == agent.id)
                    .order_by(AgentVersionRow.seq)
                )
            )
            .scalars()
            .all()
        )
        policy = await session.get(AgentIoPolicyRow, agent.id)
        triggers = (
            (
                await session.execute(
                    select(TriggerRow)
                    .where(TriggerRow.agent_id == agent.id)
                    .order_by(TriggerRow.created_at, TriggerRow.id)
                )
            )
            .scalars()
            .all()
        )
        out.append(
            {
                "id": agent.id,
                "name": agent.name,
                "created_at": _iso(agent.created_at),
                "updated_at": _iso(agent.updated_at),
                "versions": [
                    {
                        "version": v.version,
                        "content_hash": v.content_hash,
                        "created_at": _iso(v.created_at),
                        "ir": dict(v.ir),
                        "view": dict(v.view) if v.view is not None else None,
                    }
                    for v in versions
                ],
                "io_policy": (
                    {
                        "io_capture": policy.io_capture,
                        "io_retention_seconds": policy.io_retention_seconds,
                        "redact_rules": policy.redact_rules,
                    }
                    if policy is not None
                    else None
                ),
                "triggers": [
                    {
                        "id": t.id,
                        "kind": t.kind,
                        "version": t.version,
                        "content_hash": t.content_hash,
                        # A webhook's signing secret is credential material stored in the config
                        # JSONB — it never enters a bundle (the user sets a fresh one on import).
                        "config": {k: v for k, v in (t.config or {}).items() if k != "secret"},
                        "enabled": t.enabled,
                        "created_at": _iso(t.created_at),
                    }
                    for t in triggers
                ],
            }
        )
    return out


async def _export_drafts(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(AgentDraftRow).order_by(AgentDraftRow.created_at, AgentDraftRow.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": d.id,
            "agent_id": d.agent_id,
            "name": d.name,
            "ir": dict(d.ir or {}),
            "view": dict(d.view) if d.view is not None else None,
            "created_at": _iso(d.created_at),
            "updated_at": _iso(d.updated_at),
        }
        for d in rows
    ]


async def _export_runs(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        (await session.execute(select(RunRow).order_by(RunRow.created_at, RunRow.id)))
        .scalars()
        .all()
    )
    # The full row — ``params`` is on no public Run response, which is why export reads the DB.
    return [
        {
            "id": r.id,
            "session_id": r.session_id,
            "status": r.status,
            "model": r.model,
            "params": r.params,
            "graph_id": r.graph_id,
            "graph_version": r.graph_version,
            "content_hash": r.content_hash,
            "trigger_id": r.trigger_id,
            "error": r.error,
            "output": r.output,
            "awaiting_node": r.awaiting_node,
            "runtime": r.runtime,
            "created_at": _iso(r.created_at),
            "updated_at": _iso(r.updated_at),
            "completed_at": _iso(r.completed_at),
        }
        for r in rows
    ]


async def _export_traces(
    session: AsyncSession,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    spans = (
        (await session.execute(select(SpanRow).order_by(SpanRow.run_id, SpanRow.seq)))
        .scalars()
        .all()
    )
    node_io = (
        (await session.execute(select(NodeIoRow).order_by(NodeIoRow.run_id, NodeIoRow.node_id)))
        .scalars()
        .all()
    )
    refs: dict[str, str | None] = {}
    span_out = [
        {
            "id": s.id,
            "run_id": s.run_id,
            "trace_id": s.trace_id,
            "otel_span_id": s.otel_span_id,
            "parent_span_id": s.parent_span_id,
            "node_id": s.node_id,
            "node_type": s.node_type,
            "kind": s.kind,
            "name": s.name,
            "phase": s.phase,
            "branch_index": s.branch_index,
            "status": s.status,
            "start_ns": s.start_ns,
            "end_ns": s.end_ns,
            "attributes": dict(s.attributes) if s.attributes else None,
            "error": s.error,
            "executor_id": s.executor_id,
            "worker_host": s.worker_host,
            "seq": s.seq,
            "created_at": _iso(s.created_at),
        }
        for s in spans
    ]
    io_out: list[dict[str, Any]] = []
    for row in node_io:
        _collect_artifact_refs(row.inputs, refs)
        _collect_artifact_refs(row.outputs, refs)
        io_out.append(
            {
                "id": row.id,
                "run_id": row.run_id,
                "node_id": row.node_id,
                "span_id": row.span_id,
                "inputs": dict(row.inputs) if row.inputs is not None else None,
                "outputs": dict(row.outputs) if row.outputs is not None else None,
                "bytes_in": row.bytes_in,
                "bytes_out": row.bytes_out,
                "truncated": row.truncated,
                "capture_level": row.capture_level,
                "created_at": _iso(row.created_at),
            }
        )
    # ``run.output`` is a string on the row, but a graph's output node may have bound a reference
    # dict (speak/imagine) serialized to JSON — parse and walk it too, so exported audio/image
    # runs list the files the client must pack.
    outputs = (
        await session.execute(select(RunRow.output).where(RunRow.output.is_not(None)))
    ).scalars()
    for output in outputs:
        try:
            parsed = json.loads(output)
        except (TypeError, ValueError):
            continue
        _collect_artifact_refs(parsed, refs)
    artifact_refs = [
        {"ref": ref, "content_type": content_type} for ref, content_type in sorted(refs.items())
    ]
    return span_out, io_out, artifact_refs


async def _export_sessions(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(ChatSessionRow).order_by(ChatSessionRow.created_at, ChatSessionRow.id)
            )
        )
        .scalars()
        .all()
    )
    out: list[dict[str, Any]] = []
    for chat in rows:
        messages = (
            (
                await session.execute(
                    select(MessageRow)
                    .where(MessageRow.session_id == chat.id)
                    .order_by(MessageRow.position)
                )
            )
            .scalars()
            .all()
        )
        out.append(
            {
                "id": chat.id,
                "created_at": _iso(chat.created_at),
                "updated_at": _iso(chat.updated_at),
                "metadata": chat.meta,
                "messages": [
                    {
                        "id": m.id,
                        "run_id": m.run_id,
                        "role": m.role,
                        "content": m.content,
                        "position": m.position,
                        "created_at": _iso(m.created_at),
                    }
                    for m in messages
                ],
            }
        )
    return out


async def _export_connections(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(ConnectionRow).order_by(ConnectionRow.created_at, ConnectionRow.id)
            )
        )
        .scalars()
        .all()
    )
    # The FULL stored config (including the openapi ``spec`` the public wire elides) — that is
    # the point of the DB-level read. ``secret_ref`` never leaves; ``has_secret`` is the honest
    # signal the import report echoes back as ``needs_secret``.
    return [
        {
            "id": c.id,
            "name": c.name,
            "kind": c.kind,
            "config": _strip_secretish(c.config),
            "enabled": c.enabled,
            "has_secret": c.secret_ref is not None,
            "created_at": _iso(c.created_at),
            "updated_at": _iso(c.updated_at),
        }
        for c in rows
    ]


async def _export_mcp_servers(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(select(McpServerRow).order_by(McpServerRow.name))).scalars().all()
    # env/header VALUES are the user's credential material stored raw in the row — the bundle
    # carries the KEYS only (the shape the /admin view already exposes).
    return [
        {
            "name": r.name,
            "transport": r.transport,
            "command": r.command,
            "args": list(r.args or []),
            "env_keys": sorted(r.env or {}),
            "cwd": r.cwd,
            "url": r.url,
            "header_keys": sorted(r.headers or {}),
            "created_at": _iso(r.created_at),
            "updated_at": _iso(r.updated_at),
        }
        for r in rows
    ]


async def _export_rag_sources(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(RagSourceRow).order_by(RagSourceRow.created_at, RagSourceRow.id)
            )
        )
        .scalars()
        .all()
    )
    # Definition only: status/progress/error/embedding_dim are runtime state the target
    # re-discovers on its own ingest, and documents/chunks stay behind (crawl sources re-crawl;
    # upload sources are reported ``needs_upload``).
    return [
        {
            "id": s.id,
            "name": s.name,
            "kind": s.kind,
            "embedding_model": s.embedding_model,
            "config": dict(s.config or {}),
            "created_at": _iso(s.created_at),
            "updated_at": _iso(s.updated_at),
        }
        for s in rows
    ]


# ── import (bundle apply) ────────────────────────────────────────────────────────────────────────


async def apply_import_bundle(
    bundle: dict[str, Any],
    *,
    tx: TxFactory,
    agents: AgentStore,
    triggers: TriggerStore,
    policy_store: AgentIoPolicyStore,
    sync_schedule: Callable[[Trigger], Awaitable[None]],
    register_mcp: Callable[[str, McpServerConfig], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Apply a (version-checked) bundle. Sections are optional; each present section lands with
    its own per-entity transactions and contributes a counts block to the report. FK order is
    fixed here regardless of the bundle's key order: agents → drafts → session rows → runs →
    spans/node_io → messages → connections → mcp_servers → rag_sources (messages come after runs
    so ``message.run_id`` resolves; runs come after session rows so ``run.session_id`` does).
    A section present with the wrong JSON type is skipped LOUDLY (``invalid_section``) — a
    malformed bundle never 500s and never silently drops data."""
    report: dict[str, Any] = {}
    warnings: list[dict[str, Any]] = []

    agents_section = _section(bundle, "agents", warnings)
    if agents_section is not None:
        report["agents"] = await _import_agents(
            agents_section,
            warnings,
            tx=tx,
            agents=agents,
            triggers=triggers,
            policy_store=policy_store,
            sync_schedule=sync_schedule,
        )
    drafts_section = _section(bundle, "drafts", warnings)
    if drafts_section is not None:
        report["drafts"] = await _import_drafts(drafts_section, warnings, tx=tx)

    sessions_section = _section(bundle, "sessions", warnings)
    new_sessions: list[dict[str, Any]] = []
    sessions_report: dict[str, Any] | None = None
    if sessions_section is not None:
        sessions_report, new_sessions = await _import_session_rows(
            sessions_section, warnings, tx=tx
        )

    runs_section = _section(bundle, "runs", warnings)
    if runs_section is not None:
        report["runs"] = await _import_runs(runs_section, warnings, tx=tx)
    spans_section = _section(bundle, "spans", warnings)
    if spans_section is not None:
        report["spans"] = await _import_spans(spans_section, warnings, tx=tx)
    node_io_section = _section(bundle, "node_io", warnings)
    if node_io_section is not None:
        report["node_io"] = await _import_node_io(node_io_section, warnings, tx=tx)

    if sessions_report is not None:
        # Messages land AFTER runs so a bundled run reference resolves instead of detaching.
        await _import_messages(new_sessions, sessions_report, warnings, tx=tx)
        report["sessions"] = sessions_report

    connections_section = _section(bundle, "connections", warnings)
    if connections_section is not None:
        report["connections"] = await _import_connections(connections_section, warnings, tx=tx)
    mcp_section = _section(bundle, "mcp_servers", warnings)
    if mcp_section is not None:
        report["mcp_servers"] = await _import_mcp_servers(
            mcp_section, warnings, tx=tx, register_mcp=register_mcp
        )
    rag_section = _section(bundle, "rag_sources", warnings)
    if rag_section is not None:
        report["rag_sources"] = await _import_rag_sources(rag_section, warnings, tx=tx)

    report["warnings"] = warnings
    return report


def _section(bundle: dict[str, Any], name: str, warnings: list[dict[str, Any]]) -> list[Any] | None:
    """A bundle section as a list, or ``None``. Absent is fine (sections are optional); present
    with the wrong JSON type warns ``invalid_section`` and skips — never a 500 (the hostile-shape
    posture), never a silent drop (the loud-failure rule)."""
    value = bundle.get(name)
    if value is None:
        return None
    if not isinstance(value, list):
        warnings.append(
            {
                "code": "invalid_section",
                "section": name,
                "message": f"section {name!r} must be a JSON array, "
                f"got {type(value).__name__}; section skipped",
            }
        )
        return None
    return value


def _failed(section: str, ident: Any, exc: Exception) -> dict[str, Any]:
    """The loud per-entity failure entry — one bad entity is reported, never silently dropped
    (and never aborts the rest: each entity rides its own transaction)."""
    return {"code": "import_failed", "section": section, "id": ident, "message": str(exc)}


async def _import_agents(
    section: list[Any],
    warnings: list[dict[str, Any]],
    *,
    tx: TxFactory,
    agents: AgentStore,
    triggers: TriggerStore,
    policy_store: AgentIoPolicyStore,
    sync_schedule: Callable[[Trigger], Awaitable[None]],
) -> dict[str, Any]:
    counts = {
        "created": 0,
        "existing": 0,
        "versions_created": 0,
        "versions_existing": 0,
        "io_policies": 0,
        "io_policies_skipped": 0,
        "triggers_created": 0,
        "triggers_skipped": 0,
    }
    for entry in section:
        if not isinstance(entry, dict):
            warnings.append(_failed("agents", None, ValueError("agent entry is not an object")))
            continue
        try:
            agent_id = _req_str(entry, "id")
            async with tx() as session:
                if await agents.get_agent(session, agent_id) is None:
                    created_at = _ts(entry.get("created_at"))
                    session.add(
                        AgentRow(
                            id=agent_id,
                            name=str(entry.get("name") or agent_id),
                            created_at=created_at,
                            updated_at=_opt_ts(entry.get("updated_at")) or created_at,
                        )
                    )
                    await session.flush()
                    counts["created"] += 1
                else:
                    counts["existing"] += 1
        except Exception as exc:
            warnings.append(_failed("agents", entry.get("id"), exc))
            continue

        # Sub-lists get the same hostile-shape guard as top-level sections: a non-list value
        # (``"versions": 5``) warns and skips — iterating it raw would TypeError past every
        # per-entity guard and 500 the whole import mid-way with partial commits.
        versions = entry.get("versions")
        if versions is not None and not isinstance(versions, list):
            warnings.append(
                _failed("agents", agent_id, ValueError("agent 'versions' must be a JSON array"))
            )
            versions = None
        for version_entry in versions or []:
            await _import_version(agent_id, version_entry, counts, warnings, tx=tx, agents=agents)

        policy = entry.get("io_policy")
        if isinstance(policy, dict):
            await _import_io_policy(
                agent_id, policy, counts, warnings, tx=tx, policy_store=policy_store
            )
        elif policy is not None:
            warnings.append(
                _failed("agents", agent_id, ValueError("agent 'io_policy' must be a JSON object"))
            )

        triggers_list = entry.get("triggers")
        if triggers_list is not None and not isinstance(triggers_list, list):
            warnings.append(
                _failed("agents", agent_id, ValueError("agent 'triggers' must be a JSON array"))
            )
            triggers_list = None
        for trigger_entry in triggers_list or []:
            await _import_trigger(
                agent_id,
                trigger_entry,
                counts,
                warnings,
                tx=tx,
                agents=agents,
                triggers=triggers,
                sync_schedule=sync_schedule,
            )
    return counts


async def _import_version(
    agent_id: str,
    entry: Any,
    counts: dict[str, Any],
    warnings: list[dict[str, Any]],
    *,
    tx: TxFactory,
    agents: AgentStore,
) -> None:
    if not isinstance(entry, dict):
        warnings.append(_failed("agents", agent_id, ValueError("version entry is not an object")))
        return
    version = entry.get("version")
    try:
        # The publish gate, unchanged: an invalid document never enters the registry — and the
        # hash is recomputed server-side (never trusted from the bundle), same as POST /agents.
        ir = parse_document(entry.get("ir"))
        validate_graph(ir)
    except (ValidationError, GraphValidationError) as exc:
        warnings.append(
            {"code": "invalid_ir", "agent_id": agent_id, "version": version, "message": str(exc)}
        )
        return
    chash = content_hash(ir)
    doc = ir.model_dump(mode="json", by_alias=True, exclude_none=False)
    doc.pop("view", None)
    doc["contentHash"] = chash  # the stored doc is self-describing (the registry's discipline)
    view = entry.get("view")
    try:
        async with tx() as session:
            _meta, created = await agents.add_version(
                session,
                agent_id=agent_id,
                version=str(version),
                content_hash=chash,
                ir=doc,
                view=view if isinstance(view, dict) else None,
                created_at=_opt_ts(entry.get("created_at")),
            )
        counts["versions_created" if created else "versions_existing"] += 1
    except VersionConflict as exc:
        # The stored hash may predate a schema change, so hash inequality alone cannot prove
        # different content: re-hash the TARGET's stored document under today's schema and
        # compare like with like. Equal → the bundle carries content the target already has
        # (an idempotent re-import), not a conflict.
        async with tx() as session:
            existing = await agents.get_version(session, agent_id, str(version))
        if existing is not None:
            try:
                if content_hash(parse_document(existing.ir)) == chash:
                    counts["versions_existing"] += 1
                    return
            except (ValidationError, GraphValidationError):
                pass  # unparseable stored doc: fall through to the honest conflict report
        # Immutability holds on import too: same (agent, version), different content — report
        # and continue with the rest of the bundle.
        warnings.append(
            {
                "code": "version_conflict",
                "agent_id": agent_id,
                "version": version,
                "message": str(exc),
            }
        )
        return
    except Exception as exc:
        warnings.append(_failed("agents", f"{agent_id}@{version}", exc))
        return
    bundled_hash = entry.get("content_hash")
    if isinstance(bundled_hash, str) and bundled_hash and bundled_hash != chash:
        # A legitimate re-hash under a newer IR schema (defaulted-field additions shift hashes).
        # The version imports anyway — pins by the OLD hash in the same bundle will not resolve,
        # so say it loudly.
        warnings.append(
            {
                "code": "content_hash_changed",
                "agent_id": agent_id,
                "version": version,
                "bundled": bundled_hash,
                "recomputed": chash,
                "message": "recomputed content_hash differs from the bundled one (IR schema "
                "drift); the version was imported under the recomputed hash",
            }
        )


async def _import_io_policy(
    agent_id: str,
    policy: dict[str, Any],
    counts: dict[str, Any],
    warnings: list[dict[str, Any]],
    *,
    tx: TxFactory,
    policy_store: AgentIoPolicyStore,
) -> None:
    io_capture = policy.get("io_capture")
    if io_capture not in _CAPTURE_LEVELS:
        warnings.append(
            _failed("agents", agent_id, ValueError(f"unknown io_capture {io_capture!r}"))
        )
        return
    retention = policy.get("io_retention_seconds")
    redact = policy.get("redact_rules")
    try:
        async with tx() as session:
            # Skip-on-exists, like every other import write: the io policy is the agent owner's
            # LIVE privacy control (capture level + redact rules) — a bundle must never flip it
            # under them. Only an agent with no policy row on the target takes the bundled one.
            if await policy_store.get_policy(session, agent_id) is not None:
                counts["io_policies_skipped"] += 1
                return
            await policy_store.upsert_policy(
                session,
                agent_id=agent_id,
                io_capture=io_capture,
                io_retention_seconds=retention if isinstance(retention, int) else None,
                redact_rules=redact if isinstance(redact, dict) else None,
                updated_by=None,
            )
        counts["io_policies"] += 1
    except Exception as exc:
        warnings.append(_failed("agents", agent_id, exc))


async def _import_trigger(
    agent_id: str,
    entry: Any,
    counts: dict[str, Any],
    warnings: list[dict[str, Any]],
    *,
    tx: TxFactory,
    agents: AgentStore,
    triggers: TriggerStore,
    sync_schedule: Callable[[Trigger], Awaitable[None]],
) -> None:
    if not isinstance(entry, dict):
        warnings.append(_failed("agents", agent_id, ValueError("trigger entry is not an object")))
        return
    trigger_id = entry.get("id")
    kind = entry.get("kind")
    created: Trigger | None = None
    try:
        if kind not in _TRIGGER_KINDS:
            raise ValueError(f"unknown trigger kind {kind!r}")
        async with tx() as session:
            if (
                isinstance(trigger_id, str)
                and trigger_id
                and await triggers.get(session, trigger_id) is not None
            ):
                counts["triggers_skipped"] += 1
                return
            # The pin must resolve against the TARGET's versions (they may have re-hashed on
            # import) — an unresolvable pin is skipped loudly, never registered dangling.
            version_pin = _opt_str(entry, "version")
            hash_pin = _opt_str(entry, "content_hash")
            if hash_pin:
                resolved = await agents.get_version_by_hash(session, agent_id, hash_pin)
            elif version_pin:
                resolved = await agents.get_version(session, agent_id, version_pin)
            else:
                resolved = None
            if resolved is None:
                counts["triggers_skipped"] += 1
                warnings.append(
                    {
                        "code": "trigger_pin_unresolved",
                        "trigger_id": trigger_id,
                        "agent_id": agent_id,
                        "message": f"pin ({version_pin or hash_pin!r}) does not resolve against "
                        "the imported agent's versions; trigger skipped",
                    }
                )
                return
            config = {k: v for k, v in dict(entry.get("config") or {}).items() if k != "secret"}
            enabled = bool(entry.get("enabled", True))
            if kind == "webhook":
                # The signing secret never traveled — an armed webhook with no secret would
                # reject every caller, so it lands disabled until the user sets a fresh one.
                enabled = False
            created = await triggers.create(
                session,
                agent_id=agent_id,
                kind=kind,
                version=version_pin,
                content_hash=hash_pin,
                config=config,
                enabled=enabled,
                trigger_id=trigger_id if isinstance(trigger_id, str) and trigger_id else None,
                created_at=_opt_ts(entry.get("created_at")),
            )
    except Exception as exc:
        warnings.append(_failed("agents", trigger_id, exc))
        return
    counts["triggers_created"] += 1
    if created.kind == "webhook":
        warnings.append(
            {
                "code": "needs_secret",
                "trigger_id": created.id,
                "message": "webhook trigger imported disabled and without its signing secret — "
                "set a new one via PATCH /triggers/{id}, then enable it",
            }
        )
    # The same mirror step POST /triggers runs, so an enabled schedule lands as a DBOS dynamic
    # schedule in durable mode. A mirror failure is reported, not fatal — the trigger row is the
    # source of truth and the boot reconcile re-aligns schedules.
    try:
        await sync_schedule(created)
    except Exception as exc:
        warnings.append(_failed("agents", created.id, exc))


async def _import_drafts(
    section: list[Any], warnings: list[dict[str, Any]], *, tx: TxFactory
) -> dict[str, Any]:
    counts = {"created": 0, "skipped": 0}
    for entry in section:
        try:
            if not isinstance(entry, dict):
                raise ValueError("draft entry is not an object")
            draft_id = _req_str(entry, "id")
            ir = entry.get("ir")
            if not isinstance(ir, dict):
                raise ValueError("draft ir must be a JSON object")
            view = entry.get("view")
            # Drafts are the unvalidated tier — the ir is stored as-is, never parsed/hashed;
            # only the list-view labels are re-derived (the same helper every draft write uses).
            name, node_count = _derive_draft_meta(ir)
            async with tx() as session:
                if await session.get(AgentDraftRow, draft_id) is not None:
                    counts["skipped"] += 1
                    continue
                created_at = _ts(entry.get("created_at"))
                session.add(
                    AgentDraftRow(
                        id=draft_id,
                        agent_id=_opt_str(entry, "agent_id"),
                        owner_id=None,
                        name=name,
                        node_count=node_count,
                        ir=ir,
                        view=view if isinstance(view, dict) else None,
                        created_at=created_at,
                        updated_at=_opt_ts(entry.get("updated_at")) or created_at,
                    )
                )
                counts["created"] += 1
        except Exception as exc:
            warnings.append(
                _failed("drafts", entry.get("id") if isinstance(entry, dict) else None, exc)
            )
    return counts


async def _import_session_rows(
    section: list[Any], warnings: list[dict[str, Any]], *, tx: TxFactory
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Phase one of the session import: the ``chat_session`` rows only, so runs importing next
    can keep their ``session_id``. Messages follow in :func:`_import_messages` (after runs, so
    ``message.run_id`` resolves). Skip-on-exists is whole-session: an existing id keeps its
    messages untouched (merging positions into a live session would collide)."""
    counts = {"created": 0, "skipped": 0, "messages_created": 0, "detached_run_refs": 0}
    new_sessions: list[dict[str, Any]] = []
    for entry in section:
        try:
            if not isinstance(entry, dict):
                raise ValueError("session entry is not an object")
            session_id = _req_str(entry, "id")
            async with tx() as session:
                if await session.get(ChatSessionRow, session_id) is not None:
                    counts["skipped"] += 1
                    continue
                created_at = _ts(entry.get("created_at"))
                metadata = entry.get("metadata")
                session.add(
                    ChatSessionRow(
                        id=session_id,
                        created_at=created_at,
                        updated_at=_opt_ts(entry.get("updated_at")) or created_at,
                        meta=metadata if isinstance(metadata, dict) else None,
                    )
                )
                counts["created"] += 1
            new_sessions.append(entry)
        except Exception as exc:
            warnings.append(
                _failed("sessions", entry.get("id") if isinstance(entry, dict) else None, exc)
            )
    return counts, new_sessions


async def _existing_run_ids(session: AsyncSession, candidates: set[str]) -> set[str]:
    if not candidates:
        return set()
    rows = (await session.execute(select(RunRow.id).where(RunRow.id.in_(candidates)))).scalars()
    return set(rows)


async def _import_messages(
    new_sessions: list[dict[str, Any]],
    counts: dict[str, Any],
    warnings: list[dict[str, Any]],
    *,
    tx: TxFactory,
) -> None:
    """Phase two of the session import: one transaction per session's messages. A failure is
    recoverable, not permanent: every message is shape-validated BEFORE the transaction opens,
    counts move only AFTER it commits (a rolled-back insert is never reported created), and a
    session whose messages could not land is deleted again (:func:`_compensate_session`) so a
    corrected re-import retries instead of hitting whole-session skip-on-exists forever."""
    for entry in new_sessions:
        session_id = entry.get("id")
        messages = entry.get("messages")
        if messages is None or (isinstance(messages, list) and not messages):
            continue
        if not isinstance(messages, list):
            warnings.append(
                _failed(
                    "sessions", session_id, ValueError("session 'messages' must be a JSON array")
                )
            )
            await _compensate_session(session_id, counts, warnings, tx=tx)
            continue
        try:
            # Pre-validate the whole batch up front: a malformed message fails the session with
            # NOTHING written (the alternative — failing mid-transaction — rolled back siblings
            # that were already counted).
            prepared: list[dict[str, Any]] = []
            for message in messages:
                if not isinstance(message, dict):
                    raise ValueError("message entry is not an object")
                prepared.append(
                    {
                        "id": _req_str(message, "id"),
                        "run_id": message.get("run_id")
                        if isinstance(message.get("run_id"), str)
                        else None,
                        "role": _req_str(message, "role"),
                        "content": str(message.get("content") or ""),
                        "position": int(message["position"]),
                        "created_at": _ts(message.get("created_at")),
                    }
                )
        except Exception as exc:
            warnings.append(_failed("sessions", session_id, exc))
            await _compensate_session(session_id, counts, warnings, tx=tx)
            continue
        run_refs = {m["run_id"] for m in prepared if m["run_id"] is not None}
        created = 0
        detached = 0
        try:
            async with tx() as session:
                known_runs = await _existing_run_ids(session, run_refs)
                for message in prepared:
                    run_id = message["run_id"]
                    if run_id is not None and run_id not in known_runs:
                        # The run behind this turn is not on the target (runs weren't in the
                        # bundle, or that row failed) — keep the turn, drop the dangling FK.
                        run_id = None
                        detached += 1
                    result = await session.execute(
                        pg_insert(MessageRow)
                        .values(
                            id=message["id"],
                            session_id=session_id,
                            run_id=run_id,
                            role=message["role"],
                            content=message["content"],
                            position=message["position"],
                            created_at=message["created_at"],
                        )
                        .on_conflict_do_nothing(index_elements=[MessageRow.id])
                    )
                    created += int(result.rowcount or 0)
        except Exception as exc:
            warnings.append(_failed("sessions", session_id, exc))
            await _compensate_session(session_id, counts, warnings, tx=tx)
            continue
        counts["messages_created"] += created
        counts["detached_run_refs"] += detached


async def _compensate_session(
    session_id: Any,
    counts: dict[str, Any],
    warnings: list[dict[str, Any]],
    *,
    tx: TxFactory,
) -> None:
    """Undo the session row THIS import created after its messages failed to land. Leaving the
    empty row would make the loss permanent (a corrected re-import hits whole-session
    skip-on-exists); deleting it re-opens the id for retry. Runs that imported against it are
    detached, never deleted — the DELETE /sessions posture."""
    if not isinstance(session_id, str) or not session_id:
        return
    try:
        async with tx() as session:
            await session.execute(
                update(RunRow).where(RunRow.session_id == session_id).values(session_id=None)
            )
            await session.execute(delete(MessageRow).where(MessageRow.session_id == session_id))
            await session.execute(delete(ChatSessionRow).where(ChatSessionRow.id == session_id))
        counts["created"] -= 1
    except Exception as exc:
        warnings.append(_failed("sessions", session_id, exc))


async def _import_runs(
    section: list[Any], warnings: list[dict[str, Any]], *, tx: TxFactory
) -> dict[str, Any]:
    counts = {"created": 0, "skipped": 0, "detached_sessions": 0}
    for entry in section:
        try:
            if not isinstance(entry, dict):
                raise ValueError("run entry is not an object")
            run_id = _req_str(entry, "id")
            async with tx() as session:
                if await session.get(RunRow, run_id) is not None:
                    counts["skipped"] += 1
                    continue
                session_id = _opt_str(entry, "session_id")
                if session_id is not None and (
                    await session.get(ChatSessionRow, session_id) is None
                ):
                    # No such session on the target — a run's history survives detached, the
                    # same posture as DELETE /sessions.
                    session_id = None
                    counts["detached_sessions"] += 1
                created_at = _ts(entry.get("created_at"))
                # The row verbatim: the id MUST be preserved (span PKs and derived otel ids
                # embed it), and status/params/output are history, not re-derived state.
                session.add(
                    RunRow(
                        id=run_id,
                        session_id=session_id,
                        status=_req_str(entry, "status"),
                        model=str(entry.get("model") or ""),
                        params=entry.get("params"),
                        graph_id=_opt_str(entry, "graph_id"),
                        graph_version=_opt_str(entry, "graph_version"),
                        content_hash=_opt_str(entry, "content_hash"),
                        trigger_id=_opt_str(entry, "trigger_id"),
                        error=_opt_str(entry, "error"),
                        output=entry.get("output")
                        if isinstance(entry.get("output"), str)
                        else None,
                        awaiting_node=_opt_str(entry, "awaiting_node"),
                        runtime=_opt_str(entry, "runtime"),
                        created_at=created_at,
                        updated_at=_opt_ts(entry.get("updated_at")) or created_at,
                        completed_at=_opt_ts(entry.get("completed_at")),
                    )
                )
                counts["created"] += 1
        except Exception as exc:
            warnings.append(
                _failed("runs", entry.get("id") if isinstance(entry, dict) else None, exc)
            )
    return counts


async def _import_spans(
    section: list[Any], warnings: list[dict[str, Any]], *, tx: TxFactory
) -> dict[str, Any]:
    counts = {"created": 0, "skipped": 0, "skipped_missing_run": 0}
    run_refs = {
        e.get("run_id") for e in section if isinstance(e, dict) and isinstance(e.get("run_id"), str)
    }
    async with tx() as session:
        known_runs = await _existing_run_ids(session, run_refs)
    for entry in section:
        try:
            if not isinstance(entry, dict):
                raise ValueError("span entry is not an object")
            run_id = _req_str(entry, "run_id")
            if run_id not in known_runs:
                counts["skipped_missing_run"] += 1
                continue
            async with tx() as session:
                # The same first-writer-wins conflict shape TraceStore.write_span uses — a
                # re-import is a clean no-op; seq/ids/created_at are preserved, never re-minted.
                result = await session.execute(
                    pg_insert(SpanRow)
                    .values(
                        id=_req_str(entry, "id"),
                        run_id=run_id,
                        trace_id=_req_str(entry, "trace_id"),
                        otel_span_id=_req_str(entry, "otel_span_id"),
                        parent_span_id=_opt_str(entry, "parent_span_id"),
                        node_id=_opt_str(entry, "node_id"),
                        node_type=_opt_str(entry, "node_type"),
                        kind=_opt_str(entry, "kind"),
                        name=str(entry.get("name") or ""),
                        phase=_opt_str(entry, "phase"),
                        branch_index=(
                            int(entry["branch_index"])
                            if entry.get("branch_index") is not None
                            else None
                        ),
                        status=_req_str(entry, "status"),
                        start_ns=int(entry["start_ns"]),
                        end_ns=int(entry["end_ns"]) if entry.get("end_ns") is not None else None,
                        attributes=(
                            entry.get("attributes")
                            if isinstance(entry.get("attributes"), dict)
                            else None
                        ),
                        error=_opt_str(entry, "error"),
                        executor_id=_opt_str(entry, "executor_id"),
                        worker_host=_opt_str(entry, "worker_host"),
                        seq=int(entry["seq"]),
                        created_at=_ts(entry.get("created_at")),
                    )
                    .on_conflict_do_nothing(index_elements=[SpanRow.id])
                )
            counts["created" if result.rowcount else "skipped"] += 1
        except Exception as exc:
            warnings.append(
                _failed("spans", entry.get("id") if isinstance(entry, dict) else None, exc)
            )
    return counts


async def _import_node_io(
    section: list[Any], warnings: list[dict[str, Any]], *, tx: TxFactory
) -> dict[str, Any]:
    counts = {"created": 0, "skipped": 0, "skipped_missing_run": 0}
    run_refs = {
        e.get("run_id") for e in section if isinstance(e, dict) and isinstance(e.get("run_id"), str)
    }
    async with tx() as session:
        known_runs = await _existing_run_ids(session, run_refs)
    for entry in section:
        try:
            if not isinstance(entry, dict):
                raise ValueError("node_io entry is not an object")
            run_id = _req_str(entry, "run_id")
            if run_id not in known_runs:
                counts["skipped_missing_run"] += 1
                continue
            async with tx() as session:
                # ``capture_level``/``truncated`` round-trip as-is: a metadata-level or capped
                # row is honestly incomplete and must keep reporting so on the target. The
                # (run_id, node_id) conflict is NodeIoStore.write_io's idempotency guard.
                result = await session.execute(
                    pg_insert(NodeIoRow)
                    .values(
                        id=_req_str(entry, "id"),
                        run_id=run_id,
                        node_id=_req_str(entry, "node_id"),
                        span_id=_opt_str(entry, "span_id"),
                        inputs=entry.get("inputs")
                        if isinstance(entry.get("inputs"), dict)
                        else None,
                        outputs=(
                            entry.get("outputs") if isinstance(entry.get("outputs"), dict) else None
                        ),
                        bytes_in=int(entry.get("bytes_in") or 0),
                        bytes_out=int(entry.get("bytes_out") or 0),
                        truncated=bool(entry.get("truncated", False)),
                        capture_level=_req_str(entry, "capture_level"),
                        created_at=_ts(entry.get("created_at")),
                    )
                    .on_conflict_do_nothing(index_elements=[NodeIoRow.run_id, NodeIoRow.node_id])
                )
            counts["created" if result.rowcount else "skipped"] += 1
        except Exception as exc:
            warnings.append(
                _failed("node_io", entry.get("id") if isinstance(entry, dict) else None, exc)
            )
    return counts


async def _import_connections(
    section: list[Any], warnings: list[dict[str, Any]], *, tx: TxFactory
) -> dict[str, Any]:
    counts: dict[str, Any] = {
        "created": 0,
        "skipped": 0,
        "needs_secret": [],
        "needs_oauth": [],
        "needs_env": [],
    }
    for entry in section:
        try:
            if not isinstance(entry, dict):
                raise ValueError("connection entry is not an object")
            connection_id = _req_str(entry, "id")
            config = _strip_secretish(entry.get("config"))
            async with tx() as session:
                if await session.get(ConnectionRow, connection_id) is not None:
                    counts["skipped"] += 1
                    continue
                created_at = _ts(entry.get("created_at"))
                # secret_ref is ALWAYS NULL on import — credentials never travel; the row reads
                # hasSecret:false / oauth "not authorized" until the user re-enters them.
                session.add(
                    ConnectionRow(
                        id=connection_id,
                        name=str(entry.get("name") or connection_id),
                        kind=_req_str(entry, "kind"),
                        config=config,
                        secret_ref=None,
                        enabled=bool(entry.get("enabled", True)),
                        created_at=created_at,
                        updated_at=_opt_ts(entry.get("updated_at")) or created_at,
                    )
                )
                counts["created"] += 1
            if entry.get("has_secret"):
                counts["needs_secret"].append(connection_id)
            auth = config.get("auth")
            if isinstance(auth, dict) and auth.get("type") == "oauth":
                counts["needs_oauth"].append(connection_id)
            if _needs_env_reentry(config):
                # The exported config kept the env/header KEYS with blanked values (the values
                # are credential material and never travel) — tell the user what to re-enter.
                counts["needs_env"].append(connection_id)
        except Exception as exc:
            warnings.append(
                _failed("connections", entry.get("id") if isinstance(entry, dict) else None, exc)
            )
    return counts


async def _import_mcp_servers(
    section: list[Any],
    warnings: list[dict[str, Any]],
    *,
    tx: TxFactory,
    register_mcp: Callable[[str, McpServerConfig], Awaitable[None]] | None,
) -> dict[str, Any]:
    counts: dict[str, Any] = {"created": 0, "skipped": 0, "needs_env": []}
    for entry in section:
        try:
            if not isinstance(entry, dict):
                raise ValueError("mcp_server entry is not an object")
            name = _req_str(entry, "name")
            # The three transports the name-keyed registry accepts (PUT /admin/mcp/servers
            # rejects openapi/graphql — those live as connections) round-trip VERBATIM. An
            # unknown transport skips loudly: coercing it (say, sse → stdio) would persist a
            # registration with command=NULL that can never connect, with no warning.
            transport = entry.get("transport")
            if transport not in ("stdio", "http", "sse"):
                counts["skipped"] += 1
                warnings.append(
                    _failed(
                        "mcp_servers",
                        name,
                        ValueError(f"unknown mcp transport {transport!r}; server skipped"),
                    )
                )
                continue
            command = _opt_str(entry, "command")
            args = [str(a) for a in entry.get("args") or []]
            cwd = _opt_str(entry, "cwd")
            url = _opt_str(entry, "url")
            async with tx() as session:
                existing = (
                    await session.execute(select(McpServerRow).where(McpServerRow.name == name))
                ).scalar_one_or_none()
                if existing is not None:
                    counts["skipped"] += 1
                    continue
                created_at = _ts(entry.get("created_at"))
                # env/header VALUES never traveled — the registration lands with empty maps and
                # the user re-enters them (the ``needs_env`` report entry).
                session.add(
                    McpServerRow(
                        name=name,
                        transport=transport,
                        command=command,
                        args=args,
                        env={},
                        cwd=cwd,
                        url=url,
                        headers={},
                        created_at=created_at,
                        updated_at=_opt_ts(entry.get("updated_at")) or created_at,
                    )
                )
                counts["created"] += 1
            if register_mcp is not None:
                # Mirror the PUT route: register with the in-memory manager so the server is
                # usable without a restart (the lifespan rehydrate covers the next boot).
                await register_mcp(
                    name,
                    McpServerConfig(
                        transport=transport, command=command, args=args, cwd=cwd, url=url
                    ),
                )
            if entry.get("env_keys") or entry.get("header_keys"):
                counts["needs_env"].append(name)
        except Exception as exc:
            warnings.append(
                _failed("mcp_servers", entry.get("name") if isinstance(entry, dict) else None, exc)
            )
    return counts


async def _import_rag_sources(
    section: list[Any], warnings: list[dict[str, Any]], *, tx: TxFactory
) -> dict[str, Any]:
    counts: dict[str, Any] = {"created": 0, "skipped": 0, "needs_ingest": [], "needs_upload": []}
    for entry in section:
        try:
            if not isinstance(entry, dict):
                raise ValueError("rag_source entry is not an object")
            source_id = _req_str(entry, "id")
            kind = entry.get("kind")
            if kind not in ("upload", "crawl"):
                raise ValueError(f"unknown rag source kind {kind!r}")
            async with tx() as session:
                if await session.get(RagSourceRow, source_id) is not None:
                    counts["skipped"] += 1
                    continue
                created_at = _ts(entry.get("created_at"))
                config = entry.get("config")
                # Definition only, status "empty": the corpus never travels — a crawl source
                # re-ingests on the target, an upload source needs its files again.
                session.add(
                    RagSourceRow(
                        id=source_id,
                        name=str(entry.get("name") or source_id),
                        kind=kind,
                        config=config if isinstance(config, dict) else {},
                        embedding_model=_req_str(entry, "embedding_model"),
                        embedding_dim=None,
                        status="empty",
                        error=None,
                        progress=None,
                        created_at=created_at,
                        updated_at=_opt_ts(entry.get("updated_at")) or created_at,
                    )
                )
                counts["created"] += 1
            counts["needs_ingest" if kind == "crawl" else "needs_upload"].append(source_id)
        except Exception as exc:
            warnings.append(
                _failed("rag_sources", entry.get("id") if isinstance(entry, dict) else None, exc)
            )
    return counts
