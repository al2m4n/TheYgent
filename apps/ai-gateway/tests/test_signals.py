"""Request signals, read from real Claude Code traffic (the fixtures) and from hostile input.

Every parser here must be total: odd headers or a body that is not what it should be produce
``None`` or a named parse error, never an exception — the request is forwarded regardless.
"""

from __future__ import annotations

import copy
import json

import pytest
from _fixtures import Exchange, all_exchanges, load
from theygent_ai_gateway.signals import (
    BodySignals,
    as_token,
    body_signals,
    client_identity,
    credential_kind,
    parse_hints,
    read_request,
)

EXCHANGES = all_exchanges()


def _signals(exchange: Exchange) -> BodySignals:
    signals = body_signals(exchange.request_bytes())
    assert signals is not None
    return signals


def _headers(exchange: Exchange) -> dict[str, str]:
    return dict(exchange.request_headers)


# ── hints ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("exchange", EXCHANGES, ids=lambda e: e.name)
def test_request_class_matches_what_claude_code_sent(exchange: Exchange) -> None:
    hints = parse_hints(_headers(exchange))
    assert hints.request_class == exchange.request_class
    assert hints.request_class_raw is None


def test_subagent_hints() -> None:
    hints = parse_hints(_headers(load("subagent-explore-2")))
    assert hints.agent_type == "Explore"
    assert hints.agent_id is not None
    assert hints.session_id == parse_hints(_headers(load("main-explore-1"))).session_id
    assert hints.prompt_id is not None


def test_prev_tool_durations_are_parsed() -> None:
    durations = parse_hints(_headers(load("subagent-explore-2"))).prev_tool_durations
    assert durations, "the second subagent turn reports the tools its first turn ran"
    assert all(isinstance(ms, int) and ms >= 0 for _, ms in durations)


def test_compaction_hint() -> None:
    assert parse_hints(_headers(load("compaction-manual"))).compaction == "manual"


def test_missing_class_is_unlabeled() -> None:
    assert parse_hints({}).request_class == "unlabeled"


def test_an_unknown_class_is_kept_visible_not_folded_into_another() -> None:
    hints = parse_hints({"x-claude-code-request-class": "speculative draft"})
    assert hints.request_class == "unrecognized"
    assert hints.request_class_raw == "speculative-draft"  # made token-safe, still recognisable


def test_malformed_hint_values_are_dropped_not_raised() -> None:
    hints = parse_hints(
        {
            "x-claude-code-session-id": "has spaces in it",
            "x-claude-code-compaction": "sometimes",
            "x-claude-code-prev-tool-durations": "Read=4;;=3;Bash=x;Edit%3Dweird=12;Bash=742",
        }
    )
    assert hints.session_id is None
    assert hints.compaction is None
    # Kept: well-formed entries. Dropped: empty, unnamed, non-numeric, and a name that decodes
    # to something that may not leave the gateway ("Edit=weird").
    assert hints.prev_tool_durations == (("Read", 4), ("Bash", 742))


@pytest.mark.parametrize(
    ("raw", "token"),
    [("ok.value", "ok.value"), ("  spaces  here ", "spaces-here"), ("-lead", "lead"), ("…", "x")],
)
def test_as_token_always_yields_a_valid_token(raw: str, token: str) -> None:
    assert as_token(raw) == token


# ── client and credential ────────────────────────────────────────────────────


@pytest.mark.parametrize("exchange", EXCHANGES, ids=lambda e: e.name)
def test_the_client_is_identified(exchange: Exchange) -> None:
    identity = client_identity(exchange.request_header("user-agent"))
    expected = "sdk-cli" if exchange.source in ("headless", "derived") else "cli"
    assert identity.client == expected
    assert identity.version == "2.1.285"


@pytest.mark.parametrize(
    ("user_agent", "client", "version"),
    [
        ("claude-cli/2.1.285 (external, claude-desktop)", "claude-desktop", "2.1.285"),
        ("claude-cli/2.2.0 (cli)", "cli", "2.2.0"),
        ("Bun/1.4.3", "Bun", None),
        ("curl/8.7.1", "curl", None),
        ("", None, None),
        (None, None, None),
    ],
)
def test_client_identity_variants(
    user_agent: str | None, client: str | None, version: str | None
) -> None:
    identity = client_identity(user_agent)
    assert (identity.client, identity.version) == (client, version)


@pytest.mark.parametrize(
    ("headers", "kind"),
    [
        ({"authorization": "Bearer sk-ant-oat01-x"}, "oauth"),
        ({"authorization": "Bearer sk-ant-api03-x"}, "api_key"),
        ({"x-api-key": "sk-ant-api03-x"}, "api_key"),
        ({"authorization": "Bearer tyg_developer_key"}, "gateway_key"),
        ({"authorization": "Bearer something-else"}, "other"),
        ({"authorization": "Basic abc"}, "other"),
        ({}, "none"),
        # The gateway's own identity header is not the upstream credential.
        ({"x-theygent-gateway-key": "tyg_k", "authorization": "Bearer sk-ant-oat01-x"}, "oauth"),
    ],
)
def test_credential_kind(headers: dict[str, str], kind: str) -> None:
    assert credential_kind(headers) == kind


def test_every_fixture_is_a_claude_ai_login() -> None:
    assert {credential_kind(_headers(e)) for e in EXCHANGES} == {"oauth"}


# ── body shape ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("exchange", EXCHANGES, ids=lambda e: e.name)
def test_shape_counts_match_the_body(exchange: Exchange) -> None:
    signals = _signals(exchange)
    body = exchange.body
    assert signals.model == body["model"]
    assert signals.shape.tool_count == len(body.get("tools") or [])
    assert signals.shape.message_count == len(body["messages"])
    assert signals.shape.body_bytes == len(exchange.request_bytes())
    assert signals.stream is (body.get("stream") is True)


def test_main_turns_carry_claude_codes_5x_settings() -> None:
    signals = _signals(load("main-bugfix-1"))
    assert signals.shape.thinking == "adaptive"
    assert signals.shape.effort == "medium"
    assert signals.shape.max_tokens == 128000
    assert signals.shape.has_context_management


def test_mid_conversation_system_messages_are_detected() -> None:
    # Haiku 4.5 rejects these, so a router must know before choosing it.
    assert _signals(load("subagent-haiku-system-role-400")).shape.has_mid_conversation_system
    assert not _signals(load("auxiliary-session-title")).shape.has_mid_conversation_system


def test_tool_search_off_and_on_are_told_apart() -> None:
    off = _signals(load("main-tool-search-off")).shape
    on = _signals(load("main-tool-search-on")).shape
    assert off.tool_count > 100 and off.deferred_tool_count == 0
    assert on.deferred_tool_count >= 1


def test_the_session_title_request() -> None:
    signals = _signals(load("auxiliary-session-title"))
    assert signals.shape.has_output_schema
    assert signals.shape.tool_count == 0
    assert not signals.is_quota_probe
    assert signals.safeguards == ()


def test_the_quota_check_is_recognised() -> None:
    assert _signals(load("auxiliary-quota-check-429")).is_quota_probe
    assert not any(_signals(e).is_quota_probe for e in EXCHANGES if e.request_class != "auxiliary")


def test_safety_traffic_is_recognised() -> None:
    assert _signals(load("auxiliary-with-safeguards")).safeguards == ("dangerous_tool_use",)
    others = [e for e in EXCHANGES if e.name != "auxiliary-with-safeguards"]
    assert all(_signals(e).safeguards == () for e in others)


def test_cache_ttls_are_collected() -> None:
    ttls = _signals(load("main-bugfix-1")).cache_ttls
    assert ttls, "Claude Code marks its prompt for caching"
    assert set(ttls) <= {"5m", "1h"}


# ── fingerprint and conversation identity ────────────────────────────────────


def test_turns_of_one_conversation_share_fingerprint_and_first_message() -> None:
    turns = [_signals(load(f"main-bugfix-{n}")) for n in (1, 2, 3)]
    assert len({t.fingerprint for t in turns}) == 1
    assert len({t.first_message_digest for t in turns}) == 1


def test_different_kinds_have_different_fingerprints() -> None:
    kinds = ["main-bugfix-1", "subagent-explore-1", "auxiliary-session-title", "compaction-manual"]
    fingerprints = {_signals(load(name)).fingerprint for name in kinds}
    assert len(fingerprints) == len(kinds) - 1  # compaction reuses the main prompt and tools
    assert (
        _signals(load("compaction-manual")).fingerprint
        == _signals(load("main-bugfix-1")).fingerprint
    )


def test_fingerprint_ignores_the_attribution_block() -> None:
    # Block 0 carries the client version and a per-conversation fingerprint: not the kind.
    body = copy.deepcopy(load("auxiliary-session-title").body)
    assert body["system"][0]["text"].startswith("x-anthropic-billing-header:")
    before = body_signals(json.dumps(body).encode())
    body["system"][0]["text"] = "x-anthropic-billing-header: cc_version=9.9.9.9; cc_entrypoint=cli;"
    after = body_signals(json.dumps(body).encode())
    assert before is not None and after is not None
    assert after.fingerprint == before.fingerprint


def test_fingerprint_ignores_a_users_mcp_tools() -> None:
    body = copy.deepcopy(load("main-tool-search-on").body)
    before = body_signals(json.dumps(body).encode())
    body["tools"].append({"name": "mcp__someones_crm__lookup", "input_schema": {"type": "object"}})
    after = body_signals(json.dumps(body).encode())
    assert before is not None and after is not None
    assert after.fingerprint == before.fingerprint
    assert after.shape.tool_count == before.shape.tool_count + 1


def test_fingerprint_changes_with_the_output_schema() -> None:
    body = copy.deepcopy(load("auxiliary-session-title").body)
    before = body_signals(json.dumps(body).encode())
    body["output_config"]["format"] = {"type": "json_schema", "schema": {"type": "object"}}
    after = body_signals(json.dumps(body).encode())
    assert before is not None and after is not None
    assert after.fingerprint != before.fingerprint


# ── hostile input never raises ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[1, 2, 3]",
        b'"a string"',
        b"\xff\xfe binary",
        b'{"messages": "not a list", "tools": {"a": 1}, "system": 42, "max_tokens": "big"}',
        b'{"thinking": "adaptive", "output_config": [], "model": 7, "safeguards": "yes"}',
        b'{"messages": [{"role": "system"}], "max_tokens": -5, "safeguards": [1, {"type": "x y"}]}',
    ],
)
def test_odd_bodies_never_raise(body: bytes) -> None:
    signals = read_request({}, body, has_messages_body=True)
    if signals.body is None:
        assert "body_not_json" in signals.parse_errors or "body" in signals.parse_errors
    else:
        assert signals.body.shape.max_tokens is None or signals.body.shape.max_tokens >= 0


def test_headers_without_a_body_endpoint_still_yield_signals() -> None:
    signals = read_request(
        {"user-agent": "claude-cli/2.1.285 (external, cli)", "x-claude-code-request-class": "main"},
        b"",
        has_messages_body=False,
    )
    assert signals.body is None
    assert signals.parse_errors == ()
    assert signals.client.client == "cli"
    assert signals.hints.request_class == "main"


def test_a_prompts_size_is_estimated_at_claude_codes_bytes_per_token() -> None:
    from theygent_ai_gateway.signals import estimated_tokens

    # Real Claude Code requests run 2.8 to 3.0 bytes a token; the estimate errs high.
    assert estimated_tokens(2_750) == 1_000
    assert estimated_tokens(int(128_000 * 2.9)) > 128_000
