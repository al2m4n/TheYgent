"""Fast suite for the reasoning ("thinking") capability — detected from the chat template.

A reasoning model is identified by its chat template opening a ``<think>`` section (or gating one
behind ``enable_thinking``), which is the honest *local* signal — no network, no model-name guess.
These cover the pure detector + the static MLX read, and that the field rides the wire.
"""

from __future__ import annotations

import json
from pathlib import Path

from _fake_upstream import FakeUpstreamLauncher
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.launcher import _read_mlx_reasoning, _template_implies_reasoning


def test_template_markers() -> None:
    assert _template_implies_reasoning("{% generation %}<think>\n{% endgeneration %}") is True
    assert _template_implies_reasoning("{%- if enable_thinking %}...") is True
    assert _template_implies_reasoning("you are a helpful assistant") is False
    # the list-of-{name,template} form some repos ship
    assert _template_implies_reasoning([{"name": "default", "template": "x <think> y"}]) is True
    # unrecognised shapes are False, never a crash
    assert _template_implies_reasoning(None) is False
    assert _template_implies_reasoning(123) is False


def test_read_mlx_reasoning_from_local_tokenizer_config(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "tokenizer_config.json").write_text(
        json.dumps({"chat_template": "system\n<think>\n{{ content }}"})
    )
    assert _read_mlx_reasoning(str(model_dir), "local-path") is True

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "tokenizer_config.json").write_text(json.dumps({"chat_template": "just chat"}))
    assert _read_mlx_reasoning(str(plain), "local-path") is False
    # a model with no tokenizer_config at all → False (not an error)
    assert _read_mlx_reasoning(str(tmp_path / "missing"), "local-path") is False


def test_capabilities_endpoint_carries_reasoning() -> None:
    # The fake handle advertises caps without a reasoning flag → it serializes as False (the
    # additive field is always present on the wire for the UI to badge).
    with TestClient(create_app(launcher=FakeUpstreamLauncher(), enable_reaper=False)) as c:
        c.put("/admin/models/cap-probe", json={"binding": "mlx", "source": "hf", "model": "m"})
        caps = c.get("/admin/models/cap-probe/capabilities").json()
        assert caps["reasoning"] is False
        assert "toolCalling" in caps


# ── the shared, pure detectors (also drive the browse-time catalog hints) ────


def test_tool_marker_detection() -> None:
    from theygent_inference_plane.capabilities import template_implies_tools

    assert template_implies_tools("{%- if tools %}<tool_call>{% endif %}") is True
    assert template_implies_tools("emits <tool_call>{{name}}</tool_call>") is True
    assert template_implies_tools("{%- for tool in tools %}") is True
    assert template_implies_tools("just a plain chat template") is False
    assert template_implies_tools(None) is False


def test_vision_detection_signals() -> None:
    from theygent_inference_plane.capabilities import detect_vision

    # structural config key (an image processor) — the strongest signal
    assert detect_vision(config_keys=["processor_config", "tokenizer_config"]) is True
    # a VL architecture / model_type marker
    assert detect_vision(architectures=["Qwen2_5_VLForConditionalGeneration"]) is True
    assert detect_vision(model_type="qwen2vl") is True
    # image handling in the template alone
    assert detect_vision(template="<|vision_start|><|image_pad|>") is True
    # a text model that shares ...ForConditionalGeneration (T5/BART) is NOT vision
    assert detect_vision(architectures=["T5ForConditionalGeneration"], model_type="t5") is False
    assert detect_vision(architectures=["Qwen2ForCausalLM"], model_type="qwen2") is False


def test_chat_template_from_config_both_shapes() -> None:
    from theygent_inference_plane.capabilities import chat_template_from_config

    assert chat_template_from_config({"chat_template_jinja": "A"}) == "A"
    assert chat_template_from_config({"tokenizer_config": {"chat_template": "B"}}) == "B"
    assert chat_template_from_config({"architectures": ["X"]}) is None
    assert chat_template_from_config(None) is None
