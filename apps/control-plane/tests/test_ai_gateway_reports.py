"""AI gateway reports, sessions and session timelines.

The report's totals are SQL; the acceptance check is that, on seeded records, they equal
``GatewayTotals.of`` summed in Python (the definition the gateway's own report sums), overall and
for every dimension. Access: everyone reads their own records in full; at organization scope,
non-admins get totals and non-identifying groupings, and only admins see per-developer and
per-session detail or another developer's session.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from theygent_control_plane.ai_gateway.reports import DIMENSIONS, NONE_KEY, TOTALS
from theygent_ir.gateway import GatewayRequestRecord, GatewayTotals

T0 = datetime(2026, 10, 6, 9, tzinfo=UTC)
WINDOW = {"since": "2026-10-01T00:00:00Z", "until": "2026-10-15T00:00:00Z"}
PASSWORD = "user-pass-1234"
SONNET = {"kind": "anthropic", "model": "claude-sonnet-5-5"}
LOCAL = {"kind": "local", "model": "aux-local"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_user(client: TestClient, username: str, role: str) -> tuple[str, str]:
    user_id = client.post(
        "/users", json={"username": username, "password": PASSWORD, "role": role}
    ).json()["id"]
    token = client.post("/auth/login", json={"username": username, "password": PASSWORD}).json()[
        "token"
    ]
    return user_id, token


def record(n: int, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": f"rec_{n:05d}",
        "gateway_id": "gw-test",
        "started_at": (T0 + timedelta(minutes=n)).isoformat(),
        "endpoint": "messages",
        "status": 200,
        "outcome": "complete",
        "credential_kind": "oauth",
        "credential_profile": "passthrough",
        "client": "cli",
        "request_class": "main",
        "requested_model": "claude-opus-5-5",
        "served_model": "claude-opus-5-5",
        "usage": {"input_tokens": 3, "output_tokens": 100, "cache_read_tokens": 2000},
        "cost_usd": 0.0024,
        "pricing_version": "2026-09-25",
        "decision": {"action": "stay", "reasons": ["mid_conversation"]},
    }
    return {**base, **fields}


def route(target: dict[str, str], counterfactual: float | None, reason: str) -> dict[str, Any]:
    return {
        "action": "route",
        "target": target,
        "reasons": [reason],
        "counterfactual_usd": counterfactual,
    }


def seed(client: TestClient, records: list[dict[str, Any]]) -> None:
    service_key = client.post("/gateway/service-keys", json={"name": "gw"}).json()["token"]
    for start in range(0, len(records), 100):
        answer = client.post(
            "/gateway/events",
            json={"gateway_id": "gw-test", "records": records[start : start + 100]},
            headers=bearer(service_key),
        )
        assert answer.status_code == 200, answer.text


def varied(ada: str, bob: str) -> list[dict[str, Any]]:
    """Every case the totals distinguish, across developers, sessions, clients and days."""
    return [
        record(1, developer_id=ada, session_id="s-ada-1"),
        record(
            2,
            developer_id=ada,
            session_id="s-ada-1",
            request_class="subagent",
            decision=route(SONNET, 0.0011, "subagent_at_spawn"),
        ),
        record(
            3,
            developer_id=ada,
            session_id="s-ada-1",
            request_class="auxiliary",
            client="sdk-cli",
            decision=route(LOCAL, 0.0, "local_candidate"),
        ),
        record(4, developer_id=ada, session_id="s-ada-1", status=429, usage={}, cost_usd=None),
        record(
            5,
            developer_id=bob,
            session_id="s-bob-1",
            served_model="claude-imaginary-9",
            cost_usd=None,
        ),
        record(6, developer_id=bob, session_id="s-bob-1", outcome="upstream_broke"),
        record(
            7,
            developer_id=bob,
            session_id="s-bob-1",
            served_model="claude-sonnet-5-5",
            decision=route(SONNET, None, "cold_cache_boundary"),
        ),
        record(8, outcome="transport_error", status=None, usage={}, cost_usd=None),
        record(60 * 24 * 2, developer_id=bob, session_id="s-bob-2", request_class="compaction"),
    ]


def python_key(dimension: str, raw: dict[str, Any]) -> str:
    """What each report dimension means, computed from a record in Python."""
    r = GatewayRequestRecord.model_validate(raw)
    target = r.decision.target
    value: str | None = {
        "class": r.request_class,
        "client": r.client,
        "session": r.session_id,
        "model": r.requested_model,
        "served-model": r.served_model,
        "developer": r.developer_id,
        "decision": f"route:{target.model}" if r.decision.action == "route" and target else "stay",
        "reason": r.decision.reasons[0],
        "day": r.started_at.astimezone(UTC).date().isoformat(),
    }[dimension]
    return value if value is not None else NONE_KEY


def python_totals(records: list[dict[str, Any]]) -> GatewayTotals:
    total = GatewayTotals()
    for raw in records:
        total = total + GatewayTotals.of(GatewayRequestRecord.model_validate(raw))
    return total


def numbers(totals: dict[str, Any]) -> dict[str, Any]:
    return {k: totals[k] for k in GatewayTotals.model_fields}


def report(client: TestClient, token: str | None = None, **params: str) -> dict[str, Any]:
    response = client.get(
        "/gateway/report", params={**WINDOW, **params}, headers=bearer(token) if token else {}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ── the SQL is the shared definition ─────────────────────────────────────────


def test_the_totals_cover_every_field() -> None:
    assert set(TOTALS) == set(GatewayTotals.model_fields)


def test_the_report_sql_equals_the_shared_totals(client: TestClient) -> None:
    ada, _ = make_user(client, "ada", "editor")
    bob, _ = make_user(client, "bob", "editor")
    records = varied(ada, bob)
    seed(client, records)
    result = report(client, by=",".join(DIMENSIONS))
    assert numbers(result["total"]) == pytest.approx(python_totals(records).model_dump())
    assert result["total"]["saving_usd"] == pytest.approx(python_totals(records).saving_usd)
    for dimension in DIMENSIONS:
        expected: dict[str, list[dict[str, Any]]] = {}
        for raw in records:
            expected.setdefault(python_key(dimension, raw), []).append(raw)
        got = {row["key"]: numbers(row) for row in result["groups"][dimension]}
        assert set(got) == set(expected), dimension
        for key, rows in expected.items():
            assert got[key] == pytest.approx(python_totals(rows).model_dump()), (dimension, key)


def test_groups_are_ordered_by_spend(client: TestClient) -> None:
    seed(
        client,
        [
            record(1, client="a", cost_usd=0.01),
            record(2, client="b", cost_usd=0.5),
            record(3, client="c"),
        ],
    )
    keys = [row["key"] for row in report(client, by="client")["groups"]["client"]]
    assert keys == ["b", "a", "c"]


def test_the_report_names_prices_and_models_served_elsewhere(client: TestClient) -> None:
    seed(
        client,
        [record(1), record(2, served_model="claude-sonnet-5-5", pricing_version="2026-10-01")],
    )
    result = report(client)
    assert result["pricing_versions"] == ["2026-09-25", "2026-10-01"]
    assert result["served_differs"] is True
    assert (result["first"], result["last"]) == (
        T0.replace(minute=1).isoformat().replace("+00:00", "Z"),
        T0.replace(minute=2).isoformat().replace("+00:00", "Z"),
    )


def test_the_window_starts_inclusive_and_ends_exclusive(client: TestClient) -> None:
    seed(client, [record(n) for n in range(5)])
    since = (T0 + timedelta(minutes=1)).isoformat()
    until = (T0 + timedelta(minutes=3)).isoformat()
    assert report(client, since=since, until=until)["total"]["calls"] == 2


def test_the_default_window_is_the_last_week(client: TestClient) -> None:
    now = datetime.now(UTC)
    seed(
        client,
        [
            record(1, started_at=(now - timedelta(days=1)).isoformat()),
            record(2, started_at=(now - timedelta(days=10)).isoformat()),
        ],
    )
    response = client.get("/gateway/report")
    assert response.json()["total"]["calls"] == 1


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"by": "colour"}, "invalid_report_dimension"),
        (
            {"since": "2026-10-10T00:00:00Z", "until": "2026-10-01T00:00:00Z"},
            "invalid_report_range",
        ),
        ({"scope": "team"}, "invalid_report_scope"),
    ],
)
def test_bad_report_requests_fail_by_name(
    client: TestClient, params: dict[str, str], code: str
) -> None:
    response = client.get("/gateway/report", params=params)
    assert (response.status_code, response.json()["error"]["code"]) == (400, code)


# ── who sees what ────────────────────────────────────────────────────────────


@pytest.fixture
def team(client: TestClient) -> dict[str, tuple[str, str]]:
    people = {
        "ada": make_user(client, "ada", "editor"),
        "bob": make_user(client, "bob", "editor"),
        "vic": make_user(client, "vic", "viewer"),
    }
    seed(client, varied(people["ada"][0], people["bob"][0]))
    return people


def test_admins_see_everyone(client: TestClient, team: dict[str, tuple[str, str]]) -> None:
    everyone = report(client, by="developer,session")
    assert everyone["total"]["calls"] == 9
    assert {row["key"] for row in everyone["groups"]["developer"]} == {
        team["ada"][0],
        team["bob"][0],
        NONE_KEY,
    }
    ada_only = report(client, developer_id=team["ada"][0])
    assert ada_only["total"]["calls"] == 4 and ada_only["developer_id"] == team["ada"][0]


def test_a_developer_sees_their_own_records_in_full(
    client: TestClient, team: dict[str, tuple[str, str]]
) -> None:
    ada_id, ada = team["ada"]
    mine = report(client, ada, by="session,developer")
    assert mine["total"]["calls"] == 4
    assert [row["key"] for row in mine["groups"]["session"]] == ["s-ada-1"]
    assert [row["key"] for row in mine["groups"]["developer"]] == [ada_id]


@pytest.mark.parametrize("who", ["ada", "vic"])
def test_below_admin_the_organization_is_totals_and_anonymous_groupings(
    client: TestClient, team: dict[str, tuple[str, str]], who: str
) -> None:
    _, token = team[who]
    org = report(client, token, scope="org", by="class,model,decision,reason,day")
    assert org["total"]["calls"] == 9
    for by in ("developer", "session", "class,session"):
        refused = client.get(
            "/gateway/report", params={**WINDOW, "scope": "org", "by": by}, headers=bearer(token)
        )
        assert refused.status_code == 403
    other = team["bob"][0]
    for scope in ("mine", "org"):
        refused = client.get(
            "/gateway/report",
            params={**WINDOW, "scope": scope, "developer_id": other},
            headers=bearer(token),
        )
        assert refused.status_code == 403, scope
    assert (
        client.get(
            "/gateway/sessions", params={**WINDOW, "scope": "org"}, headers=bearer(token)
        ).status_code
        == 403
    )


def test_gateway_credentials_read_no_reports(
    client: TestClient, team: dict[str, tuple[str, str]]
) -> None:
    gateway_key = client.post("/gateway/keys", json={"name": "laptop"}).json()["token"]
    service_key = client.post("/gateway/service-keys", json={"name": "gw2"}).json()["token"]
    for token in (gateway_key, service_key):
        for path in ("/gateway/report", "/gateway/sessions", "/gateway/sessions/s-ada-1"):
            assert client.get(path, headers=bearer(token)).status_code == 401


# ── sessions ─────────────────────────────────────────────────────────────────


def sessions(client: TestClient, token: str | None = None, **params: str) -> list[dict[str, Any]]:
    response = client.get(
        "/gateway/sessions", params={**WINDOW, **params}, headers=bearer(token) if token else {}
    )
    assert response.status_code == 200, response.text
    return response.json()["sessions"]


def test_sessions_are_listed_most_recently_active_first(
    client: TestClient, team: dict[str, tuple[str, str]]
) -> None:
    listed = sessions(client)
    assert [s["session_id"] for s in listed] == ["s-bob-2", "s-bob-1", "s-ada-1"]
    ada_session = listed[2]
    assert ada_session["developer_id"] == team["ada"][0]
    assert ada_session["totals"]["calls"] == 4
    assert ada_session["first_at"] < ada_session["last_at"]
    page = sessions(client, limit="1", before=listed[0]["last_at"])
    assert [s["session_id"] for s in page] == ["s-bob-1"]


def test_a_developer_lists_only_their_sessions(
    client: TestClient, team: dict[str, tuple[str, str]]
) -> None:
    assert [s["session_id"] for s in sessions(client, team["ada"][1])] == ["s-ada-1"]
    assert sessions(client, team["vic"][1]) == []


def timeline(client: TestClient, session_id: str, token: str | None = None, **params: str) -> Any:
    return client.get(
        f"/gateway/sessions/{session_id}", params=params, headers=bearer(token) if token else {}
    )


def test_a_session_timeline_shows_each_request_and_its_decision(
    client: TestClient, team: dict[str, tuple[str, str]]
) -> None:
    detail = timeline(client, "s-ada-1", team["ada"][1]).json()
    assert detail["developer_id"] == team["ada"][0]
    assert detail["totals"]["calls"] == 4 and detail["truncated"] is False
    entries = detail["requests"]
    assert [e["id"] for e in entries] == ["rec_00001", "rec_00002", "rec_00003", "rec_00004"]
    routed = entries[1]
    assert routed["request_class"] == "subagent"
    assert routed["decision"] == {
        "action": "route",
        "target": SONNET,
        "reasons": ["subagent_at_spawn"],
        "counterfactual_usd": 0.0011,
        "applied": False,
    }
    assert routed["usage"]["cache_read_tokens"] == 2000
    assert entries[0]["decision"]["target"] is None


def test_another_developers_session_is_not_there(
    client: TestClient, team: dict[str, tuple[str, str]]
) -> None:
    assert timeline(client, "s-bob-1", team["ada"][1]).status_code == 404
    assert timeline(client, "s-bob-1").status_code == 200  # the admin
    missing = timeline(client, "s-nobody")
    assert (missing.status_code, missing.json()["error"]["code"]) == (
        404,
        "gateway_session_not_found",
    )


def test_a_long_timeline_pages_without_skipping_ties(client: TestClient) -> None:
    records = [record(n, session_id="s-long") for n in range(1, 6)]
    # The second and third start at the same instant, across the first page boundary: a cursor
    # on the start time alone would skip the third.
    records[2]["started_at"] = records[1]["started_at"]
    seed(client, records)
    seen: list[str] = []
    after: str | None = None
    while True:
        params = {"limit": "2", **({"after": after} if after else {})}
        page = timeline(client, "s-long", **params).json()
        seen += [e["id"] for e in page["requests"]]
        if not page["truncated"]:
            break
        after = page["requests"][-1]["id"]
    assert sorted(seen) == [f"rec_{n:05d}" for n in range(1, 6)]
    assert len(seen) == len(set(seen))


# ── setup warnings and seats ─────────────────────────────────────────────────


def test_setup_problems_are_flagged_by_the_gateways_own_rules(client: TestClient) -> None:
    seed(
        client,
        [
            record(1, shape={"tool_count": 201, "deferred_tool_count": 0}),
            record(2, shape={"tool_count": 30, "deferred_tool_count": 0}),
            record(3, shape={"tool_count": 11, "deferred_tool_count": 1}),  # tool search on
            record(4, shape={"tool_count": 14, "deferred_tool_count": 0}),  # too few to tell
            record(5, request_class="unlabeled", client_version="2.1.285"),
            record(6, request_class="unlabeled", client="python-httpx"),  # not Claude Code
            record(7, request_class="unrecognized", request_class_raw="speculative"),
            record(8, served_model="claude-imaginary-9", cost_usd=None),
            record(9, status=429, usage={}, cost_usd=None),  # nothing used: nothing to price
        ],
    )
    warnings = {w["code"]: w for w in report(client)["warnings"]}
    assert set(warnings) == {
        "tool_search_off",
        "hint_headers_off",
        "unrecognized_request_class",
        "unpriced_models",
    }
    assert warnings["tool_search_off"]["calls"] == 2
    assert warnings["hint_headers_off"]["calls"] == 1
    assert warnings["unrecognized_request_class"]["values"] == ["speculative"]
    assert (warnings["unpriced_models"]["calls"], warnings["unpriced_models"]["values"]) == (
        1,
        ["claude-imaginary-9"],
    )


def test_a_clean_setup_has_no_warnings(client: TestClient) -> None:
    seed(client, [record(1, shape={"tool_count": 11, "deferred_tool_count": 1})])
    assert report(client)["warnings"] == []


def seat(five: float, seven: float, reset_5h: datetime) -> dict[str, Any]:
    return {
        "five_hour_utilization": five,
        "seven_day_utilization": seven,
        "five_hour_resets_at": reset_5h.isoformat(),
        "seven_day_resets_at": (T0 + timedelta(days=5)).isoformat(),
    }


def test_seat_use_is_read_per_developer_and_never_across_a_reset(
    client: TestClient, team: dict[str, tuple[str, str]]
) -> None:
    ada, bob = team["ada"][0], team["bob"][0]
    window_1, window_2 = T0 + timedelta(hours=2), T0 + timedelta(hours=7)
    seed(
        client,
        [
            record(101, developer_id=ada, seat_allowance=seat(0.10, 0.02, window_1)),
            record(102, developer_id=ada, seat_allowance=seat(0.25, 0.03, window_1)),
            # The 5-hour window reset: 0.25 → 0.30 is a new window, not 0.05 of use.
            record(103, developer_id=ada, seat_allowance=seat(0.30, 0.04, window_2)),
            record(104, developer_id=ada, seat_allowance=seat(0.32, 0.05, window_2)),
            record(105, developer_id=bob, seat_allowance=seat(0.50, 0.20, window_1)),
        ],
    )
    seats = {s["developer_id"]: s for s in report(client)["seats"]}
    assert set(seats) == {ada, bob}
    assert seats[ada]["five_hour_utilization"] == 0.32
    assert seats[ada]["five_hour_used"] == pytest.approx(0.15 + 0.02)
    assert seats[ada]["seven_day_used"] == pytest.approx(0.03)
    assert (seats[bob]["five_hour_used"], seats[bob]["seven_day_used"]) == (0, 0)
    # A developer reads their own seat; at organization scope a non-admin sees none.
    assert [s["developer_id"] for s in report(client, team["ada"][1])["seats"]] == [ada]
    assert report(client, team["vic"][1], scope="org")["seats"] == []
