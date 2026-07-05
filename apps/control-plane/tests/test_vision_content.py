"""Fast suite for multimodal ``llm`` content — the cross-plane vision follow-up.

The inference plane serves vision locally (mlx_vlm on ``/v1/chat/completions``), but a
vision agent could not yet be DRIVEN from a graph: an ``llm`` node's message ``content`` was a plain
string, so it could not carry an OpenAI ``image_url`` content block. The IR now lets a ``content``
be a list of content parts (text + image_url); these tests prove the part survives IR serialize +
the *real* gateway hop (the fake inference is a genuine HTTP server, so ``captured["messages"]`` is
what crossed the wire — exactly the OpenAI shape LiteLLM forwards to the engine).

What they pin:
  * a graph bound to a vision logical model with an image content block runs end-to-end, and the
    image block reaches the wire VERBATIM (the core requirement);
  * the ``$in`` template applies INSIDE a content part — both a text part and the image url — so the
    image can enter from the run input (``image_url="$in"``), the thing a string content can't do;
  * the ``detail`` hint forwards when set and is absent when unset (a clean OpenAI image_url);
  * a plain-string content is untouched (the string-only path still works alongside the new one).
"""

from __future__ import annotations

from _fake_inference import FakeInference
from _ir import llm_ir, vision_llm_ir
from fastapi.testclient import TestClient


def _run(client: TestClient, ir: dict, *, input_: str = "the run input") -> dict:
    return client.post("/graphs/runs", json={"ir": ir, "input": input_, "stream": False}).json()


def test_vision_graph_runs_end_to_end_and_image_survives_the_gateway_hop(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # A static image block + a templated text part. The run completes, and the messages that crossed
    # the real HTTP seam carry the image_url block verbatim (incl. ``detail``) with text rendered.
    data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
    ir = vision_llm_ir("Describe: $in", data_uri, detail="low")
    body = _run(client, ir, input_="what is in this image?")
    assert body["status"] == "completed"
    assert fake_inference.captured["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe: what is in this image?"},
                {"type": "image_url", "image_url": {"url": data_uri, "detail": "low"}},
            ],
        }
    ]


def test_image_url_can_come_from_a_graph_input(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # The headline: the image itself enters from the run input via ``$in`` inside the image part —
    # a vision agent driven by a graph, not a hardcoded image. The url is substituted on the wire.
    image = "https://example.com/cat.png"
    ir = vision_llm_ir("Describe the image.", "$in")
    body = _run(client, ir, input_=image)
    assert body["status"] == "completed"
    assert fake_inference.captured["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe the image."},
                {
                    "type": "image_url",
                    "image_url": {"url": image},
                },  # no ``detail`` → absent on wire
            ],
        }
    ]


def test_vision_logical_model_id_is_forwarded(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # The binding's logical id (a vision model) reaches the seam unchanged — the
    # logical-id invariant, here over a multimodal request.
    ir = vision_llm_ir("Describe: $in", "data:image/png;base64,AAAA")
    body = _run(client, ir, input_="hi")
    assert body["status"] == "completed"
    assert fake_inference.captured["model"] == "qwen2-vl-vision"


def test_string_content_still_works_alongside_multimodal(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # The string-only path is untouched: a plain-string content still renders to a plain string
    # on the wire (the union didn't wrap it), so existing text agents are unaffected.
    body = _run(client, llm_ir("Summarize: $in"), input_="a long document")
    assert body["status"] == "completed"
    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "Summarize: a long document"}
    ]
