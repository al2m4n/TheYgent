"""End to end against the real thing: the Agent SDK drives headless Claude Code through a real
gateway to the real Anthropic API, once in personal mode and once in team mode, and the gateway's
totals per requested model must equal Claude Code's own per-model usage (``modelUsage``) — the
figure Claude Code shows its user.

Opt-in, because it spends real model usage on the caller's own Claude credentials (a claude.ai
login stored by Claude Code, ``ANTHROPIC_API_KEY`` or ``CLAUDE_CODE_OAUTH_TOKEN``):

    THEYGENT_E2E_CLAUDE_CODE=1 uv run --package theygent-ai-gateway \\
        pytest -m integration -n0 tests/test_e2e_claude_code.py

It skips cleanly without the opt-in, without the Agent SDK, or when Claude Code has no usable
credentials. ``THEYGENT_E2E_MODEL`` picks the model (default: Haiku 4.5, the cheapest).

Claude Code here runs with nothing from the caller's own setup: no settings files, and none of the
caller's ``CLAUDE*``/``ANTHROPIC*`` variables except the two credentials above — a caller inside
another Claude Code session would otherwise hand the child its base URL and session.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from _fake_anthropic import serve
from _support import FakeSink
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.configsync import StaticConfigSource
from theygent_ai_gateway.identity import StaticKeySource
from theygent_ai_gateway.report import LogReader, build_report
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ir.gateway import (
    GatewayConfig,
    GatewayKey,
    GatewayKeySet,
    GatewayRequestRecord,
    PolicyConfig,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("THEYGENT_E2E_CLAUDE_CODE", "").strip() != "1",
        reason="opt-in: set THEYGENT_E2E_CLAUDE_CODE=1 (spends real model usage)",
    ),
]

sdk = pytest.importorskip("claude_agent_sdk")

MODEL = os.environ.get("THEYGENT_E2E_MODEL", "").strip() or "claude-haiku-4-5"
#: Router mode's measured pair: Sonnet 5.5 takes Claude Code's Opus 5.5 requests as they are.
ROUTER_REQUESTED, ROUTER_TARGET = "claude-opus-5-5", "claude-sonnet-5-5"
PASSPHRASE = "theygent-e2e-" + secrets.token_hex(4)
#: Credentials Claude Code may use; every other CLAUDE*/ANTHROPIC* variable is the caller's own.
CREDENTIAL_VARIABLES = {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
SETTLE_SECONDS = 20.0


@pytest.fixture(autouse=True)
def _a_clean_claude_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("CLAUDE", "ANTHROPIC")) and name not in CREDENTIAL_VARIABLES:
            monkeypatch.delenv(name)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "workspace"
    directory.mkdir()
    (directory / "note.txt").write_text(f"{PASSPHRASE}\n")
    return directory


async def drive_claude_code(
    gateway_url: str, workspace: Path, extra_env: dict[str, str], *, model: str = MODEL
) -> Any:
    """One headless Claude Code prompt through the gateway, with the setup the AI Gateway page
    hands out; returns the SDK's result message."""
    options = sdk.ClaudeAgentOptions(
        model=model,
        cwd=str(workspace),
        # Claude Code's full toolset, so its prompt is large enough to cache: cache writes and
        # reads are where accounting goes wrong. Only Read runs without asking.
        allowed_tools=["Read"],
        max_turns=4,
        setting_sources=[],
        strict_mcp_config=True,
        env={
            "ANTHROPIC_BASE_URL": gateway_url,
            "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
            "ENABLE_TOOL_SEARCH": "true",
            **extra_env,
        },
    )
    result = None
    async for message in sdk.query(
        prompt="Read note.txt and answer with its exact contents, nothing else.",
        options=options,
    ):
        if isinstance(message, sdk.ResultMessage):
            result = message
    assert result is not None, "Claude Code ended without a result"
    if result.is_error and result.api_error_status in (401, 403):
        pytest.skip(f"Claude Code has no usable credentials ({result.api_error_status})")
    assert not result.is_error, f"Claude Code failed: {result.errors or result.result}"
    assert PASSPHRASE in (result.result or ""), "the prompt did not go through end to end"
    return result


def wait_for(read: Callable[[], list[GatewayRequestRecord]]) -> list[GatewayRequestRecord]:
    """The records once their count has stopped growing: the last exchange is recorded when its
    stream ends, a moment after Claude Code has its answer."""
    deadline = time.monotonic() + SETTLE_SECONDS
    seen, stable_since = -1, time.monotonic()
    while time.monotonic() < deadline:
        records = read()
        if len(records) != seen:
            seen, stable_since = len(records), time.monotonic()
        elif records and time.monotonic() - stable_since > 2.0:
            return records
        time.sleep(0.25)
    return read()


def assert_totals_match(records: list[GatewayRequestRecord], result: Any) -> None:
    unnamed = {r.endpoint for r in records if r.requested_model is None}
    assert "messages" not in unnamed, "a Messages call was recorded without its model"
    report = build_report(records, dimensions=["model"])
    groups = report.groups["model"].values()
    cached = sum(t.cache_read_tokens + t.cache_write_tokens for t in groups)
    assert cached > 0, "nothing was cached, so cache accounting went unchecked"
    usage: dict[str, dict[str, Any]] = result.model_usage or {}
    assert usage, "Claude Code reported no per-model usage"
    by_model = report.groups["model"]
    for model, figures in usage.items():
        seen = sorted(by_model)
        assert model in by_model, f"{model}: Claude Code used it; the gateway saw {seen}"
        totals = by_model[model]
        expected = {
            "input": figures.get("inputTokens", 0),
            "output": figures.get("outputTokens", 0),
            "cache read": figures.get("cacheReadInputTokens", 0),
            "cache write": figures.get("cacheCreationInputTokens", 0),
        }
        actual = {
            "input": totals.input_tokens,
            "output": totals.output_tokens,
            "cache read": totals.cache_read_tokens,
            "cache write": totals.cache_write_tokens,
        }
        assert actual == expected, f"{model}: gateway {actual} != Claude Code {expected}"
    # Calls without a model (the model list, a connectivity check) carry no tokens and no cost;
    # Claude Code does not count them either. Anything else is traffic only the gateway priced.
    for model in set(by_model) - set(usage):
        totals = by_model[model]
        tokens = (
            totals.input_tokens
            + totals.output_tokens
            + totals.cache_read_tokens
            + totals.cache_write_tokens
        )
        assert tokens == 0 and totals.cost_usd == 0, (
            f"the gateway counted {model} ({totals.calls} calls), which Claude Code did not"
        )
    # The setup the gateway hands out leaves nothing for its own warnings to flag.
    codes = {w.code for w in report.warnings}
    assert not codes & {"tool_search_off", "hint_headers_off"}, codes


async def test_personal_mode_totals_equal_claude_codes_usage(
    tmp_path: Path, workspace: Path
) -> None:
    settings = GatewaySettings(state_dir=tmp_path / "state", gateway_id="gw-e2e-personal")
    log_dir = settings.state_dir / "requests"
    with serve(create_app(settings)) as url:
        result = await drive_claude_code(url, workspace, {})
        records = await asyncio.to_thread(wait_for, lambda: list(LogReader(log_dir)))
    assert records and all(r.request_class != "unlabeled" for r in records if r.client_version)
    assert_totals_match(records, result)


async def test_team_mode_totals_equal_claude_codes_usage(tmp_path: Path, workspace: Path) -> None:
    gateway_key = "tyg_e2e_" + secrets.token_urlsafe(24)
    keys = GatewayKeySet(
        version=1,
        generated_at=datetime.now(UTC),
        keys=[
            GatewayKey(
                key_id="key_e2e",
                token_sha256=hashlib.sha256(gateway_key.encode()).hexdigest(),
                developer_id="usr_e2e",
                credential_profile="passthrough",
            )
        ],
    )
    settings = GatewaySettings(
        state_dir=tmp_path / "state",
        gateway_id="gw-e2e-team",
        auth_mode="team",
        # The control plane's three sources are stood in for in-process below; nothing calls it.
        control_plane_url="http://127.0.0.1:1",
        service_key="tygs_e2e_unused",
    )
    sink = FakeSink()
    observer = GatewayConfig(version=1, mode="observer", updated_at=datetime.now(UTC))
    app = create_app(
        settings,
        key_source=StaticKeySource(keys),
        # Applied from the first request, as a gateway that already synced it would be.
        config=observer,
        config_source=StaticConfigSource(observer),
        event_sink=sink,
    )
    with serve(app) as url:
        result = await drive_claude_code(
            url, workspace, {"ANTHROPIC_CUSTOM_HEADERS": f"X-TheYgent-Gateway-Key: {gateway_key}"}
        )
        delivered = await asyncio.to_thread(
            wait_for, lambda: [r for batch in sink.batches for r in batch.records]
        )
    assert delivered and {r.developer_id for r in delivered} == {"usr_e2e"}
    assert all(r.config_version == 1 for r in delivered)
    assert_totals_match(delivered, result)


async def test_router_mode_serves_opus_requests_on_sonnet(tmp_path: Path, workspace: Path) -> None:
    # Claude Code asks for Opus 5.5; the router serves its conversation on Sonnet 5.5 (cold at the
    # start, so free to move). Claude Code finishes its work, files the usage under the model it
    # asked for — as it always does — and the gateway records what actually served it.
    settings = GatewaySettings(state_dir=tmp_path / "state", gateway_id="gw-e2e-router")
    router = GatewayConfig(
        version=1,
        mode="router",
        policy=PolicyConfig(subagent_target=ROUTER_TARGET, main_target=ROUTER_TARGET),
        updated_at=datetime.now(UTC),
    )
    log_dir = settings.state_dir / "requests"
    with serve(create_app(settings, config=router)) as url:
        result = await drive_claude_code(url, workspace, {}, model=ROUTER_REQUESTED)
        records = await asyncio.to_thread(wait_for, lambda: list(LogReader(log_dir)))
    messages = [r for r in records if r.endpoint == "messages" and r.requested_model]
    routed = [r for r in messages if r.decision.applied]
    assert routed, "nothing was routed"
    assert all(r.served_model and r.served_model.startswith(ROUTER_TARGET) for r in routed)
    assert all(
        r.baseline_usd is not None and r.cost_usd is not None and r.baseline_usd > r.cost_usd
        for r in routed
    )
    assert not [r for r in messages if "target_refused" in r.decision.reasons]
    assert_totals_match(records, result)
