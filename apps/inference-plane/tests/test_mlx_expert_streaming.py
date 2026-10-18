"""MLX expert streaming: the launch settings, the launch command, and the server's import surface.

A mixture-of-experts chat model registered with ``"streamExperts": true`` is served by
``mlx_expert_stream`` — mlx_lm.server with its loader swapped so the routed experts stay on disk —
run in mlx-lm's own interpreter. Nothing here needs mlx: the server's exactness is checked by its
own self-test in ``test_integration_mlx_expert_streaming.py``.
"""

from __future__ import annotations

import ast
import os
import stat
import sys
from pathlib import Path

import pytest
from _fake_upstream import FakeUpstreamLauncher
from _payloads import managed_payload
from fastapi.testclient import TestClient
from theygent_inference_plane.binary import (
    MLX_PYTHON_ENV_VAR,
    EngineBinaryNotFound,
    resolve_mlx_python,
)
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
        model="org/moe-4bit",
        modality=modality,  # ty: ignore[invalid-argument-type]
        params=dict(params),
    )


def _executable(path: Path, text: str) -> str:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def mlx_server_script(tmp_path: Path) -> str:
    """An ``mlx_lm.server`` console script whose ``#!`` names an interpreter."""
    python = _executable(tmp_path / "python", "#!/bin/sh\n")
    return _executable(tmp_path / "mlx_lm.server", f"#!{python}\nimport mlx_lm.server\n")


# ── registration validates the settings ──────────────────────────────────────


@pytest.mark.parametrize(
    "binding",
    [
        _binding(streamExperts=True),
        _binding(streamExperts=True, expertCacheGb=8),
        _binding(streamExperts=True, expertCacheGb=2.5),
        _binding(streamExperts=False),
    ],
)
def test_valid_settings_pass(binding: ManagedBinding) -> None:
    assert launch_params_defect(binding) is None


@pytest.mark.parametrize(
    ("binding", "fragment"),
    [
        (_binding("llamacpp", streamExperts=True), "mlx chat bindings only"),
        (_binding(modality="vision", streamExperts=True), "mlx chat bindings only"),
        (_binding(streamExperts="yes"), "true or false"),
        (_binding(streamExperts=1), "true or false"),
        (_binding(streamExperts=True, expertCacheGb=0), "positive number"),
        (_binding(streamExperts=True, expertCacheGb=-4), "positive number"),
        (_binding(streamExperts=True, expertCacheGb=True), "positive number"),
        (_binding(streamExperts=True, expertCacheGb="8"), "positive number"),
        (_binding(streamExperts=True, expertCacheGb=float("inf")), "positive number"),
        (_binding(expertCacheGb=8), "set 'streamExperts': true"),
        (_binding(streamExperts=False, expertCacheGb=8), "set 'streamExperts': true"),
    ],
)
def test_invalid_settings_are_named(binding: ManagedBinding, fragment: str) -> None:
    defect = launch_params_defect(binding)
    assert defect is not None and fragment in defect, defect


def test_registration_refuses_invalid_settings(client: TestClient) -> None:
    payload = managed_payload(binding="mlx", source="hf", model="org/moe-4bit")
    payload["params"] = {"streamExperts": True, "expertCacheGb": 0}
    r = client.put("/admin/models/m", json=payload)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_binding"

    payload["params"] = {"streamExperts": True}
    payload["binding"] = "llamacpp"
    assert client.put("/admin/models/m", json=payload).status_code == 422


def test_settings_never_reach_a_request(client: TestClient, launcher: FakeUpstreamLauncher) -> None:
    payload = managed_payload(binding="mlx", source="hf", model="org/moe-4bit")
    payload["params"] = {"streamExperts": True, "expertCacheGb": 6, "temperature": 0.3}
    assert client.put("/admin/models/m", json=payload).status_code == 200
    r = client.post(
        "/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert r.status_code == 200, r.text
    body = launcher.handles[0].last_chat_body
    assert body is not None
    assert body["temperature"] == 0.3
    assert {"streamExperts", "expertCacheGb"} <= LAUNCH_PARAMS
    assert not {"streamExperts", "expertCacheGb"} & body.keys()


# ── the launch command ───────────────────────────────────────────────────────


def test_a_plain_mlx_model_still_runs_mlx_lm_server(mlx_server_script: str) -> None:
    cmd = MlxLauncher(binary_path=mlx_server_script)._build_command(_binding(), 9000)
    assert cmd == [
        mlx_server_script,
        "--model",
        "org/moe-4bit",
        "--host",
        "127.0.0.1",
        "--port",
        "9000",
    ]
    off = MlxLauncher(binary_path=mlx_server_script)._build_command(
        _binding(streamExperts=False), 9000
    )
    assert off[0] == mlx_server_script


def test_a_streamed_model_runs_the_streaming_server_in_mlx_lms_python(
    mlx_server_script: str, tmp_path: Path
) -> None:
    launcher = MlxLauncher(binary_path=mlx_server_script)
    cmd = launcher._build_command(_binding(streamExperts=True), 9000)
    assert cmd == [
        str(tmp_path / "python"),
        "-I",
        str(EXPERT_STREAM_SERVER),
        "--model",
        "org/moe-4bit",
        "--host",
        "127.0.0.1",
        "--port",
        "9000",
    ]
    sized = launcher._build_command(_binding(streamExperts=True, expertCacheGb=12), 9000)
    assert sized[-2:] == ["--expert-cache-gb", "12"]
    assert EXPERT_STREAM_SERVER.is_file()


def test_a_launch_refuses_settings_that_bypassed_registration(mlx_server_script: str) -> None:
    launcher = MlxLauncher(binary_path=mlx_server_script)
    with pytest.raises(RuntimeError, match="positive number"):
        launcher._build_command(_binding(streamExperts=True, expertCacheGb=-1), 9000)


# ── finding mlx-lm's interpreter ─────────────────────────────────────────────


def test_python_m_form_uses_its_own_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MLX_PYTHON_ENV_VAR, raising=False)
    assert resolve_mlx_python([sys.executable, "-m", "mlx_lm.server"]) == sys.executable


def test_a_script_uses_its_shebang_interpreter(
    monkeypatch: pytest.MonkeyPatch, mlx_server_script: str, tmp_path: Path
) -> None:
    monkeypatch.delenv(MLX_PYTHON_ENV_VAR, raising=False)
    assert resolve_mlx_python([mlx_server_script]) == str(tmp_path / "python")


def test_an_env_shebang_is_looked_up_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(MLX_PYTHON_ENV_VAR, raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = _executable(bin_dir / "python3", "#!/bin/sh\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    script = _executable(tmp_path / "mlx_lm.server", "#!/usr/bin/env -S python3\n")
    assert resolve_mlx_python([script]) == python


def test_the_override_wins_and_empty_means_unset(
    monkeypatch: pytest.MonkeyPatch, mlx_server_script: str, tmp_path: Path
) -> None:
    other = _executable(tmp_path / "other-python", "#!/bin/sh\n")
    monkeypatch.setenv(MLX_PYTHON_ENV_VAR, other)
    assert resolve_mlx_python([mlx_server_script]) == other
    monkeypatch.setenv(MLX_PYTHON_ENV_VAR, "")
    assert resolve_mlx_python([mlx_server_script]) == str(tmp_path / "python")
    monkeypatch.setenv(MLX_PYTHON_ENV_VAR, str(tmp_path / "missing"))
    with pytest.raises(EngineBinaryNotFound, match="not an executable"):
        resolve_mlx_python([mlx_server_script])


def test_a_launcher_without_a_shebang_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(MLX_PYTHON_ENV_VAR, raising=False)
    binary = tmp_path / "mlx_lm.server"
    binary.write_bytes(b"\xcf\xfa\xed\xfe native launcher")
    binary.chmod(0o755)
    with pytest.raises(EngineBinaryNotFound, match=MLX_PYTHON_ENV_VAR):
        resolve_mlx_python([str(binary)])


# ── the server runs outside the plane's environment ──────────────────────────


def test_the_server_imports_only_the_stdlib_and_mlx_lms_environment() -> None:
    """It is run by path in mlx-lm's interpreter, where this package is not installed: mlx,
    mlx-lm, and mlx-lm's own dependencies numpy and huggingface_hub are all it can rely on."""
    tree = ast.parse(EXPERT_STREAM_SERVER.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports need this package"
            if node.module:
                imported.add(node.module.split(".")[0])
    foreign = imported - sys.stdlib_module_names - {"mlx", "mlx_lm", "numpy", "huggingface_hub"}
    assert not foreign, f"mlx_expert_stream imports {foreign}"
