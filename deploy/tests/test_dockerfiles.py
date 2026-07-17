"""Dockerfile contract guards — the images must keep building as the repo evolves.

Everything here is static analysis of the Dockerfiles against the working tree; no daemon.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib

import pytest
from contract_utils import DOCKERFILES, REPO


def _lines(name: str) -> list[str]:
    """Logical lines of a Dockerfile (backslash continuations folded)."""
    raw = DOCKERFILES[name].read_text()
    return [ln.strip() for ln in re.sub(r"\\\s*\n", " ", raw).splitlines()]


def _copy_sources(name: str) -> list[str]:
    """Context-relative COPY sources (stage-to-stage COPY --from is excluded)."""
    sources: list[str] = []
    for ln in _lines(name):
        if not ln.startswith("COPY") or "--from=" in ln:
            continue
        parts = [p for p in ln.split()[1:] if not p.startswith("--")]
        sources.extend(parts[:-1])  # last token is the destination
    return sources


def _args(name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in _lines(name):
        m = re.match(r"ARG\s+([A-Z0-9_]+)=(.*)$", ln)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _from_images(name: str) -> list[str]:
    args = _args(name)
    images = []
    for ln in _lines(name):
        m = re.match(r"FROM\s+(\S+)", ln)
        if not m:
            continue
        image = m.group(1)
        for arg, value in args.items():
            image = image.replace(f"${{{arg}}}", value)
        images.append(image)
    return images


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_dockerfile_exists(name: str) -> None:
    assert DOCKERFILES[name].is_file()


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_copy_sources_exist_in_repo(name: str) -> None:
    """Every COPY source must exist relative to the build context (the repo root)."""
    missing = [src for src in _copy_sources(name) if not (REPO / src).exists()]
    assert not missing, f"{name}: COPY sources missing from the repo: {missing}"


@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_base_images_are_pinned(name: str) -> None:
    """No :latest / untagged / rolling-alias bases — a floating base is silent drift.
    Requiring a digit in the tag rejects the alias forms (:latest, :server, :stable)
    while accepting every real version pin (3.12-slim-trixie, 0.11, server-b9994…)."""
    stage_names = {
        m.group(1)
        for ln in _lines(name)
        if (m := re.search(r"\sAS\s+(\S+)$", ln)) and ln.startswith("FROM")
    }
    for image in _from_images(name):
        if image in stage_names:  # FROM <earlier stage>
            continue
        assert ":" in image, f"{name}: base image {image!r} has no tag"
        tag = image.rsplit(":", 1)[1]
        assert any(ch.isdigit() for ch in tag), (
            f"{name}: base image {image!r} uses a rolling alias tag — pin a versioned tag"
        )


PYTHON_SERVICES = ["control-plane", "inference-plane", "worker"]


@pytest.mark.parametrize("name", PYTHON_SERVICES)
def test_python_base_matches_repo_pin(name: str) -> None:
    pinned = (REPO / ".python-version").read_text().strip()
    base = _args(name)["PYTHON_BASE"]
    assert pinned in base, (
        f"{name}: PYTHON_BASE {base!r} does not match .python-version ({pinned}) — "
        "bump both together"
    )


def _workspace_member_manifests() -> list[str]:
    """Repo-relative pyproject paths of every PYTHON workspace member (root globs minus
    the root pyproject's excludes). A new member must be COPYd in every python Dockerfile
    or `uv sync --locked` fails inside the build."""
    root = tomllib.loads((REPO / "pyproject.toml").read_text())
    ws = root["tool"]["uv"]["workspace"]
    excluded = {e.rstrip("/") for e in ws.get("exclude", [])}
    members: list[str] = []
    for glob in ws["members"]:
        for pyproject in sorted(REPO.glob(f"{glob}/pyproject.toml")):
            rel = pyproject.parent.relative_to(REPO).as_posix()
            if rel not in excluded:
                members.append(f"{rel}/pyproject.toml")
    return members


@pytest.mark.parametrize("name", PYTHON_SERVICES)
def test_python_dockerfiles_copy_every_member_manifest(name: str) -> None:
    sources = set(_copy_sources(name))
    assert "uv.lock" in sources, f"{name}: uv.lock must be copied for a --locked sync"
    missing = [m for m in _workspace_member_manifests() if m not in sources]
    assert not missing, (
        f"{name}: workspace member manifests not copied (uv sync --locked will fail in "
        f"the image build): {missing}"
    )


@pytest.mark.parametrize("name", PYTHON_SERVICES)
def test_cmd_is_a_declared_console_script(name: str) -> None:
    """The image CMD must be a console script the package actually declares."""
    cmd_line = next(ln for ln in _lines(name) if ln.startswith("CMD"))
    script = re.findall(r'"([^"]+)"', cmd_line)[0]
    pkg_dir = {"control-plane": "apps/control-plane", "worker": "apps/worker"}.get(
        name, "apps/inference-plane"
    )
    pyproject = tomllib.loads((REPO / pkg_dir / "pyproject.toml").read_text())
    assert script in pyproject["project"]["scripts"], (
        f"{name}: CMD {script!r} is not in [project.scripts] of {pkg_dir}/pyproject.toml"
    )


def test_exposed_ports_match_app_defaults() -> None:
    """EXPOSE must track the ports the apps default to (and compose/k8s reuse)."""
    expected = {"control-plane": "8080", "inference-plane": "8081", "interface": "80"}
    for name, port in expected.items():
        exposes = [ln for ln in _lines(name) if ln.startswith("EXPOSE")]
        assert exposes and port in exposes[0].split(), (
            f"{name}: expected EXPOSE {port}, got {exposes}"
        )


def test_dockerignore_keeps_the_context_lean() -> None:
    """Unlike .gitignore, bare .dockerignore patterns match only at the context ROOT —
    nested state needs the **/ prefix or it leaks into COPYd trees (a host node_modules
    overlaying the image's install, __pycache__ in src layers, a stray nested .env baking
    VITE_* overrides into the SPA bundle)."""
    text = (REPO / ".dockerignore").read_text()
    for entry in (".git", ".run", "docs"):
        assert re.search(rf"^{re.escape(entry)}$", text, re.M), (
            f".dockerignore must exclude {entry!r}"
        )
    for entry in ("node_modules", "dist", "__pycache__", ".venv", ".env", ".env.*", "*.gguf"):
        assert re.search(rf"^\*\*/{re.escape(entry)}$", text, re.M), (
            f".dockerignore must exclude {entry!r} at EVERY depth (the **/ prefix) — "
            "bare patterns only match at the context root"
        )


def test_interface_dockerfile_copies_pnpm_workspace() -> None:
    sources = set(_copy_sources("interface"))
    for needed in ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"):
        assert needed in sources, f"interface: {needed} must be copied for pnpm install"
    assert "packages/ir-types" in sources or "packages/ir-types/package.json" in sources


@pytest.mark.parametrize("name", ["control-plane", "worker"])
def test_mcp_stdio_launchers_ship_in_the_images(name: str) -> None:
    """Hub-installed stdio MCP servers launch via npx (npm) / uvx (pypi); both processes
    spawn them locally, so both images must carry the launchers (ARG-gated) and point
    their caches at the writable /data volume — HOME is deliberately read-only."""
    text = "\n".join(_lines(name))
    assert "ARG WITH_MCP_LAUNCHERS=1" in text, f"{name}: launcher layer must be ARG-gated"
    assert re.search(r"FROM node:\S+ AS node-src", text), (
        f"{name}: node (npx) must be copied from the slim node image"
    )
    assert "npx-cli.js /usr/local/bin/npx" in text, f"{name}: npx shim must be linked"
    assert re.search(r"COPY --from=uv /uv /uvx /bin/", text), f"{name}: uvx must ship"
    # Stdio servers spawn with a SCRUBBED environment (only HOME/PATH/… pass through), so
    # launcher caches derive from HOME — it must sit on the writable /data volume.
    assert "HOME=/data/home" in text, f"{name}: HOME must live on the /data volume"


def test_alembic_tree_ships_in_the_control_plane_image() -> None:
    """Migrations run from the control-plane image (compose one-shot / k8s initContainer)."""
    sources = set(_copy_sources("control-plane"))
    assert "apps/control-plane/alembic.ini" in sources
    assert "apps/control-plane/alembic" in sources


def test_js_render_layer_is_arg_gated_in_the_control_plane_image() -> None:
    """render_js crawling needs chromium baked in (a runtime playwright install cannot work
    in a container) — behind an off-by-default ARG, browsers OUTSIDE /data so the volume
    never shadows them. The BrowserNotInstalled hint names this exact build arg."""
    text = "\n".join(_lines("control-plane"))
    assert "ARG WITH_JS_RENDER=0" in text
    assert "playwright install --with-deps chromium" in text
    assert "PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright" in text
    hint_source = REPO / "apps/control-plane/src/theygent_control_plane/rag/crawl.py"
    assert "WITH_JS_RENDER=1" in hint_source.read_text(), (
        "the container hint in rag/crawl.py must reference the build arg by name"
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
@pytest.mark.parametrize("name", sorted(DOCKERFILES))
def test_dockerfile_parses(name: str) -> None:
    """BuildKit syntax check only (--check builds nothing)."""
    proc = subprocess.run(
        ["docker", "build", "--check", "-f", str(DOCKERFILES[name]), "."],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
    assert proc.returncode == 0, f"{name}: docker build --check failed"
