"""End to end against a real local model: router mode answers a session-title request with a model
on a running inference plane, and Claude Code would read the answer as it reads Anthropic's.

Claude Code sends its side requests (session titles, prompt suggestions) only in interactive
sessions — a headless run sends none — so this drives the gateway with the recorded session-title
request's shape and settings, in our own wording, and fakes the Anthropic upstream: nothing is
spent, and the upstream must receive nothing.

Opt-in, because it needs an inference plane with a chat model registered under a logical id:

    THEYGENT_E2E_LOCAL_MODEL=aux-local uv run --package theygent-ai-gateway \\
        pytest -m integration -n0 tests/test_e2e_local_model.py

``THEYGENT_E2E_INFERENCE_PLANE_URL`` names the inference plane (default
``http://127.0.0.1:8081/v1``). It skips cleanly without the opt-in or when the plane is not up.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from _fake_anthropic import fake_anthropic, serve
from _fixtures import load
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.observe import Observation
from theygent_ai_gateway.settings import DEFAULT_INFERENCE_PLANE_URL, GatewaySettings
from theygent_ai_gateway.signals import read_request
from theygent_ir.gateway import (
    FingerprintRule,
    GatewayConfig,
    LocalModel,
    LocalRates,
    LocalRouting,
    LocalShare,
    PolicyConfig,
)

MODEL = os.environ.get("THEYGENT_E2E_LOCAL_MODEL", "").strip()
INFERENCE = (
    os.environ.get("THEYGENT_E2E_INFERENCE_PLANE_URL", "").strip() or DEFAULT_INFERENCE_PLANE_URL
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not MODEL, reason="opt-in: set THEYGENT_E2E_LOCAL_MODEL to a logical chat model id"
    ),
]

TITLE = load("auxiliary-session-title")
#: A cold model load can take a while; an answer after that takes well under a second.
LOAD_SECONDS = 120.0


def title_request() -> bytes:
    """The recorded session-title request — its settings, schema and billing block — with a prompt
    and a task of our own in place of the placeholders."""
    system = TITLE.body["system"]
    body = {
        **TITLE.body,
        "system": [
            system[0],
            {
                "type": "text",
                "text": "Write a short, sentence-case title (three to seven words) for the "
                "coding session the user describes. Answer with JSON holding one field, title.",
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "The pagination helper drops the last item when the total is a "
                        "multiple of the page size. Fix it and add a regression test.",
                    }
                ],
            }
        ],
    }
    return json.dumps(body, separators=(",", ":")).encode()


@pytest.fixture(scope="module", autouse=True)
def _a_warm_local_model() -> None:
    """Skip unless the inference plane serves the model; load it once, outside the timed runs."""
    try:
        httpx.post(
            f"{INFERENCE}/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
            timeout=LOAD_SECONDS,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.skip(f"the inference plane at {INFERENCE} does not serve {MODEL}: {exc}")


@pytest.mark.parametrize("stream", [True, False])
def test_a_local_model_writes_the_session_title(tmp_path: Path, stream: bool) -> None:
    raw = title_request()
    body = json.loads(raw)
    if not stream:
        body.pop("stream")
        raw = json.dumps(body, separators=(",", ":")).encode()
    headers = list(TITLE.request_headers)
    signals = read_request(dict(headers), raw, has_messages_body=True)
    assert signals.body is not None and signals.body.fingerprint is not None
    config = GatewayConfig(
        version=1,
        mode="router",
        policy=PolicyConfig(local_target=MODEL),
        fingerprints=[FingerprintRule(fingerprint=signals.body.fingerprint, status="allow_local")],
        updated_at=datetime.now(UTC),
    )
    seen: list[Observation] = []
    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url,
            inference_plane_url=INFERENCE,
            gateway_id="gw-e2e-local",
            state_dir=tmp_path,
        )
        with serve(create_app(settings, observe=seen.append, config=config)) as url:
            answer = httpx.post(
                f"{url}/v1/messages",
                content=raw,
                headers=[*headers, ("x-fake-fixture", TITLE.name)],
                timeout=60,
            )
            deadline = time.monotonic() + 10
            while not seen:
                assert time.monotonic() < deadline, "no observation"
                time.sleep(0.01)
    assert upstream.requests == [], "the local model did not answer; the upstream did"
    assert answer.status_code == 200
    if stream:
        assert answer.headers["content-type"].startswith("text/event-stream")
        text = "".join(
            json.loads(line[6:])["delta"]["text"]
            for line in answer.text.splitlines()
            if line.startswith("data: ") and '"content_block_delta"' in line
        )
    else:
        message = answer.json()
        assert (message["type"], message["model"]) == ("message", MODEL)
        text = message["content"][0]["text"]
    title = json.loads(text)
    assert set(title) == {"title"} and isinstance(title["title"], str) and title["title"].strip()
    [observed] = seen
    assert observed.decision.applied and observed.decision.target is not None
    assert (observed.decision.target.kind, observed.response.model) == ("local", MODEL)
    assert observed.cost_usd == 0.0
    usage = observed.response.usage
    assert usage.input_tokens > 0 and usage.output_tokens > 0
    baseline = PriceBook.packaged().cost(TITLE.body["model"], usage)
    assert baseline is not None and observed.baseline_usd == pytest.approx(baseline)


def test_a_local_conversation_is_compacted_by_its_local_model(tmp_path: Path) -> None:
    # The recorded main turn and its compaction, with every main conversation served locally: the
    # compaction reads the conversation's cache, so it runs on the same local model — nothing
    # reaches Anthropic — and costs what the model's rates say.
    rates = LocalRates(input=0.5, output=2.0)
    config = GatewayConfig(
        version=1,
        mode="router",
        policy=PolicyConfig(
            local_models=[LocalModel(model=MODEL, context_tokens=32_768, tools=True, rates=rates)],
            local_routing=LocalRouting(main=LocalShare(share=1.0)),
        ),
        updated_at=datetime.now(UTC),
    )
    seen: list[Observation] = []
    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url,
            inference_plane_url=INFERENCE,
            gateway_id="gw-e2e-local-compaction",
            state_dir=tmp_path,
        )
        with serve(create_app(settings, observe=seen.append, config=config)) as url:
            for exchange in (load("main-bugfix-1"), load("compaction-manual")):
                count = len(seen)
                answer = httpx.post(
                    f"{url}{exchange.target}",
                    content=exchange.request_bytes(),
                    headers=[*exchange.request_headers, ("x-fake-fixture", exchange.name)],
                    timeout=LOAD_SECONDS,
                )
                assert answer.status_code == 200, answer.text
                deadline = time.monotonic() + 10
                while len(seen) <= count:
                    assert time.monotonic() < deadline, "no observation"
                    time.sleep(0.01)
    main, compaction = seen
    print(
        "\nlocal main and compaction:",
        [
            (o.decision.reasons, o.timing.total_ms, o.response.usage.output_tokens, o.cost_usd)
            for o in seen
        ],
    )
    assert upstream.requests == [], "Anthropic was asked"
    assert main.decision.applied and main.decision.reasons[-1] == "local_share"
    assert compaction.decision.applied and compaction.decision.target is not None
    assert compaction.decision.target.model == MODEL
    assert compaction.decision.reasons == ["compaction", "follows_warm_cache"]
    usage = compaction.response.usage
    assert usage.output_tokens > 0
    expected = (usage.input_tokens * 0.5 + usage.output_tokens * 2.0) / 1_000_000
    assert compaction.cost_usd == pytest.approx(expected)


def test_the_request_is_the_recorded_one_in_our_words() -> None:
    # Everything the gateway decides on but the text is as Claude Code sent it.
    ours, recorded = json.loads(title_request()), TITLE.body
    assert {k: v for k, v in ours.items() if k not in ("system", "messages")} == {
        k: v for k, v in recorded.items() if k not in ("system", "messages")
    }
    assert ours["system"][0] == recorded["system"][0]
