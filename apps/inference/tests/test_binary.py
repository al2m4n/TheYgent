"""Binary resolution — env -> PATH -> fail loudly (plan §"Binary resolution")."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from theygent_inference import binary
from theygent_inference.binary import LlamaServerNotFound, resolve_llama_server_binary
from theygent_inference.launcher import LlamaCppLauncher


def test_resolve_prefers_explicit_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exe = tmp_path / "llama-server"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv(binary.ENV_VAR, str(exe))
    assert resolve_llama_server_binary() == str(exe)


def test_resolve_env_pointing_at_nonexecutable_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "nope"
    monkeypatch.setenv(binary.ENV_VAR, str(missing))
    with pytest.raises(LlamaServerNotFound):
        resolve_llama_server_binary()


def test_resolve_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(binary.ENV_VAR, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(LlamaServerNotFound):
        resolve_llama_server_binary()


def test_launcher_not_ready_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(binary.ENV_VAR, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    launcher = LlamaCppLauncher()
    assert launcher.ready is False
    assert launcher.not_ready_reason is not None
    assert "llama-server not found" in launcher.not_ready_reason
