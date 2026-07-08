"""Engine-agnostic inline-think splitting — the raw-template reasoning shape.

OpenAI-compatible servers disagree on where a reasoning model's thinking travels: some emit
a separate ``reasoning_content`` delta field (already covered by the reasoning tests), others
leave it INLINE in ``delta.content`` as ``<think>…</think>`` tags. These tests pin the
control-plane's splitter: the tags and the thinking reach ``event: reasoning`` (never the
answer stream), ``run.output`` and the stored session turn hold only the clean answer, and a
tag split across chunk boundaries never leaks half a tag anywhere. Recognition is ONE leading
block max — a tag printed after answer text has started (code/docs about reasoning models) is
literal and must never eat the rest of the answer. Unit tests cover the splitter's
held-back-tail and one-leading-block semantics directly.
"""

from __future__ import annotations

import json

from _fake_inference import FULL_MESSAGE, INLINE_THINKING, FakeInference
from _ir import llm_ir
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from theygent_control_plane.reasoning import ThinkSplitter, split_think

# ── ThinkSplitter unit tests (pure, no fixtures) ─────────────────────────────


def _drain(splitter: ThinkSplitter, chunks: list[str]) -> tuple[str, str]:
    answer, reasoning = "", ""
    for chunk in chunks:
        a, r = splitter.push(chunk)
        answer += a
        reasoning += r
    a, r = splitter.flush()
    return answer + a, reasoning + r


def test_splitter_leading_block() -> None:
    assert split_think("<think>pondering</think>after") == ("after", "pondering")


def test_splitter_leading_whitespace_then_tag_is_still_markup() -> None:
    # "Effective stream start": whitespace before the tag doesn't disarm recognition —
    # engines pad the leading block with newlines.
    assert split_think("  \n<think>pondering</think>answer") == ("  \nanswer", "pondering")
    # ...also when the whitespace and the tag arrive in separate chunks.
    answer, reasoning = _drain(ThinkSplitter(), ["  ", "<th", "ink>t</think>ok"])
    assert (answer, reasoning) == ("  ok", "t")


def test_splitter_second_tag_after_answer_is_literal() -> None:
    # One leading block max: once the block closed and answer text flows, a second <think>
    # is literal answer text — nothing after it is eaten.
    answer, reasoning = split_think("<think>a</think>x<think>b</think>y")
    assert answer == "x<think>b</think>y"
    assert reasoning == "a"


def test_splitter_tag_after_answer_text_is_literal() -> None:
    # Recognition dies with the first non-whitespace answer text: a model that merely
    # PRINTS the tag mid-answer keeps its whole answer.
    text = "real answer <think>not thinking</think> more answer"
    assert split_think(text) == (text, "")


def test_splitter_code_snippet_with_tags_untouched() -> None:
    # Docs/code about reasoning models mention both tags mid-answer — all literal.
    text = "Wrap thinking in <think> and close with </think> tags."
    assert split_think(text) == (text, "")
    # Chunked the same way it would stream — including a would-be partial tag tail.
    answer, reasoning = _drain(ThinkSplitter(), ["Wrap thinking in <th", "ink> tags."])
    assert (answer, reasoning) == ("Wrap thinking in <think> tags.", "")


def test_splitter_tags_split_across_chunks() -> None:
    # Both tags of the leading block fragmented across chunk boundaries — no half-tag may
    # leak to either side.
    answer, reasoning = _drain(ThinkSplitter(), ["<th", "ink>pondering</thi", "nk>answer text"])
    assert answer == "answer text"
    assert reasoning == "pondering"


def test_splitter_char_by_char() -> None:
    # The degenerate stream (one character per chunk) still splits the leading block exactly.
    text = "<think>bb</think>ace"
    answer, reasoning = _drain(ThinkSplitter(), list(text))
    assert answer == "ace"
    assert reasoning == "bb"


def test_splitter_unclosed_block_stays_reasoning() -> None:
    # A model that spent its whole budget thinking: everything after <think> is reasoning,
    # including a held partial closing tag at flush; the answer stays genuinely blank.
    answer, reasoning = _drain(ThinkSplitter(), ["<think>all thinking", " no answer</thi"])
    assert answer == ""
    assert reasoning == "all thinking no answer</thi"


def test_splitter_false_partial_flushes_as_answer() -> None:
    # A leading "<th" that the stream ends on was NOT a tag — it is answer text (never
    # dropped).
    splitter = ThinkSplitter()
    answer, reasoning = splitter.push(" <th")
    assert (answer, reasoning) == (" ", "")  # tail held back, not yet released
    assert splitter.flush() == ("<th", "")


def test_splitter_false_partial_after_answer_not_held() -> None:
    # After answer text has started, a would-be partial tag is literal immediately — the
    # splitter holds nothing back once recognition is off.
    splitter = ThinkSplitter()
    assert splitter.push("a <th") == ("a <th", "")
    assert splitter.flush() == ("", "")


def test_splitter_stray_close_tag_is_literal_answer() -> None:
    # A closer with no opening tag is literal text.
    assert split_think("odd </think> text") == ("odd </think> text", "")


# ── the /runs paths ──────────────────────────────────────────────────────────


def _events(text: str) -> list[tuple[str | None, str]]:
    out: list[tuple[str | None, str]] = []
    event: str | None = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            out.append((event, line[len("data:") :].strip()))
            event = None
    return out


def _stream_events(client: TestClient, path: str, body: dict) -> list[tuple[str | None, str]]:
    with client.stream("POST", path, json=body) as resp:
        return _events("".join(resp.iter_text()))


def test_inline_think_stream_splits_reasoning_from_answer(pg_url: str) -> None:
    # The think span arrives as `event: reasoning`; the delta stream reassembles to the
    # clean answer — no tags, no thinking — even with both tags split across chunks.
    with FakeInference(mode="inline_think") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            events = _stream_events(
                client, "/runs", {"input": "hi", "model": "triage-fast", "stream": True}
            )
    reasoning = "".join(json.loads(d)["reasoning"] for ev, d in events if ev == "reasoning")
    deltas = "".join(json.loads(d)["delta"] for ev, d in events if ev == "delta")
    assert reasoning == INLINE_THINKING
    assert deltas == FULL_MESSAGE
    assert "<think" not in deltas and "think>" not in deltas
    assert json.loads(events[-2][1])["status"] == "completed"


def test_inline_think_output_and_session_turn_are_clean(pg_url: str) -> None:
    # The persisted output AND the stored session turn hold only the answer — inline
    # thinking must not pollute session replay (it would re-enter the prompt as history).
    with FakeInference(mode="inline_think") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            events = _stream_events(
                client,
                "/runs",
                {"input": "hi", "model": "triage-fast", "stream": True, "session_id": "s_think"},
            )
            run_id = json.loads(events[0][1])["runId"]
            assert client.get(f"/runs/{run_id}").json()["output"] == FULL_MESSAGE
            detail = client.get("/sessions/s_think").json()
            assert [(m["role"], m["content"]) for m in detail["messages"]] == [
                ("user", "hi"),
                ("assistant", FULL_MESSAGE),
            ]


def test_inline_think_non_stream_output_is_clean(pg_url: str) -> None:
    # The non-stream completion carries the thinking inline in the whole message — the
    # persisted output is the stripped answer (the thinking is progress, not the answer).
    with FakeInference(mode="inline_think") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            body = client.post(
                "/runs", json={"input": "hi", "model": "triage-fast", "stream": False}
            ).json()
            assert body["output"] == FULL_MESSAGE
            assert client.get(f"/runs/{body['runId']}").json()["output"] == FULL_MESSAGE


def test_inline_think_all_thinking_is_honest_empty(pg_url: str) -> None:
    # An unclosed block with no answer (budget exhausted mid-think, inline form) leaves the
    # output genuinely blank — the existing empty-output honesty path reports it and no
    # blank session turn is stored.
    with FakeInference(mode="inline_think_unclosed") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            events = _stream_events(
                client,
                "/runs",
                {"input": "hi", "model": "triage-fast", "stream": True, "session_id": "s_empty"},
            )
            reasoning = "".join(json.loads(d)["reasoning"] for ev, d in events if ev == "reasoning")
            assert INLINE_THINKING in reasoning
            assert [d for ev, d in events if ev == "delta"] == []  # no answer deltas at all
            run_id = json.loads(events[0][1])["runId"]
            got = client.get(f"/runs/{run_id}").json()
            assert got["status"] == "completed"
            assert got["output"] == ""
            assert got["error"] is not None and "maxTokens" in got["error"]
            assert client.get("/sessions/s_empty").json()["messages"] == []


def test_both_field_and_inline_forms_land_as_reasoning(pg_url: str) -> None:
    # A stream carrying BOTH shapes (a reasoning_content delta AND inline tags) doesn't
    # crash; both land as reasoning and the answer stays clean.
    with FakeInference(mode="inline_think_both") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            events = _stream_events(
                client, "/runs", {"input": "hi", "model": "triage-fast", "stream": True}
            )
    reasoning = "".join(json.loads(d)["reasoning"] for ev, d in events if ev == "reasoning")
    deltas = "".join(json.loads(d)["delta"] for ev, d in events if ev == "delta")
    assert "field thinking..." in reasoning
    assert INLINE_THINKING in reasoning
    assert deltas == FULL_MESSAGE


# ── the graph path (the walker's llm executor makes the same split) ──────────


def test_graph_inline_think_streams_and_persists_clean(pg_url: str) -> None:
    with FakeInference(mode="inline_think") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            events = _stream_events(
                client, "/graphs/runs", {"ir": llm_ir("$in"), "input": "hi", "stream": True}
            )
            reasoning = "".join(json.loads(d)["reasoning"] for ev, d in events if ev == "reasoning")
            deltas = "".join(json.loads(d)["delta"] for ev, d in events if ev == "delta")
            assert reasoning == INLINE_THINKING
            assert deltas == FULL_MESSAGE

            # Non-stream graph run: the journaled/persisted output is the stripped answer.
            body = client.post(
                "/graphs/runs", json={"ir": llm_ir("$in"), "input": "hi", "stream": False}
            ).json()
            assert body["output"] == FULL_MESSAGE
