"""Platform settings — a typed catalog over ``platform_setting``, with an honest apply story.

The knobs that used to be env-vars-read-once or compile-time constants (telemetry export, I/O
capture bounds, MCP timeouts/registries, RAG ingest/retrieval defaults) become editable at
runtime. Three rules keep the surface honest:

1. **Precedence: explicit env > stored > default** — "explicit" means set AND non-empty (the
   empty-string-equals-unset convention every entrypoint already uses). An env-**pinned** key is
   locked (env wins, ``env_pinned``); an env-**capped** key stays editable up to the env value
   (``effective = min(env, stored-or-default)``, ``env_capped`` + the cap value reported) — a
   capped knob rendered as locked would be dishonest in the other direction. A stored value that
   no longer validates resolves to the default with ``stored_invalid`` — never half-applied.
   Rows whose key left the catalog are ignored and surfaced as ``orphaned`` (deletable via a
   ``null`` patch), never a boot failure.

2. **A typed catalog, not a free-form KV.** ``CATALOG`` is the schema: unknown keys are rejected
   loudly, constraints are validated server-side, and the UI renders from the catalog entries the
   API returns. Sensitive values (OTLP headers) are Fernet-encrypted through the existing
   ``SecretStore`` — the ``platform_setting`` row holds a ``{"secret_ref", "names"}`` envelope,
   reads return ``{"set": true, "names": [...]}``, and the plaintext exists only server-side
   (``resolve_sensitive``), e.g. while building the OTLP exporter.

3. **Apply is two-phase, and "live" must be true in every process.** A patch is atomic
   validate+persist in one transaction (any invalid key → nothing stored), then best-effort
   post-commit hooks reported per key in ``apply_errors`` — a hook failure never rolls back the
   stored value. Every process (each API replica AND the worker) runs the :meth:`refresher
   <SettingsService.start_refresher>`: a background loop that re-reads the table, diffs against
   the last-applied state, and re-runs the hooks on change — that loop is what makes a "live"
   badge true beyond the one replica that handled the PATCH.

The one sovereignty-critical wrinkle lives in the OTLP keys: the ambient
``OTEL_EXPORTER_OTLP_ENDPOINT`` env var pins the endpoint *value* but must NEVER pin export *on*
— ``telemetry.otlp_enabled`` merely *defaults* to true when the env endpoint is set, so a stored
``false`` is a working kill switch. Telemetry egress must always be user-stoppable from the UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from theygent_control_plane.models import PlatformSettingRow
from theygent_control_plane.observability.spans import min_capture
from theygent_control_plane.run import now
from theygent_control_plane.secrets import SecretStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from theygent_control_plane.observability import Telemetry

logger = logging.getLogger("theygent.control_plane.settings")

#: A live-apply hook. Zero-arg on purpose: a hook re-reads whatever it needs from the service
#: (so one hook can cover several keys — the OTLP rebuild covers four — and a refresher re-run
#: and a post-patch run are the same code path). Registered per key or per ``"prefix."``.
SettingsHook = Callable[[], Awaitable[None]]

_CAPTURE_LEVELS = ("off", "metadata", "full")


@dataclass(frozen=True)
class SettingSpec:
    """One catalog entry — the schema for one settable key. ``default`` may be a zero-arg
    callable for dynamic defaults (``telemetry.otlp_enabled`` defaults to whether the ambient
    OTLP endpoint env var is set). ``env_mode`` is ``"pin"`` (env value wins, key locked) or
    ``"cap"`` (effective = min(env, stored-or-default), key stays editable) or ``None``."""

    key: str
    type: str  # bool | int | float | str | enum | list[str] | list[registry] | secret
    default: Any
    env_var: str | None = None
    env_mode: str | None = None  # "pin" | "cap" | None
    apply: str = "live"
    constraints: dict[str, Any] = field(default_factory=dict)
    sensitive: bool = False
    description: str = ""
    group: str = ""

    def resolved_default(self) -> Any:
        return self.default() if callable(self.default) else self.default


def _otlp_default_enabled() -> bool:
    # Back-compat default only: an ambient endpoint used to switch export on, so it still
    # *defaults* export on — but a stored false always wins (the kill switch). The env var
    # never PINS export on.
    return bool((os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip())


CATALOG: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="telemetry.otlp_enabled",
        type="bool",
        default=_otlp_default_enabled,
        description=(
            "Export spans to an OTLP collector. The kill switch: a stored false stops export "
            "even when the deployment sets an endpoint env var — telemetry egress is always "
            "user-stoppable. Defaults to on only when the environment configures an endpoint."
        ),
        group="telemetry",
    ),
    SettingSpec(
        key="telemetry.otlp_endpoint",
        type="str",
        default=None,
        env_var="OTEL_EXPORTER_OTLP_ENDPOINT",
        env_mode="pin",
        constraints={"nullable": True},
        description="The OTLP/HTTP collector endpoint spans are exported to.",
        group="telemetry",
    ),
    SettingSpec(
        key="telemetry.otlp_headers",
        type="secret",
        default=None,
        sensitive=True,
        constraints={"nullable": True},
        description=(
            "Headers sent with every OTLP export request (collector auth). Write-only: reads "
            "return the header names, never the values. Writing replaces the whole map."
        ),
        group="telemetry",
    ),
    SettingSpec(
        key="telemetry.otlp_redact_attrs",
        type="list[str]",
        default=[],
        env_var="THEYGENT_OTEL_REDACT_ATTRS",
        env_mode="pin",
        description=(
            "Span attribute names replaced with '[redacted]' before crossing the OTLP "
            "boundary. The local span store keeps every attribute."
        ),
        group="telemetry",
    ),
    SettingSpec(
        key="telemetry.io_capture",
        type="enum",
        default="full",
        env_var="THEYGENT_IO_CAPTURE",
        env_mode="cap",
        constraints={"choices": list(_CAPTURE_LEVELS)},
        description=(
            "The deployment-wide ceiling on node I/O payload capture. Stored values can only "
            "tighten the env cap; per-agent policy applies under this ceiling per run."
        ),
        group="telemetry",
    ),
    SettingSpec(
        key="telemetry.io_capture_max_bytes",
        type="int",
        default=256 * 1024,
        env_var="THEYGENT_IO_CAPTURE_MAX_BYTES",
        env_mode="cap",
        constraints={"min": 1024, "max": 10 * 1024 * 1024},
        description="Per-payload cap for captured node I/O; larger payloads are truncated.",
        group="telemetry",
    ),
    SettingSpec(
        key="mcp.call_timeout_s",
        type="float",
        default=120.0,
        env_var="THEYGENT_MCP_CALL_TIMEOUT_S",
        env_mode="pin",
        apply="live (next connect)",
        constraints={"min": 1, "max": 3600},
        description=(
            "Ceiling for one MCP tool invocation. Applies to newly established/reconnected "
            "server connections."
        ),
        group="mcp",
    ),
    SettingSpec(
        key="mcp.connect_timeout_s",
        type="float",
        default=30.0,
        apply="live (next connect)",
        constraints={"min": 1, "max": 600},
        description=(
            "Ceiling for establishing one MCP server connection (spawn + handshake), so a "
            "wedged server can never hang a run or an unattended trigger fire."
        ),
        group="mcp",
    ),
    SettingSpec(
        key="mcp.extra_registries",
        type="list[registry]",
        default=[],
        env_var="THEYGENT_MCP_REGISTRIES",
        env_mode="pin",
        description=(
            "Self-hosted MCP registries (id/label/url) browsable alongside the built-in "
            "public ones — the air-gapped allowlist."
        ),
        group="mcp",
    ),
    SettingSpec(
        key="mcp.oauth_redirect_url",
        type="str",
        default="http://localhost:8080/mcp/oauth/callback",
        env_var="THEYGENT_OAUTH_REDIRECT_URL",
        env_mode="pin",
        description=(
            "Where the authorization server sends the browser back after consent. Change it "
            "when the API runs behind a reverse proxy or a non-default bind."
        ),
        group="mcp",
    ),
    SettingSpec(
        key="rag.chunk_max_tokens",
        type="int",
        default=450,
        apply="live (next ingest)",
        constraints={"min": 50, "max": 4000},
        description="Token budget per retrieval chunk at ingest time.",
        group="rag",
    ),
    SettingSpec(
        key="rag.chunk_overlap_tokens",
        type="int",
        default=60,
        apply="live (next ingest)",
        constraints={"min": 0, "max": 1000},
        description="Token overlap between blind-split chunks at ingest time.",
        group="rag",
    ),
    SettingSpec(
        key="rag.crawl_desired_concurrency",
        type="int",
        default=2,
        apply="live (next crawl)",
        constraints={"min": 1, "max": 16},
        description="Concurrent page fetches a crawl aims for (politeness/throughput).",
        group="rag",
    ),
    SettingSpec(
        key="rag.crawl_max_concurrency",
        type="int",
        default=4,
        apply="live (next crawl)",
        constraints={"min": 1, "max": 32},
        description="Hard upper bound on concurrent page fetches during a crawl.",
        group="rag",
    ),
    SettingSpec(
        key="rag.max_upload_bytes",
        type="int",
        default=50 * 1024 * 1024,
        constraints={"min": 1024 * 1024, "max": 1024 * 1024 * 1024},
        description="Maximum size of one uploaded retrieval document.",
        group="rag",
    ),
    SettingSpec(
        key="rag.default_top_k",
        type="int",
        default=5,
        constraints={"min": 1, "max": 50},
        description=(
            "How many passages retrieval returns when a query/node does not set top_k explicitly."
        ),
        group="rag",
    ),
    SettingSpec(
        key="rag.default_embedding_model",
        type="str",
        default=None,
        apply="live (form default)",
        constraints={"nullable": True},
        description=(
            "The embedding model the create-source form suggests. Each source still pins its "
            "own model at create time (that binding stays immutable)."
        ),
        group="rag",
    ),
)

_SPECS: dict[str, SettingSpec] = {s.key: s for s in CATALOG}


class SettingsValidationError(ValueError):
    """One or more keys in a patch failed validation — NOTHING was stored. ``errors`` maps each
    offending key to its message; ``summary`` is the one-line 422 body."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__(self.summary())

    def summary(self) -> str:
        return "; ".join(f"{key}: {msg}" for key, msg in sorted(self.errors.items()))


# ── env + value parsing (pure) ───────────────────────────────────────────────


def _env_raw(spec: SettingSpec) -> str | None:
    """The env value iff EXPLICIT (set AND non-empty — the existing convention); else None."""
    if spec.env_var is None:
        return None
    raw = os.environ.get(spec.env_var)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _parse_env(spec: SettingSpec, raw: str) -> tuple[bool, Any]:
    """Parse an explicit env value into the spec's type. A malformed value returns
    ``(False, None)`` — treated as unset with a warning, so a typo'd env var degrades to the
    stored/default resolution instead of poisoning every request. (Boot-time surfaces that MUST
    fail loudly on a malformed env — the registry allowlist — still do so at their own read.)"""
    try:
        if spec.type == "bool":
            low = raw.lower()
            if low in ("1", "true", "yes", "on"):
                return True, True
            if low in ("0", "false", "no", "off"):
                return True, False
            return False, None
        if spec.type == "int":
            return True, int(raw)
        if spec.type == "float":
            return True, float(raw)
        if spec.type == "enum":
            low = raw.lower()
            return (True, low) if low in spec.constraints.get("choices", ()) else (False, None)
        if spec.type == "list[str]":
            return True, [item.strip() for item in raw.split(",") if item.strip()]
        if spec.type == "list[registry]":
            parsed = json.loads(raw)
            # Validation also normalizes each row to exactly {id,label,url} — the env value
            # must resolve to the same shape a stored value applies as.
            return True, _validate_value(spec, parsed)
        return True, raw  # str
    except Exception:
        return False, None


def _validate_value(spec: SettingSpec, value: Any) -> Any:
    """Validate a to-be-stored (or stored) value against the spec. Raises ``ValueError`` with an
    actionable message; returns the (possibly normalized) value. ``None`` is never valid here —
    a null patch means reset and never reaches storage."""
    c = spec.constraints
    if spec.type == "bool":
        if not isinstance(value, bool):
            raise ValueError("expected true or false")
        return value
    if spec.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("expected an integer")
        _check_range(value, c)
        return value
    if spec.type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("expected a number")
        value = float(value)
        _check_range(value, c)
        return value
    if spec.type == "enum":
        choices = c.get("choices", ())
        if value not in choices:
            raise ValueError(f"expected one of {list(choices)}")
        return value
    if spec.type == "str":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("expected a non-empty string (use null to reset)")
        return value
    if spec.type == "list[str]":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("expected a list of strings")
        return value
    if spec.type == "list[registry]":
        if not isinstance(value, list):
            raise ValueError('expected a list of {"id","label","url"} objects')
        normalized: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict) or not all(
                isinstance(item.get(k), str) and item[k].strip() for k in ("id", "label", "url")
            ):
                raise ValueError(
                    'each registry must carry non-empty string "id", "label" and "url"'
                )
            # Extra keys (a hand-written env entry may carry annotations like "note") are
            # tolerated on input — the boot-time env parser accepts them too, and rejecting
            # them here would resolve the env var as unset and silently strip the operator's
            # self-hosted registries. Only the three known keys are stored/applied.
            normalized.append({k: item[k] for k in ("id", "label", "url")})
        return normalized
    if spec.type == "secret":
        if (
            not isinstance(value, dict)
            or not value
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
        ):
            raise ValueError("expected a non-empty {name: value} map of strings (null to reset)")
        return value
    raise ValueError(f"unhandled setting type {spec.type!r}")  # pragma: no cover - catalog bug


def _check_range(value: float, constraints: dict[str, Any]) -> None:
    lo, hi = constraints.get("min"), constraints.get("max")
    if lo is not None and value < lo:
        raise ValueError(f"must be >= {lo}")
    if hi is not None and value > hi:
        raise ValueError(f"must be <= {hi}")


def _apply_cap(spec: SettingSpec, cap: Any, value: Any) -> Any:
    """min(env cap, chosen) — numeric for numbers, the capture-level meet for the enum."""
    if spec.type == "enum":
        return min_capture(cap, value)
    return min(cap, value)


def _is_secret_envelope(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("secret_ref"), str)
        and isinstance(value.get("names"), list)
    )


def _masked(names: list[str]) -> dict[str, Any]:
    """The sensitive read shape: names are non-secret, values never return."""
    return {"set": True, "names": sorted(names)}


@dataclass
class ResolvedSetting:
    """One key's resolution: the effective value plus everything the UI needs to render the
    honest state (source, pin/cap, invalid-stored). Sensitive values are already masked here —
    the raw envelope never leaves the service except through ``resolve_sensitive``."""

    spec: SettingSpec
    value: Any
    stored_value: Any  # masked for sensitive; None when no row
    source: str  # env | stored | default
    env_pinned: bool = False
    env_capped: bool = False
    env_cap_value: Any = None
    stored_invalid: bool = False
    change_token: Any = None  # what the refresher diffs on (raw envelope for sensitive keys)

    def snapshot_entry(self) -> dict[str, Any]:
        s = self.spec
        return {
            "key": s.key,
            "value": self.value,
            "stored_value": self.stored_value,
            "default": None if s.sensitive else s.resolved_default(),
            "type": s.type,
            "source": self.source,
            "env_var": s.env_var,
            "env_pinned": self.env_pinned,
            "env_capped": self.env_capped,
            "env_cap_value": self.env_cap_value,
            "apply": s.apply,
            # Every v1 key applies live (some at the next connect/ingest); nothing requires a
            # restart, so nothing can be pending one.
            "restart_pending": False,
            "stored_invalid": self.stored_invalid,
            "constraints": dict(s.constraints),
            "description": s.description,
            "group": s.group,
        }


def resolve_setting(key: str, stored: dict[str, Any]) -> ResolvedSetting:
    """Pure resolution of one catalog key against the stored-row map (env > stored > default,
    with pin/cap semantics). Shared by the service's async paths and the sync bootstrap
    (:func:`bootstrap_value`) so the two can never disagree."""
    spec = _SPECS[key]
    env_value: Any = None
    env_explicit = False
    raw_env = _env_raw(spec)
    if raw_env is not None:
        ok, parsed = _parse_env(spec, raw_env)
        if ok:
            env_value, env_explicit = parsed, True
        else:
            logger.warning(
                "settings.env_unparseable",
                extra={"key": key, "env_var": spec.env_var},
            )

    stored_value: Any = None
    stored_present = key in stored
    stored_invalid = False
    if stored_present:
        stored_value = stored[key]
        if spec.sensitive:
            if not _is_secret_envelope(stored_value):
                stored_invalid = True
        else:
            try:
                stored_value = _validate_value(spec, stored_value)
            except ValueError:
                stored_invalid = True

    if env_explicit and spec.env_mode == "pin":
        return ResolvedSetting(
            spec=spec,
            value=env_value,
            stored_value=_stored_view(spec, stored, stored_present, stored_invalid),
            source="env",
            env_pinned=True,
            stored_invalid=stored_invalid,
            change_token=env_value,
        )

    use_stored = stored_present and not stored_invalid
    if spec.sensitive:
        chosen = _masked(stored[key]["names"]) if use_stored else spec.resolved_default()
        token = stored[key] if use_stored else None
    else:
        chosen = stored_value if use_stored else spec.resolved_default()
        token = chosen
    source = "stored" if use_stored else "default"

    if env_explicit and spec.env_mode == "cap":
        effective = _apply_cap(spec, env_value, chosen)
        return ResolvedSetting(
            spec=spec,
            value=effective,
            stored_value=_stored_view(spec, stored, stored_present, stored_invalid),
            source=source,
            env_capped=True,
            env_cap_value=env_value,
            stored_invalid=stored_invalid,
            change_token=effective,
        )

    return ResolvedSetting(
        spec=spec,
        value=chosen,
        stored_value=_stored_view(spec, stored, stored_present, stored_invalid),
        source=source,
        stored_invalid=stored_invalid,
        change_token=token,
    )


def _stored_view(spec: SettingSpec, stored: dict[str, Any], present: bool, invalid: bool) -> Any:
    """What ``stored_value`` reports: the raw stored value (even an invalid one — the UI must be
    able to show what is stored vs what applies), masked for sensitive keys."""
    if not present:
        return None
    raw = stored[spec.key]
    if spec.sensitive:
        return _masked(raw["names"]) if _is_secret_envelope(raw) else {"set": True, "names": []}
    return raw


def bootstrap_value(key: str) -> Any:
    """Sync env>default resolution (no DB) — what an accessor answers before the service's first
    load. Stored values arrive as soon as the lifespan loads the service (and every refresher
    period after that), so this is only the first-instants fallback."""
    return resolve_setting(key, {}).value


class SettingsService:
    """The one platform-settings authority for a process: resolution (with a short in-process
    cache), atomic patches, live-apply hooks, and the cross-process refresher."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        secrets: SecretStore,
        cache_ttl_s: float = 15.0,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._secrets = secrets
        self._ttl = cache_ttl_s
        self._resolved: dict[str, ResolvedSetting] = {}
        self._orphaned: list[str] = []
        self._loaded_at: float | None = None
        # The change tokens the hooks last ran against — the refresher/patch diff baseline.
        self._applied: dict[str, Any] = {}
        self._hooks: list[tuple[str, SettingsHook]] = []
        self._refresh_task: asyncio.Task[None] | None = None
        self._resolve_lock = asyncio.Lock()

    # ── hooks ──────────────────────────────────────────────────────────────

    def register_hook(self, key_or_prefix: str, hook: SettingsHook) -> None:
        """Register a live-apply hook for one key (exact match) or a family (a pattern ending in
        ``"."`` matches every key under it). Hooks run best-effort after a patch commits and
        whenever the refresher observes a change; a hook shared across keys runs once per apply
        cycle."""
        self._hooks.append((key_or_prefix, hook))

    def _hooks_for(self, changed: set[str]) -> list[tuple[SettingsHook, list[str]]]:
        out: list[tuple[SettingsHook, list[str]]] = []
        seen: dict[int, int] = {}  # id(hook) -> index into out
        for pattern, hook in self._hooks:
            matched = [
                k
                for k in sorted(changed)
                if k == pattern or (pattern.endswith(".") and k.startswith(pattern))
            ]
            if not matched:
                continue
            if id(hook) in seen:
                slot = out[seen[id(hook)]]
                slot[1].extend(k for k in matched if k not in slot[1])
            else:
                seen[id(hook)] = len(out)
                out.append((hook, matched))
        return out

    async def _run_hooks(self, changed: set[str]) -> dict[str, str]:
        errors: dict[str, str] = {}
        for hook, keys in self._hooks_for(changed):
            try:
                await hook()
            except Exception as exc:  # best-effort by contract — never roll back the store
                for key in keys:
                    errors[key] = str(exc)
                logger.warning("settings.apply_failed", extra={"keys": keys, "error": str(exc)})
        return errors

    # ── resolution + cache ─────────────────────────────────────────────────

    async def _read_rows(self) -> dict[str, Any]:
        async with self._sessionmaker() as session:
            rows = (await session.execute(select(PlatformSettingRow))).scalars().all()
        return {row.key: row.value for row in rows}

    def _resolve_all(self, stored: dict[str, Any]) -> None:
        self._resolved = {key: resolve_setting(key, stored) for key in _SPECS}
        self._orphaned = sorted(k for k in stored if k not in _SPECS)
        self._loaded_at = time.monotonic()

    async def load(self) -> None:
        """Initial resolve (lifespan): read + resolve and set the hook baseline WITHOUT running
        hooks — boot wiring applies the initial effective values explicitly."""
        async with self._resolve_lock:
            self._resolve_all(await self._read_rows())
            self._applied = {k: r.change_token for k, r in self._resolved.items()}

    async def _ensure_fresh(self, *, force: bool = False) -> None:
        stale = self._loaded_at is None or time.monotonic() - self._loaded_at >= self._ttl
        if not (force or stale):
            return
        async with self._resolve_lock:
            self._resolve_all(await self._read_rows())

    async def get(self, key: str) -> Any:
        """The effective value for one catalog key (short-TTL in-process cache). Unknown keys
        are a programming error — ``KeyError``."""
        if key not in _SPECS:
            raise KeyError(f"unknown setting {key!r}")
        await self._ensure_fresh()
        return self._resolved[key].value

    def value(self, key: str) -> Any:
        """Sync read of the last-resolved effective value (the accessor seam for call sites that
        cannot await — MCP client construction, the OAuth redirect). Falls back to env>default
        when the service has not loaded yet; the refresher keeps it at most one period stale."""
        if key not in _SPECS:
            raise KeyError(f"unknown setting {key!r}")
        resolved = self._resolved.get(key)
        return resolved.value if resolved is not None else bootstrap_value(key)

    def accessor(self, key: str) -> Callable[[], Any]:
        """A zero-arg sync accessor bound to one key — what gets threaded into constructors."""
        if key not in _SPECS:
            raise KeyError(f"unknown setting {key!r}")
        return lambda: self.value(key)

    async def snapshot(self) -> dict[str, Any]:
        """The full GET payload minus the boot block (which is app wiring's knowledge): every
        catalog entry plus the orphaned stored keys. Always a fresh read — a settings page must
        never show another replica's 15-seconds-ago state as current."""
        await self._ensure_fresh(force=True)
        return {
            "settings": [self._resolved[s.key].snapshot_entry() for s in CATALOG],
            "orphaned": list(self._orphaned),
        }

    async def resolve_sensitive(self, key: str) -> dict[str, str] | None:
        """Decrypt a sensitive key's stored map — SERVER-SIDE use only (building the OTLP
        exporter, the collector probe). Never reachable through GET/PATCH responses."""
        spec = _SPECS[key]
        if not spec.sensitive:
            raise KeyError(f"setting {key!r} is not sensitive")
        async with self._sessionmaker() as session:
            row = await session.get(PlatformSettingRow, key)
            if row is None or not _is_secret_envelope(row.value):
                return None
            plaintext = await self._secrets.resolve(session, row.value["secret_ref"])
        if plaintext is None:
            return None
        parsed = json.loads(plaintext)
        return {str(k): str(v) for k, v in parsed.items()}

    # ── patch (atomic validate + persist, then best-effort hooks) ──────────

    async def patch(self, changes: dict[str, Any]) -> dict[str, str]:
        """Apply a ``{key: value|null}`` patch: validate EVERY key first (any failure →
        :class:`SettingsValidationError`, nothing stored), persist in ONE transaction (null =
        delete the row — and, for sensitive/orphaned keys, their secret row in the same tx),
        then run the live-apply hooks best-effort and return their per-key ``apply_errors``."""
        errors: dict[str, str] = {}
        validated: dict[str, Any] = {}
        for key, value in changes.items():
            spec = _SPECS.get(key)
            if spec is None:
                # Unknown key: a null is the sanctioned way to delete an orphaned row; any
                # non-null value is rejected loudly (the catalog is the schema).
                if value is not None:
                    errors[key] = "unknown setting"
                continue
            if value is None:
                continue  # reset — always valid
            raw_env = _env_raw(spec)
            # Locked only when the pin actually RESOLVES (an unparseable env value is treated
            # as unset at resolution, so rejecting a write against it would be dishonest).
            if raw_env is not None and spec.env_mode == "pin" and _parse_env(spec, raw_env)[0]:
                errors[key] = f"locked by the {spec.env_var} environment variable (env-pinned)"
                continue
            try:
                validated[key] = _validate_value(spec, value)
            except ValueError as exc:
                errors[key] = str(exc)
        if errors:
            raise SettingsValidationError(errors)

        ts = now()
        async with self._sessionmaker() as session, session.begin():
            for key, value in changes.items():
                spec = _SPECS.get(key)
                row = await session.get(PlatformSettingRow, key)
                if value is None:
                    if row is not None:
                        if _is_secret_envelope(row.value):
                            await self._secrets.delete(session, row.value["secret_ref"])
                        await session.delete(row)
                    continue
                stored_value = validated[key]
                if spec is not None and spec.sensitive:
                    # Full replacement under a FRESH ref (nothing hashes a platform setting, and
                    # a new ref is what lets other replicas' refreshers see the rotation); the
                    # old secret row is deleted in the same transaction.
                    if row is not None and _is_secret_envelope(row.value):
                        await self._secrets.delete(session, row.value["secret_ref"])
                    ref = await self._secrets.create(session, json.dumps(stored_value))
                    stored_value = {"secret_ref": ref, "names": sorted(stored_value)}
                if row is None:
                    session.add(PlatformSettingRow(key=key, value=stored_value, updated_at=ts))
                else:
                    row.value = stored_value
                    row.updated_at = ts

        return await self.refresh()

    # ── refresh (the cross-process live-apply loop) ────────────────────────

    async def refresh(self) -> dict[str, str]:
        """Re-read + re-resolve, diff against the last-applied state, and run hooks for every
        changed key. The post-patch apply and the background refresher are THIS one code path —
        which is what makes a "live" badge true on other replicas and on the worker."""
        async with self._resolve_lock:
            self._resolve_all(await self._read_rows())
            tokens = {k: r.change_token for k, r in self._resolved.items()}
            changed = {k for k, tok in tokens.items() if tok != self._applied.get(k)}
        if not changed:
            return {}
        errors = await self._run_hooks(changed)
        # Advance the baseline only for keys whose hooks ran clean. A key whose hook raised
        # keeps its OLD token, so the next refresh diffs it as changed and retries the hook —
        # otherwise one transient failure (a collector briefly down, say) would leave this
        # process desynced until the stored value happened to change again. A permanently
        # failing hook retries once per refresh period; _run_hooks logs one warning per
        # attempt (message + keys, no stack), so the retry loop stays readable.
        for key, token in tokens.items():
            if key not in errors:
                self._applied[key] = token
        return errors

    def start_refresher(self, period_s: float = 15.0) -> None:
        """Start the background refresher (idempotent). ``period_s`` bounds cross-process apply
        lag — the accepted staleness story; injectable so tests can run a fast loop."""
        if self._refresh_task is not None and not self._refresh_task.done():
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(period_s)
                try:
                    await self.refresh()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # the loop must survive a transient DB error
                    logger.warning("settings.refresh_failed", extra={"error": str(exc)})

        self._refresh_task = asyncio.get_running_loop().create_task(_loop())

    async def stop_refresher(self) -> None:
        task, self._refresh_task = self._refresh_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


# ── shared wiring helpers (API process + worker use the SAME ones) ──────────


async def otlp_sink_from_settings(service: SettingsService) -> Any:
    """Build the OTLP sink from EFFECTIVE settings, honoring the kill switch: export is on iff
    ``telemetry.otlp_enabled`` (stored false always wins) AND an endpoint exists (the env var
    pins the endpoint *value*, never export *on*). Returns ``None`` when export is off."""
    from theygent_control_plane.observability.otlp import build_otlp_sink

    if not await service.get("telemetry.otlp_enabled"):
        return None
    endpoint = await service.get("telemetry.otlp_endpoint")
    if not endpoint:
        return None
    headers = await service.resolve_sensitive("telemetry.otlp_headers")
    redact = frozenset(await service.get("telemetry.otlp_redact_attrs"))
    return build_otlp_sink(endpoint=endpoint, headers=headers, redact_attrs=redact)


def register_telemetry_hooks(service: SettingsService, telemetry: Telemetry) -> None:
    """Wire the telemetry live-apply hooks: capture bounds as plain attribute sets, and the OTLP
    sink as a serialized build → swap → flush-the-old. ``Telemetry.otlp`` is the single mutable
    holder (span close re-reads it), so the swap is atomic from the emitters' view; the old
    sink's ``shutdown`` flushes its batch off-thread so no tail spans are lost. One lock
    serializes rebuilds — two rapid patches must not interleave build/swap/shutdown."""
    otlp_lock = asyncio.Lock()

    async def apply_otlp() -> None:
        async with otlp_lock:
            new_sink = await otlp_sink_from_settings(service)
            old_sink, telemetry.otlp = telemetry.otlp, new_sink
            if old_sink is not None:
                await asyncio.to_thread(old_sink.shutdown)

    for key in (
        "telemetry.otlp_enabled",
        "telemetry.otlp_endpoint",
        "telemetry.otlp_headers",
        "telemetry.otlp_redact_attrs",
    ):
        service.register_hook(key, apply_otlp)

    async def apply_ceiling() -> None:
        telemetry.ceiling = await service.get("telemetry.io_capture")

    async def apply_max_bytes() -> None:
        telemetry.max_bytes = await service.get("telemetry.io_capture_max_bytes")

    service.register_hook("telemetry.io_capture", apply_ceiling)
    service.register_hook("telemetry.io_capture_max_bytes", apply_max_bytes)
