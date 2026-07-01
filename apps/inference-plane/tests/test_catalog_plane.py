"""M16 §1.2 plane guard — discovery + install stay in the inference plane.

Models install HERE (the user's trust domain): the download runs in-process and the registration
writes to the inference-plane-local registry. So ``catalog`` and ``downloader`` must reach for NO
control-plane Postgres — no SQLAlchemy / asyncpg / control-plane import. A model install crossing
the shared DB would be a §4-class plane regression. Checked against real imports (AST), mirroring
``test_registry_persistence.py``'s guard, so a docstring mentioning "Postgres" never trips it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import theygent_inference_plane.catalog as catalog_mod
import theygent_inference_plane.downloader as downloader_mod

FORBIDDEN = {"sqlalchemy", "asyncpg", "psycopg", "theygent_control_plane", "theygent_worker"}


def _top_level_imports(module) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def test_catalog_imports_no_control_plane() -> None:
    bad = _top_level_imports(catalog_mod) & FORBIDDEN
    assert not bad, f"catalog must not import control-plane/db modules: {bad}"


def test_downloader_imports_no_control_plane() -> None:
    bad = _top_level_imports(downloader_mod) & FORBIDDEN
    assert not bad, f"downloader must not import control-plane/db modules: {bad}"
