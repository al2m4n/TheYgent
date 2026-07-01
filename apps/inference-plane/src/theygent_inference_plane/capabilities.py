"""Static, network-free capability detection from a model's chat template + config.

These are the SAME heuristics the post-install capability probe uses against a *running* engine
(``launcher.py``), factored out so the discovery catalog (``catalog.py``) can derive best-effort
capabilities at **browse time** — from the small metadata Hugging Face returns *without* downloading
weights (the chat template, ``architectures``, the GGUF header). So the browser can show
``reasoning`` / ``tool_calling`` / ``vision`` before the user installs anything.

Everything here is pure string/dict inspection — **no network, no subprocess, no engine**. It is
deliberately best-effort: a chat-template marker is a strong hint, not a guarantee, and template
conventions vary by model family. The authoritative source stays the live capability **probe**
(``/admin/models/{id}/capabilities``), which interrogates the spawned engine and reports
``approximate=False`` where it truly probes. Discovery hints and the probe are complementary, not
redundant — the probe confirms what the catalog only guesses.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# ── reasoning ("thinking") ───────────────────────────────────────────────────
# A reasoning model exposes a hidden chain-of-thought: the chat template opens a ``<think>`` section
# (Qwen3, DeepSeek-R1 distills, …) or gates one behind ``enable_thinking``. The template is the
# honest local signal — no network, no model-name guessing (so Qwen2.5-Instruct reads False).
REASONING_MARKERS = ("<think>", "enable_thinking", "reasoning_content")

# ── tool / function calling ──────────────────────────────────────────────────
# A tool-calling template branches on a ``tools`` variable and emits ``<tool_call>`` /
# ``tool_calls`` (OpenAI-style). Markers are matched against the lowercased template.
TOOL_MARKERS = (
    "<tool_call>",
    "tool_calls",
    "if tools",
    "for tool",
    "{{ tools",
    "{{- tools",
    "<tools>",
)

# ── vision (VLM) ─────────────────────────────────────────────────────────────
# Three independent signals, any one of which marks a vision model: a structural key in the config
# (an image processor / vision tower), a VL family marker in the architecture/model_type, or image
# handling in the chat template. Bare ``"...ForConditionalGeneration"`` is intentionally NOT a
# marker (T5/BART are text-only seq2seq models that share it) — we require an actual VL signal.
VISION_CONFIG_KEYS = (
    "vision_config",
    "processor_config",
    "image_token_id",
    "image_token_index",
    "vision_tower",
    "mm_projector",
)
VISION_ARCH_MARKERS = (
    "vl",  # qwen2vl, qwen2_5_vl, …
    "vision",
    "llava",
    "idefics",
    "mllama",
    "smolvlm",
    "paligemma",
    "internvl",
    "minicpmv",
    "pixtral",
    "fuyu",
    "kosmos",
)
VISION_TEMPLATE_MARKERS = (
    "<|vision_start|>",
    "image_pad",
    "pixel_values",
    "image_url",
    "vision_start",
    "<image>",
)


def _template_text(template: object) -> str:
    """Flatten a chat template to searchable lowercase text.

    Accepts the jinja string, the list-of-``{name, template}`` form some repos ship, or anything
    else (→ empty string, so callers get ``False`` rather than a crash)."""
    if isinstance(template, list):
        parts = (str(t.get("template", "")) for t in template if isinstance(t, dict))
        return " ".join(parts).lower()
    if isinstance(template, str):
        return template.lower()
    return ""


def template_implies_reasoning(template: object) -> bool:
    """True if a chat template contains a thinking marker. Best-effort; unknown shapes → False."""
    text = _template_text(template)
    return any(marker in text for marker in REASONING_MARKERS)


def template_implies_tools(template: object) -> bool:
    """True if a chat template renders tool/function-call scaffolding. Best-effort → False."""
    text = _template_text(template)
    return any(marker in text for marker in TOOL_MARKERS)


def detect_vision(
    *,
    architectures: Iterable[str] | None = None,
    model_type: str | None = None,
    config_keys: Iterable[str] | None = None,
    template: object = None,
) -> bool:
    """True if any vision signal is present: a structural config key, a VL architecture/model_type
    marker, or image handling in the chat template. Best-effort — any single signal suffices."""
    keys = set(config_keys or ())
    if any(k in keys for k in VISION_CONFIG_KEYS):
        return True
    arch_text = " ".join([*(architectures or []), model_type or ""]).lower()
    if any(marker in arch_text for marker in VISION_ARCH_MARKERS):
        return True
    return any(marker in _template_text(template) for marker in VISION_TEMPLATE_MARKERS)


def chat_template_from_config(config: Any) -> object | None:
    """Pull the chat template out of an HF ``expand=config`` object (a dict): the top-level
    ``chat_template_jinja`` if set, else ``tokenizer_config.chat_template`` (None when absent)."""
    if not isinstance(config, dict):
        return None
    if ct := config.get("chat_template_jinja"):
        return ct
    tok = config.get("tokenizer_config")
    if isinstance(tok, dict):
        return tok.get("chat_template")
    return None
