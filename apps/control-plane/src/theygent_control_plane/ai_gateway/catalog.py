"""The fingerprint catalog: every kind of auxiliary request, what was observed of it, and how
it is to be routed.

A kind's review status and label are rules in the gateway config, the immutable document every
decision was made under; the catalog joins the latest config's rules onto the observed stats and
lists rules for kinds not seen yet. Changing a status saves a new config version.

Fingerprints are digests of the client's own prompt, so they change with client releases. The
kinds Phase 0 identified are therefore recognised by their shape instead, and only ever
*suggested*: the status stays an admin's decision.

* ``quota_check`` — Claude Code's startup check: one output token, no tools. It exists to read
  rate-limit state, so it is never routed.
* ``main_prefix_side_request`` — a side request that starts with the main conversation's first
  message and so rides its cache: cheap where it is, slow anywhere else.
* ``session_title`` — structured output, no tools, standalone: small and checkable, a local
  candidate.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from theygent_ir.gateway import (
    FingerprintEntry,
    FingerprintRule,
    FingerprintStatus,
    GatewayConfig,
    KnownKind,
)

from theygent_control_plane.ai_gateway.store import AiGatewayStore, ConfigDraft, draft_of
from theygent_control_plane.models import AiGatewayFingerprintRow

#: Whether an observed shape (and whether it rode the main conversation's cache) is a kind.
type Matcher = Callable[[dict[str, Any], bool | None], bool]

#: (kind, suggested status, matcher). Checked in order; the first match wins.
KNOWN_KINDS: tuple[tuple[KnownKind, FingerprintStatus, Matcher], ...] = (
    (
        "quota_check",
        "never_route",
        lambda shape, _: shape.get("max_tokens") == 1 and not shape.get("tool_count"),
    ),
    ("main_prefix_side_request", "never_route", lambda _, shares: shares is True),
    (
        "session_title",
        "allow_local",
        lambda shape, shares: (
            bool(shape.get("has_output_schema")) and not shape.get("tool_count") and shares is False
        ),
    ),
)


def recognise(
    shape: dict[str, Any] | None, shares_main_prefix: bool | None
) -> tuple[KnownKind, FingerprintStatus] | None:
    if shape is None and shares_main_prefix is None:
        return None
    for kind, status, matches in KNOWN_KINDS:
        if matches(shape or {}, shares_main_prefix):
            return kind, status
    return None


async def entries(
    session: AsyncSession, store: AiGatewayStore
) -> tuple[int | None, list[FingerprintEntry]]:
    """The latest config's version, and every kind: observed ones (most recently seen first),
    then rules for kinds not seen yet."""
    config = await store.latest_config(session)
    rules = {rule.fingerprint: rule for rule in config.fingerprints} if config else {}
    rows = await session.execute(
        select(AiGatewayFingerprintRow).order_by(
            AiGatewayFingerprintRow.last_seen_at.desc(), AiGatewayFingerprintRow.fingerprint
        )
    )
    listed: list[FingerprintEntry] = []
    for row in rows.scalars():
        rule = rules.pop(row.fingerprint, None)
        known = recognise(row.shape, row.shares_main_prefix)
        listed.append(
            FingerprintEntry.model_validate(
                {
                    "fingerprint": row.fingerprint,
                    "status": rule.status if rule else "unreviewed",
                    "label": rule.label if rule else None,
                    "first_seen": row.first_seen_at,
                    "last_seen": row.last_seen_at,
                    "request_count": row.request_count,
                    "client_versions": row.client_versions,
                    "shape": row.shape,
                    "shares_main_prefix": row.shares_main_prefix,
                    "known_kind": known[0] if known else None,
                    "suggested_status": known[1] if known else None,
                }
            )
        )
    for fingerprint in sorted(rules):
        rule = rules[fingerprint]
        listed.append(
            FingerprintEntry(fingerprint=fingerprint, status=rule.status, label=rule.label)
        )
    return (config.version if config else None), listed


def with_rule(
    fingerprint: str, status: FingerprintStatus, label: str | None
) -> Callable[[GatewayConfig | None], ConfigDraft]:
    """A config change setting one kind's rule and keeping everything else. ``unreviewed`` with
    no label removes the rule: no rule already means unreviewed."""
    rule = FingerprintRule(fingerprint=fingerprint, status=status, label=label)

    def change(latest: GatewayConfig | None) -> ConfigDraft:
        draft = draft_of(latest)
        rules = [r for r in draft.fingerprints if r.fingerprint != fingerprint]
        if status != "unreviewed" or label is not None:
            rules.append(rule)
        return draft.model_copy(update={"fingerprints": sorted(rules, key=lambda r: r.fingerprint)})

    return change


async def entry(
    session: AsyncSession, store: AiGatewayStore, fingerprint: str
) -> FingerprintEntry | None:
    _, listed = await entries(session, store)
    return next((e for e in listed if e.fingerprint == fingerprint), None)
