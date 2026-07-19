"""Sample-agent catalog — ready-made agents a user installs from Settings to learn the platform.

Each sample ships as a JSON spec under ``data/``: metadata (title, capability badges, model
slots), the connections it needs, and a complete agent IR. The IR is a template in exactly two
controlled ways — everything else is literal, valid IR:

- **Model slots.** ``model_slots`` maps a slot name (``local`` / ``remote``) to the keys of
  ``ir.models`` it fills; the installer stamps the caller's chosen logical id + binding into
  those bindings. The caller picks models at install time because a logical model id is an
  environment binding, never portable content.
- **Connection refs.** ``{connection:<key>}`` strings resolve to the id of the connection the
  installer created (or found, by name) for the ``connections`` entry with that key — agents
  reference connections by id, and ids are minted per environment.

``{seed:<name>}`` strings inside a connection config resolve to a local demo file the installer
seeds (the SQLite database behind the private-SQL sample), so a sample never depends on data the
user has to fabricate first. Installed agents are ordinary registry agents — the ``sample-`` name
prefix and the hashed ``metadata.sample`` marker are the only traces of their origin, and the user
edits or deletes them like any other agent.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

#: Curated display order — the learning path, not alphabetical.
_SAMPLE_FILES = (
    "hybrid_reasoner.json",
    "private_sql_analyst.json",
    "expense_approver.json",
    "tool_belt.json",
    "second_opinion.json",
    "pii_firewall.json",
    "visual_inspector.json",
    "voice_desk.json",
    "art_department.json",
    "handbook_qa.json",
    "editorial_loop.json",
    "batch_briefing.json",
)


class SampleModelsError(ValueError):
    """A required model slot was not filled at install time. Carries the missing slot names so
    the API can name them in the 400 — never a silent fallback to the placeholder binding."""

    def __init__(self, sample_id: str, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            f"sample {sample_id!r} requires a model for slot(s) {', '.join(sorted(missing))}"
        )


@dataclass(frozen=True)
class SampleModelSlot:
    label: str
    description: str
    keys: tuple[str, ...]
    #: The inference-plane modality this slot's model must serve (``vision`` /
    #: ``audio.transcription`` / ``audio.speech`` / ``images.generation`` / ``embeddings``).
    #: ``None`` = a chat model — the UI's local/remote split applies.
    modality: str | None = None


@dataclass(frozen=True)
class SampleConnection:
    key: str
    name: str
    config: dict[str, Any]
    seed: str | None


@dataclass(frozen=True)
class SampleRagSource:
    """A retrieval source the sample needs: an ``upload``-kind source pinned to the caller's
    embedding-slot model, seeded with a document shipped under ``data/``. The IR references it
    via a ``{rag:<key>}`` placeholder (source ids are minted per environment, like connections)."""

    key: str
    name: str
    embedding_slot: str
    seed_doc: str


@dataclass(frozen=True)
class SampleSpec:
    id: str
    title: str
    description: str
    capabilities: tuple[str, ...]
    durable: bool
    input_example: Any
    model_slots: dict[str, SampleModelSlot]
    connections: tuple[SampleConnection, ...]
    ir: dict[str, Any]
    #: Child agents installed BEFORE the primary (subgraph/loop/map pins reference them by
    #: version, so the child must exist when the parent's pin resolves).
    extra_agents: tuple[dict[str, Any], ...] = ()
    rag_sources: tuple[SampleRagSource, ...] = ()
    #: An optional trigger created alongside a fresh install (shipped DISABLED — arming an
    #: unattended entry point is always the user's explicit click, never a side effect).
    trigger: dict[str, Any] | None = None

    @property
    def agent_id(self) -> str:
        """The registry agent id an install produces — fixed per sample (it comes from the IR),
        so "installed" is simply "an agent with this id exists"."""
        return str(self.ir["id"])

    def all_irs(self) -> tuple[dict[str, Any], ...]:
        """Every IR this sample installs, children first (install order)."""
        return (*self.extra_agents, self.ir)


@lru_cache(maxsize=1)
def load_catalog() -> tuple[SampleSpec, ...]:
    specs: list[SampleSpec] = []
    for filename in _SAMPLE_FILES:
        raw = json.loads(
            resources.files("theygent_control_plane.samples")
            .joinpath("data", filename)
            .read_text(encoding="utf-8")
        )
        specs.append(
            SampleSpec(
                id=raw["id"],
                title=raw["title"],
                description=raw["description"],
                capabilities=tuple(raw["capabilities"]),
                durable=bool(raw["durable"]),
                input_example=raw.get("input_example"),
                model_slots={
                    slot: SampleModelSlot(
                        label=cfg["label"],
                        description=cfg["description"],
                        keys=tuple(cfg["keys"]),
                        modality=cfg.get("modality"),
                    )
                    for slot, cfg in raw["model_slots"].items()
                },
                connections=tuple(
                    SampleConnection(
                        key=c["key"],
                        name=c["name"],
                        config=c["config"],
                        seed=c.get("seed"),
                    )
                    for c in raw["connections"]
                ),
                ir=raw["ir"],
                extra_agents=tuple(raw.get("extra_agents", [])),
                rag_sources=tuple(
                    SampleRagSource(
                        key=r["key"],
                        name=r["name"],
                        embedding_slot=r["embedding_slot"],
                        seed_doc=r["seed_doc"],
                    )
                    for r in raw.get("rag_sources", [])
                ),
                trigger=raw.get("trigger"),
            )
        )
    return tuple(specs)


def get_sample(sample_id: str) -> SampleSpec | None:
    return next((s for s in load_catalog() if s.id == sample_id), None)


def _substitute(value: Any, mapping: dict[str, str]) -> Any:
    """Replace whole-string placeholders (``{connection:key}`` / ``{seed:name}``) anywhere in a
    JSON structure. Whole-string only — a placeholder never interpolates into a longer literal,
    so ordinary strings (including GraphQL's own ``$``/``{`` syntax) pass through untouched."""
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, dict):
        return {k: _substitute(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, mapping) for v in value]
    return value


def render_connection_config(conn: SampleConnection, seed_paths: dict[str, str]) -> dict[str, Any]:
    """The connection's config with ``{seed:<name>}`` placeholders resolved to seeded paths."""
    mapping = {f"{{seed:{name}}}": path for name, path in seed_paths.items()}
    return _substitute(copy.deepcopy(conn.config), mapping)


def render_ir(
    spec: SampleSpec,
    *,
    models: dict[str, tuple[str, str]],
    connection_ids: dict[str, str],
    rag_ids: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The installable IRs (children first, primary last): model slots stamped with the caller's
    ``(logical_id, binding)`` wherever a slot key appears in an IR's models map, and
    ``{connection:<key>}`` / ``{rag:<key>}`` refs resolved to real ids. Raises
    :class:`SampleModelsError` when a declared slot is unfilled — the placeholder bindings in the
    shipped JSON must never reach the registry — and ``ValueError`` for a slot key no shipped IR
    declares (a catalog bug, not a caller mistake)."""
    missing = [
        slot
        for slot in spec.model_slots
        if slot not in models or not (models[slot][0] or "").strip()
    ]
    if missing:
        raise SampleModelsError(spec.id, missing)
    irs = [copy.deepcopy(ir) for ir in spec.all_irs()]
    for slot, slot_spec in spec.model_slots.items():
        logical_id, binding = models[slot]
        for key in slot_spec.keys:
            stamped = False
            for ir in irs:
                entry = ir.get("models", {}).get(key)
                if entry is not None:
                    entry["model"] = logical_id
                    entry["binding"] = binding
                    stamped = True
            if not stamped:
                raise ValueError(
                    f"sample {spec.id!r}: slot {slot!r} names model key {key!r}, which no "
                    "shipped IR declares"
                )
    mapping = {f"{{connection:{key}}}": cid for key, cid in connection_ids.items()}
    for key, rid in (rag_ids or {}).items():
        mapping[f"{{rag:{key}}}"] = rid
    return [_substitute(ir, mapping) for ir in irs]


def seed_document(name: str) -> tuple[str, str, bytes]:
    """A shipped seed document by data-file name → ``(filename, content_type, bytes)`` for the
    RAG upload path. Unknown names are a loud catalog bug."""
    if name != "handbook_md":
        raise ValueError(f"unknown seed document {name!r}")
    data = (
        resources.files("theygent_control_plane.samples")
        .joinpath("data", "handbook.md")
        .read_bytes()
    )
    return ("theygent-handbook.md", "text/markdown", data)


# ── demo data seeding ────────────────────────────────────────────────────────


def _seed_crm_sqlite(path: str) -> None:
    """A small, deterministic CRM database (customers + orders) for the private-SQL sample. Fixed
    values, no clock and no randomness — reseeding an existing file is a no-op so reinstalling a
    sample never rewrites data the user may have played with. The write goes to a temp file and
    lands via ``os.replace`` so a crash mid-seed never leaves a half-built database at the real
    path, and two concurrent seeders each publish a complete file (last one wins, both valid)."""
    if os.path.exists(path):
        return
    tmp = f"{path}.tmp.{os.getpid()}"
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT,
                country TEXT,
                signup_date TEXT
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER REFERENCES customers(id),
                amount_eur REAL,
                status TEXT,
                ordered_at TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO customers (id, name, country, signup_date) VALUES (?, ?, ?, ?)",
            [
                # Deliberately name-free labels: invented company names could collide with
                # real marks, and this data ships with the product.
                (1, "customer-01", "DE", "2025-03-11"),
                (2, "customer-02", "SE", "2025-04-02"),
                (3, "customer-03", "US", "2025-05-19"),
                (4, "customer-04", "JP", "2025-06-30"),
                (5, "customer-05", "FR", "2025-08-14"),
                (6, "customer-06", "AU", "2025-09-01"),
                (7, "customer-07", "TR", "2025-10-22"),
                (8, "customer-08", "NO", "2025-12-05"),
            ],
        )
        conn.executemany(
            "INSERT INTO orders (id, customer_id, amount_eur, status, ordered_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (1, 1, 4200.0, "paid", "2026-01-08"),
                (2, 3, 1150.5, "paid", "2026-01-12"),
                (3, 2, 980.0, "refunded", "2026-01-15"),
                (4, 5, 2300.0, "paid", "2026-01-21"),
                (5, 1, 660.0, "paid", "2026-02-02"),
                (6, 4, 5400.0, "paid", "2026-02-06"),
                (7, 7, 310.0, "pending", "2026-02-11"),
                (8, 6, 8750.0, "paid", "2026-02-18"),
                (9, 2, 1425.0, "paid", "2026-03-01"),
                (10, 8, 990.0, "paid", "2026-03-05"),
                (11, 3, 2750.0, "refunded", "2026-03-09"),
                (12, 5, 1875.0, "paid", "2026-03-16"),
                (13, 1, 3200.0, "paid", "2026-03-27"),
                (14, 4, 720.0, "pending", "2026-04-03"),
                (15, 6, 4100.0, "paid", "2026-04-11"),
                (16, 7, 1560.0, "paid", "2026-04-19"),
                (17, 8, 2380.0, "paid", "2026-05-02"),
                (18, 2, 640.0, "paid", "2026-05-14"),
                (19, 3, 5150.0, "paid", "2026-05-23"),
                (20, 5, 895.0, "pending", "2026-06-04"),
            ],
        )
        conn.commit()
    except BaseException:
        conn.close()
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    else:
        conn.close()
        os.replace(tmp, path)


#: Seed name → seeder. A seeder writes the demo file at the given path (idempotent).
_SEEDERS = {"crm_sqlite": _seed_crm_sqlite}


def seed_demo_file(seed: str, base_dir: str) -> str:
    """Seed the named demo file under ``<base_dir>/samples/`` and return its absolute path.
    Unknown seed names are a loud error — a sample spec naming a seeder that does not exist is a
    catalog bug, never a skipped step."""
    seeder = _SEEDERS.get(seed)
    if seeder is None:
        raise ValueError(f"unknown sample seed {seed!r}; known: {sorted(_SEEDERS)}")
    samples_dir = os.path.join(base_dir, "samples")
    os.makedirs(samples_dir, exist_ok=True)
    path = os.path.abspath(os.path.join(samples_dir, f"{seed.replace('_', '-')}.db"))
    seeder(path)
    return path
