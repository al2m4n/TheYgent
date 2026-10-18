"""MLX ``promptCacheSize``: how many prompt states ``mlx_lm.server`` keeps for reuse.

Agents send long, mostly repeated prompts; mlx-lm reuses the cached state of an earlier prompt that
matches the new one's start. Its default of 10 cached states can be outgrown by a model serving
several agents or concurrent runs, so a registration may raise it. It is a launch setting: checked
at registration, turned into ``--prompt-cache-size`` for the plain and the expert-streaming server,
and never sent with a request.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from _fake_upstream import FakeUpstreamLauncher
from _payloads import managed_payload
from fastapi.testclient import TestClient
from theygent_inference_plane.launcher import (
    EXPERT_STREAM_SERVER,
    LAUNCH_PARAMS,
    MlxLauncher,
    launch_params_defect,
)
from theygent_ir import ManagedBinding


def _binding(engine: str = "mlx", modality: str = "chat", **params: object) -> ManagedBinding:
    return ManagedBinding(
        binding=engine,  # ty: ignore[invalid-argument-type]
        source="hf",
        model="org/model-4bit",
        modality=modality,  # ty: ignore[invalid-argument-type]
        params=dict(params),
    )


@pytest.fixture
def mlx_server_script(tmp_path: Path) -> str:
    """An ``mlx_lm.server`` console script whose ``#!`` names an interpreter."""
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n")
    script = tmp_path / "mlx_lm.server"
    script.write_text(f"#!{python}\nimport mlx_lm.server\n")
    for path in (python, script):
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(script)


def _flag(cmd: list[str]) -> str | None:
    return cmd[cmd.index("--prompt-cache-size") + 1] if "--prompt-cache-size" in cmd else None


# ── registration validates it ────────────────────────────────────────────────


def test_a_positive_integer_on_an_mlx_chat_model_is_accepted() -> None:
    assert launch_params_defect(_binding(promptCacheSize=32)) is None
    assert launch_params_defect(_binding(promptCacheSize=1, streamExperts=True)) is None


@pytest.mark.parametrize(
    ("binding", "fragment"),
    [
        (_binding(promptCacheSize=0), "positive integer"),
        (_binding(promptCacheSize=-4), "positive integer"),
        (_binding(promptCacheSize=2.5), "positive integer"),
        (_binding(promptCacheSize="32"), "positive integer"),
        (_binding(promptCacheSize=True), "positive integer"),
        (_binding("llamacpp", promptCacheSize=32), "mlx chat bindings only"),
        (_binding(modality="vision", promptCacheSize=32), "mlx chat bindings only"),
    ],
)
def test_other_values_and_engines_are_refused_by_name(
    binding: ManagedBinding, fragment: str
) -> None:
    defect = launch_params_defect(binding)
    assert defect is not None and fragment in defect, defect


def test_registration_refuses_an_invalid_size(client: TestClient) -> None:
    payload = managed_payload(binding="mlx", source="hf", model="org/model-4bit")
    payload["params"] = {"promptCacheSize": 0}
    r = client.put("/admin/models/m", json=payload)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_binding"


def test_it_never_reaches_a_request(client: TestClient, launcher: FakeUpstreamLauncher) -> None:
    payload = managed_payload(binding="mlx", source="hf", model="org/model-4bit")
    payload["params"] = {"promptCacheSize": 32, "temperature": 0.3}
    assert client.put("/admin/models/m", json=payload).status_code == 200
    r = client.post(
        "/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert r.status_code == 200, r.text
    body = launcher.handles[0].last_chat_body
    assert body is not None and body["temperature"] == 0.3
    assert "promptCacheSize" in LAUNCH_PARAMS
    assert "promptCacheSize" not in body


# ── the launch command ───────────────────────────────────────────────────────


def test_the_plain_server_gets_the_flag(mlx_server_script: str) -> None:
    launcher = MlxLauncher(binary_path=mlx_server_script)
    cmd = launcher._build_command(_binding(promptCacheSize=32), 9000)
    assert cmd[0] == mlx_server_script
    assert _flag(cmd) == "32"
    assert _flag(launcher._build_command(_binding(), 9000)) is None


def test_the_expert_streaming_server_passes_it_through(mlx_server_script: str) -> None:
    cmd = MlxLauncher(binary_path=mlx_server_script)._build_command(
        _binding(promptCacheSize=24, streamExperts=True, expertCacheGb=6), 9000
    )
    assert str(EXPERT_STREAM_SERVER) in cmd
    assert _flag(cmd) == "24"
    assert cmd[cmd.index("--expert-cache-gb") + 1] == "6"


def test_a_launch_refuses_a_size_that_bypassed_registration(mlx_server_script: str) -> None:
    with pytest.raises(RuntimeError, match="positive integer"):
        MlxLauncher(binary_path=mlx_server_script)._build_command(_binding(promptCacheSize=0), 9000)
