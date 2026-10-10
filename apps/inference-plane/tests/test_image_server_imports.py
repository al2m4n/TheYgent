"""Plane guard — the bundled image wrapper starts without the plane's app.

``ImageServerLauncher`` spawns ``image_server`` on the plane's own interpreter as a module of this
package, so the package ``__init__`` runs in the wrapper process before the wrapper does. The
wrapper is stdlib-only so a cold image-engine spawn binds its port at once; if the wrapper or the
package root imports ``app`` → ``gateway`` → LiteLLM, every spawn pays seconds before its port is
up. The static check pins the wrapper's own imports to the stdlib (AST, like
``test_catalog_plane.py``); the runtime check spawns the launcher's own command, so a change to
the launch form or to the package root that drags LiteLLM back in fails here too.
"""

from __future__ import annotations

import ast
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from theygent_inference_plane import image_server
from theygent_inference_plane.launcher import ImageServerLauncher
from theygent_ir import ManagedBinding

_PACKAGE = "theygent_inference_plane"
_STARTUP_WAIT_SEC = 30.0


def _imported_top_level(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _healthy(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


def test_image_server_imports_only_the_stdlib() -> None:
    non_stdlib = _imported_top_level(Path(image_server.__file__)) - sys.stdlib_module_names
    assert not non_stdlib, f"image_server must stay stdlib-only, imports: {non_stdlib}"


def test_image_wrapper_starts_without_the_plane_app_or_litellm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Any existing file resolves as the generator CLI; it is never invoked (no render is requested).
    monkeypatch.setenv("THEYGENT_SDCPP_BIN", sys.executable)
    binding = ManagedBinding(
        binding="llamacpp", source="hf", model="m", modality="images.generation"
    )
    port = _free_port()
    cmd = ImageServerLauncher("sdcpp")._build_command(binding, port)
    assert cmd[0] == sys.executable, "the wrapper runs on the plane's own interpreter"

    # -X importtime reports every module the child imports on stderr, one line each as it loads.
    # A file (not a pipe) holds it, so a regression's thousands of lines can't block the child.
    log = tmp_path / "importtime.log"
    with log.open("wb") as err:
        proc = subprocess.Popen(
            [cmd[0], "-X", "importtime", *cmd[1:]], stdout=subprocess.DEVNULL, stderr=err
        )
    try:
        deadline = time.monotonic() + _STARTUP_WAIT_SEC
        while not _healthy(port):
            assert proc.poll() is None, f"wrapper exited early:\n{log.read_text()[-2000:]}"
            assert time.monotonic() < deadline, "wrapper never became healthy"
            time.sleep(0.02)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    loaded = {
        line.rsplit("|", 1)[-1].strip()
        for line in log.read_text().splitlines()
        if line.startswith("import time:")
    }
    assert _PACKAGE in loaded, "importtime output was not captured"
    plane = sorted(
        name
        for name in loaded
        if name.startswith(f"{_PACKAGE}.") and name != f"{_PACKAGE}.image_server"
    )
    litellm = [name for name in loaded if name.split(".")[0] == "litellm"]
    assert not plane and not litellm, (
        f"the image wrapper process imported plane modules {plane} "
        f"and {len(litellm)} litellm modules"
    )
