"""SQLAlchemy ORM rows — the persistence shape (M4 §3), kept SEPARATE from the domain.

These are *rows*, not the API/logic entities. The domain ``Run`` (``run.py``) is a clean
Pydantic model mapped to/from ``RunRow`` (§1.3): the wire contract must not be welded to
the schema, so each can evolve without breaking the other — the same seam-thinking as
IR-vs-React-Flow. A thin mapping layer (``store.py``) is the cost; decoupling is the payoff.

``Base.metadata`` here is Alembic's ``target_metadata``, but the schema is owned by the
hand-written migration (§0/§1.1) — **never** ``create_all``, including in tests. Keep the
migration and these models in lock-step by hand.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# TIMESTAMPTZ everywhere (§3): tz-aware instants so ordering/age is unambiguous across
# horizontally-scaled instances. Ordering itself keys off explicit columns (position /
# the FK graph), never a timestamp — clock skew across instances makes those unreliable.
_TZ = TIMESTAMP(timezone=True)


class ThreadRow(Base):
    __tablename__ = "thread"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)
    # Reserved name on Declarative (``Base.metadata``), so the attribute is ``meta`` while
    # the column stays ``metadata``. No semantics in M4 — reserved for later (§3).
    meta: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)


class RunRow(Base):
    __tablename__ = "run"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # NULL = one-shot run with no thread/memory — M3's behavior, preserved (§3). Threads
    # are opt-in. ON DELETE not set: M4 never deletes threads/runs.
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("thread.id"), nullable=True)
    status: Mapped[str] = mapped_column(String)  # created|streaming|completed|failed
    model: Mapped[str] = mapped_column(String)  # logical id (never an engine name)
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)


class MessageRow(Base):
    __tablename__ = "message"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # thread_id is denormalized (also reachable via run) for direct ordered thread reads.
    thread_id: Mapped[str] = mapped_column(ForeignKey("thread.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"))
    role: Mapped[str] = mapped_column(String)  # user|assistant (system later)
    content: Mapped[str] = mapped_column(String)
    # Monotonic within the thread; THE ordering key (not timestamps — see _TZ note).
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(_TZ)

    # UNIQUE: a thread's positions are dense and monotonic; uniqueness is correct by
    # design. It is also the safety net for concurrency — append_turn serializes writes
    # with SELECT … FOR UPDATE on the thread row, but if that lock ever regressed, a
    # colliding position would fail loudly here instead of silently losing a turn (the
    # one race shared Postgres state introduced that a single-instance dict could not).
    __table_args__ = (Index("ix_message_thread_position", "thread_id", "position", unique=True),)
