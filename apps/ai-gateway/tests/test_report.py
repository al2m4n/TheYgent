"""The observer report, from the local request log.

The acceptance check is Claude Code's own accounting: the bug-fix session replayed through the
gateway reports exactly the tokens Claude Code's ``modelUsage`` reported for that run. The saving
is pinned against the routing numbers the plan rests on (the Explore subagent trio: $0.1621 on Opus
5.5, $0.0810 estimated on Sonnet 5.5), priced on the frozen price list.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import theygent_ai_gateway.__main__ as entrypoint
from _fake_anthropic import fake_anthropic, serve
from _fixtures import Exchange, all_exchanges, load
from _prices import FROZEN_PRICES
from _support import send
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.observe import ResponseObserver
from theygent_ai_gateway.report import (
    NONE_KEY,
    TOOL_SEARCH_OFF_MIN_TOOLS,
    build_report,
    parse_when,
    render_json,
    render_text,
    report_from_log,
)
from theygent_ai_gateway.settings import ENV_STATE_DIR, GatewaySettings
from theygent_ir.gateway import GatewayRequestRecord

T0 = datetime(2026, 10, 10, 9, tzinfo=UTC)
PRICES = PriceBook.packaged()

#: Claude Code's ``modelUsage`` for the Phase 0 bug-fix run (spike/FINDINGS.md): everything filed
#: under the requested model.
CLAUDE_CODE_MODEL_USAGE = {
    "claude-opus-5-5": {
        "outputTokens": 800,
        "cacheReadInputTokens": 67_247,
        "cacheCreationInputTokens": 19_474,
    }
}


def replay(tmp_path: Path, exchanges: list[Exchange]) -> Path:
    """Send ``exchanges`` through a standalone gateway, in order; returns its log directory."""
    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url, gateway_id="gw-test", state_dir=tmp_path
        )
        with serve(create_app(settings, prices=FROZEN_PRICES)) as url:
            for exchange in exchanges:
                send(url, exchange)
    return tmp_path / "requests"


def conversation(name: str) -> list[Exchange]:
    return [e for e in all_exchanges() if e.conversation == name]


def cost_of(exchange: Exchange) -> float:
    observer = ResponseObserver(exchange.response_header("content-type"))
    observer.feed(exchange.response_bytes())
    summary = observer.finish()
    cost = PRICES.cost(summary.model, summary.usage)
    assert cost is not None
    return cost


def rec(n: int = 0, **fields: Any) -> GatewayRequestRecord:
    base: dict[str, Any] = {
        "id": f"r{n:05d}",
        "gateway_id": "gw-test",
        "started_at": T0 + timedelta(minutes=n),
        "endpoint": "messages",
        "status": 200,
        "outcome": "complete",
        "credential_kind": "oauth",
        "credential_profile": "passthrough",
        "request_class": "main",
        "decision": {"action": "stay", "reasons": ["mid_conversation"]},
    }
    return GatewayRequestRecord.model_validate({**base, **fields})


# ── against Claude Code's own accounting ─────────────────────────────────────


def test_the_report_matches_claude_codes_own_accounting(tmp_path: Path) -> None:
    exchanges = conversation("bugfix")[:3]
    assert [e.name for e in exchanges] == ["main-bugfix-1", "main-bugfix-2", "main-bugfix-3"]
    report = report_from_log(replay(tmp_path, exchanges), dimensions=["model", "session"])
    [(model, by_model)] = report.groups["model"].items()
    [by_session] = report.groups["session"].values()
    expected = CLAUDE_CODE_MODEL_USAGE[model]
    for totals in (by_model, by_session, report.total):
        assert totals.calls == 3
        assert totals.output_tokens == expected["outputTokens"]
        assert totals.cache_read_tokens == expected["cacheReadInputTokens"]
        assert totals.cache_write_tokens == expected["cacheCreationInputTokens"]
    assert report.total.cost_usd == pytest.approx(sum(cost_of(e) for e in exchanges))
    assert report.warnings == []  # a correctly set-up session raises nothing


def test_models_are_grouped_as_claude_code_files_them(tmp_path: Path) -> None:
    # The spike served these Opus requests on Sonnet: Claude Code still filed them under Opus.
    report = report_from_log(
        replay(tmp_path, conversation("explore-sonnet")), dimensions=["model", "served-model"]
    )
    assert {k: t.calls for k, t in report.groups["model"].items()} == {"claude-opus-5-5": 3}
    assert {k: t.calls for k, t in report.groups["served-model"].items()} == {
        "claude-sonnet-5-5": 3
    }
    assert report.served_differs


def test_the_saving_is_what_the_decisions_would_have_saved(tmp_path: Path) -> None:
    report = report_from_log(replay(tmp_path, conversation("explore")), dimensions=["class"])
    subagent, main = report.groups["class"]["subagent"], report.groups["class"]["main"]
    assert subagent.calls == subagent.routed_calls == 3
    assert subagent.cost_usd == pytest.approx(0.1621, abs=5e-5)
    assert subagent.with_routing_usd == pytest.approx(0.0810, abs=5e-5)
    assert main.routed_calls == 0 and main.with_routing_usd == pytest.approx(main.cost_usd)
    assert report.total.saving_usd == pytest.approx(subagent.saving_usd)
    assert report.total.routed_local_calls == 0


def test_routing_to_a_local_model_avoids_api_calls() -> None:
    usage = {"input_tokens": 900, "output_tokens": 20}
    local = {
        "action": "route",
        "target": {"kind": "local", "model": "aux-local"},
        "reasons": ["local_candidate"],
        "counterfactual_usd": 0.0,
    }
    report = build_report(
        [
            rec(1, request_class="auxiliary", usage=usage, cost_usd=0.005, decision=local),
            rec(2, usage=usage, cost_usd=0.05),
        ]
    )
    total = report.total
    assert (total.routed_calls, total.routed_local_calls, total.routed_local_tokens) == (1, 1, 920)
    assert total.cost_usd == pytest.approx(0.055)
    assert total.with_routing_usd == pytest.approx(0.05)
    assert total.saving_usd == pytest.approx(0.005)


# ── setup warnings ───────────────────────────────────────────────────────────


def codes(report_records: list[GatewayRequestRecord]) -> dict[str, int]:
    return {w.code: w.calls for w in build_report(report_records).warnings}


def test_tool_search_off_is_detected_from_the_request_shape(tmp_path: Path) -> None:
    report = report_from_log(replay(tmp_path, [load("main-tool-search-off")]))
    assert [w.code for w in report.warnings] == ["tool_search_off"]
    assert "ENABLE_TOOL_SEARCH=true" in report.warnings[0].message


@pytest.mark.parametrize(
    ("tools", "deferred", "warned"),
    [(201, 0, True), (TOOL_SEARCH_OFF_MIN_TOOLS, 0, True), (11, 1, False), (14, 0, False)],
)
def test_the_tool_search_rule(tools: int, deferred: int, warned: bool) -> None:
    shape = {"tool_count": tools, "deferred_tool_count": deferred}
    assert ("tool_search_off" in codes([rec(shape=shape)])) is warned


def test_claude_code_without_hint_headers_is_flagged() -> None:
    claude_code = rec(1, request_class="unlabeled", client="cli", client_version="2.1.285")
    other_client = rec(2, request_class="unlabeled", client="python-httpx")
    assert codes([claude_code, other_client]) == {"hint_headers_off": 1}


def test_an_unrecognized_request_class_is_flagged() -> None:
    record = rec(request_class="unrecognized", request_class_raw="speculative")
    report = build_report([record])
    [warning] = report.warnings
    assert warning.code == "unrecognized_request_class" and "speculative" in warning.message


def test_unpriced_calls_are_flagged_and_left_out_of_the_spend() -> None:
    unpriced = rec(1, served_model="claude-imaginary-9", usage={"output_tokens": 10})
    priced = rec(2, served_model="claude-opus-5-5", usage={"output_tokens": 10}, cost_usd=0.0002)
    report = build_report([unpriced, priced])
    assert report.total.unpriced_calls == 1
    assert report.total.cost_usd == pytest.approx(0.0002)
    [warning] = report.warnings
    assert warning.code == "unpriced_models" and "claude-imaginary-9" in warning.message


def test_unreadable_lines_are_counted_but_a_line_still_being_written_is_not(
    tmp_path: Path,
) -> None:
    good = rec(1).model_dump_json().encode()
    (tmp_path / "requests-000001.jsonl").write_bytes(good + b"\n{torn\n" + good[:20])
    report = report_from_log(tmp_path)
    assert report.total.calls == 1
    assert report.unreadable_lines == 1
    assert [w.code for w in report.warnings] == ["unreadable_lines"]


# ── seats, window, groups ────────────────────────────────────────────────────


def seat(five: float, seven: float, reset_5h: datetime) -> dict[str, Any]:
    return {
        "five_hour_utilization": five,
        "seven_day_utilization": seven,
        "five_hour_resets_at": reset_5h.isoformat(),
        "seven_day_resets_at": (T0 + timedelta(days=5)).isoformat(),
    }


def test_seat_use_is_read_per_developer_and_never_across_a_reset() -> None:
    window_1 = T0 + timedelta(hours=2)
    window_2 = T0 + timedelta(hours=7)
    records = [
        rec(1, developer_id="usr_a", seat_allowance=seat(0.10, 0.02, window_1)),
        rec(2, developer_id="usr_a", seat_allowance=seat(0.25, 0.03, window_1)),
        # The 5-hour window reset: 0.25 → 0.30 is a new window, not 0.05 of use.
        rec(3, developer_id="usr_a", seat_allowance=seat(0.30, 0.04, window_2)),
        rec(4, developer_id="usr_a", seat_allowance=seat(0.32, 0.05, window_2)),
        rec(5, developer_id="usr_b", seat_allowance=seat(0.50, 0.20, window_1)),
    ]
    seats = build_report(records).seats
    a, b = seats["usr_a"], seats["usr_b"]
    assert a.latest.five_hour_utilization == 0.32
    assert a.five_hour_used == pytest.approx(0.15 + 0.02)
    assert a.seven_day_used == pytest.approx(0.03)
    assert (b.five_hour_used, b.seven_day_used) == (0.0, 0.0)


def test_the_window_filters_by_start_time() -> None:
    records = [rec(n, usage={"output_tokens": 1}) for n in range(10)]
    report = build_report(records, since=T0 + timedelta(minutes=3), until=T0 + timedelta(minutes=6))
    assert report.total.calls == 3
    assert (report.first, report.last) == (T0 + timedelta(minutes=3), T0 + timedelta(minutes=5))


def test_groups_name_missing_values_and_decisions() -> None:
    route = {
        "action": "route",
        "target": {"kind": "anthropic", "model": "claude-sonnet-5-5"},
        "reasons": ["subagent_at_spawn"],
        "counterfactual_usd": 0.01,
    }
    report = build_report(
        [rec(1, decision=route), rec(2, session_id="s-1")],
        dimensions=["decision", "session", "reason"],
    )
    assert set(report.groups["decision"]) == {"route:claude-sonnet-5-5", "stay"}
    assert set(report.groups["session"]) == {NONE_KEY, "s-1"}
    assert set(report.groups["reason"]) == {"subagent_at_spawn", "mid_conversation"}


def test_an_unknown_dimension_is_refused() -> None:
    with pytest.raises(ValueError, match="colour"):
        build_report([], dimensions=["colour"])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30m", T0 - timedelta(minutes=30)),
        ("24h", T0 - timedelta(hours=24)),
        ("7d", T0 - timedelta(days=7)),
        ("2w", T0 - timedelta(weeks=2)),
        ("2026-10-01", datetime(2026, 10, 1, tzinfo=UTC)),
        ("2026-10-01T12:00:00+02:00", datetime(2026, 10, 1, 10, tzinfo=UTC)),
    ],
)
def test_parse_when(value: str, expected: datetime) -> None:
    assert parse_when(value, now=T0) == expected


def test_parse_when_refuses_what_it_cannot_read() -> None:
    with pytest.raises(ValueError, match="soon"):
        parse_when("soon")


# ── rendering and the command line ───────────────────────────────────────────


def test_text_names_requested_models_and_trims_long_groups() -> None:
    records = [
        rec(n, session_id=f"s-{n}", requested_model="claude-opus-5-5", served_model=served)
        for n, served in enumerate(["claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5-5"])
    ]
    text = render_text(build_report(records, dimensions=["model", "session"]), top=2)
    assert "Claude Code files tokens under the model it requested" in text
    assert "… 1 more" in text


def test_router_mode_shows_what_routing_saved() -> None:
    applied = {
        "action": "route",
        "target": {"kind": "anthropic", "model": "claude-sonnet-5-5"},
        "reasons": ["subagent_at_spawn"],
        "applied": True,
    }
    records = [
        rec(1, served_model="claude-sonnet-5-5", cost_usd=0.2, baseline_usd=0.5, decision=applied),
        rec(2, cost_usd=0.3),
    ]
    report = build_report(records, dimensions=["model"])
    assert (report.total.baseline_usd, report.total.cost_usd) == (pytest.approx(0.8), 0.5)
    text = render_text(report)
    assert "Without routing  $0.8000" in text
    assert "Saving           $0.3000 (37.5%)" in text
    assert "routed 1 · would route 0" in text
    assert "without routing" in text.split("By model")[1]
    # Observer-only traffic shows no "without routing": it is the spend.
    observed = render_text(build_report([rec(3, cost_usd=0.3)], dimensions=["model"]))
    assert "Without routing" not in observed and "routed" not in observed.replace("would route", "")


def test_shadow_runs_show_as_calibration() -> None:
    route = {
        "action": "route",
        "target": {"kind": "anthropic", "model": "claude-sonnet-5-5"},
        "reasons": ["subagent_at_spawn"],
        "counterfactual_usd": 0.2,
    }

    def shadow(outcome: str, output: int) -> dict[str, Any]:
        return {
            "target": route["target"],
            "outcome": outcome,
            "usage": {"output_tokens": output},
            "cost_usd": 0.05,
            "agreed": None if outcome != "answered" else output == 80,
        }

    records = [
        rec(
            1,
            usage={"output_tokens": 100},
            cost_usd=0.5,
            decision=route,
            shadow=shadow("answered", 80),
        ),
        rec(
            2,
            usage={"output_tokens": 100},
            cost_usd=0.5,
            decision=route,
            shadow=shadow("answered", 70),
        ),
        rec(
            3, usage={"output_tokens": 50}, cost_usd=0.5, decision=route, shadow=shadow("failed", 0)
        ),
        rec(4, usage={"output_tokens": 100}, cost_usd=0.5, decision=route),
    ]
    report = build_report(records, dimensions=["model"])
    assert set(report.shadows) == {"claude-sonnet-5-5"}
    text = render_text(report)
    assert "Shadow runs      3 (2 usable) · cost $0.1500" in text
    calibration = text.split("Calibration (shadow runs)")[1]
    # 150 written by the target where the requested model wrote 200; the failed one adds nothing.
    assert "claude-sonnet-5-5" in calibration and "75%" in calibration
    assert "50%" in calibration  # one of the two compared turns picked the same next action
    assert json.loads(render_json(report))["shadows"]["claude-sonnet-5-5"]["shadow_calls"] == 3
    # Without shadows, no calibration.
    assert "Shadow runs" not in render_text(build_report(records[3:], dimensions=["model"]))


def test_a_local_trial_calibrates_its_local_model() -> None:
    # Router mode served the subagent on Sonnet; the local trial measured the local model.
    served = {
        "action": "route",
        "target": {"kind": "anthropic", "model": "claude-sonnet-5-5"},
        "reasons": ["subagent_at_spawn"],
        "applied": True,
    }
    trial = {
        "target": {"kind": "local", "model": "coder-medium"},
        "outcome": "answered",
        "usage": {"output_tokens": 40},
        "cost_usd": 0.0,
        "agreed": True,
        "trial": True,
    }
    records = [
        rec(1, usage={"output_tokens": 50}, cost_usd=0.3, decision=served, shadow=trial),
        rec(2, usage={"output_tokens": 50}, cost_usd=0.3, decision=served),
    ]
    report = build_report(records, dimensions=["shadow-model", "decision"])
    by_shadow = report.groups["shadow-model"]
    assert set(by_shadow) == {"coder-medium", NONE_KEY}
    assert (by_shadow["coder-medium"].shadow_agreed_calls, by_shadow[NONE_KEY].calls) == (1, 1)
    # The decision's grouping still says where the requests went.
    assert set(report.groups["decision"]) == {"route:claude-sonnet-5-5"}
    assert set(report.shadows) == {"coder-medium"}
    assert "coder-medium" in render_text(report).split("Calibration (shadow runs)")[1]


def test_json_round_trips() -> None:
    data = json.loads(render_json(build_report([rec(1, usage={"output_tokens": 5})])))
    assert data["total"]["calls"] == 1 and data["total"]["output_tokens"] == 5
    assert data["groups"]["class"]["main"]["calls"] == 1


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as exit_info:
        entrypoint.main(argv)
    out, err = capsys.readouterr()
    code = exit_info.value.code
    return (code if isinstance(code, int) else 1), out, err


def test_the_report_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    replay(tmp_path, conversation("bugfix")[:3])
    code, out, _ = run_cli(["report", "--state-dir", str(tmp_path)], capsys)
    assert code == 0
    assert "Spend" in out and "claude-opus-5-5" in out and "67,247" in out
    code, out, _ = run_cli(
        ["report", "--state-dir", str(tmp_path), "--json", "--by", "session"], capsys
    )
    assert code == 0 and json.loads(out)["total"]["cache_read_tokens"] == 67_247


def test_the_report_command_reads_the_state_dir_from_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    replay(tmp_path, conversation("bugfix")[:1])
    monkeypatch.setenv(ENV_STATE_DIR, str(tmp_path))
    code, out, _ = run_cli(["report", "--json"], capsys)
    assert code == 0 and json.loads(out)["total"]["calls"] == 1


@pytest.mark.parametrize(
    ("argv", "code", "says"),
    [
        (["report", "--by", "colour"], 2, "choose from"),
        (["report", "--since", "soon"], 2, "neither a duration"),
        (["report"], 1, "no request log"),
    ],
)
def test_the_report_command_fails_by_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    code: int,
    says: str,
) -> None:
    monkeypatch.setenv(ENV_STATE_DIR, str(tmp_path / "empty"))
    exit_code, _, err = run_cli(argv, capsys)
    assert exit_code == code and says in err


def test_an_unknown_command_is_refused() -> None:
    with pytest.raises(SystemExit) as exit_info:
        entrypoint.main(["reprot"])
    assert "unknown command 'reprot'" in str(exit_info.value)
