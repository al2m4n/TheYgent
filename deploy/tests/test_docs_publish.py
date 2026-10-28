"""Docs publish guards — the deploy-time repair of the early versions' canonical URLs."""

from __future__ import annotations

import gzip
import importlib.util
from pathlib import Path

import pytest
import yaml
from contract_utils import REPO

SCRIPT = REPO / ".github/scripts/docs_repair_canonicals.py"
REDIRECTS = REPO / "docs/user-docs/root/_redirects"
DOCS_WORKFLOW = REPO / ".github/workflows/docs.yml"

_spec = importlib.util.spec_from_file_location("docs_repair_canonicals", SCRIPT)
assert _spec and _spec.loader
repair_canonicals = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair_canonicals)

ORIGIN = "https://docs.theygent.ai/latest/"


def _page(canonical: str) -> str:
    return f'<html><head><link rel="canonical" href="{canonical}"></head></html>'


def _sitemap(*urls: str) -> str:
    return "<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>"


@pytest.fixture
def site(tmp_path: Path) -> Path:
    (tmp_path / "_redirects").write_text(
        "# comment\n/latest/v0.2.0/* /latest/:splat 301\n/latest/v1.0.1/* /latest/:splat 301\n"
    )
    for version in ("v0.2.0", "v1.0.1"):
        old = f"{ORIGIN}{version}/"
        (tmp_path / version / "chat").mkdir(parents=True)
        (tmp_path / version / "index.html").write_text(_page(old))
        (tmp_path / version / "chat/index.html").write_text(_page(f"{old}chat/"))
        sitemap = _sitemap(old, f"{old}chat/")
        (tmp_path / version / "sitemap.xml").write_text(sitemap)
        (tmp_path / version / "sitemap.xml.gz").write_bytes(gzip.compress(sitemap.encode()))
    (tmp_path / "v1.0.1/llms-full.txt").write_text(
        f"Browsable at {ORIGIN}v1.0.1\nSource: {ORIGIN}v1.0.1/chat/\nNot this: {ORIGIN}v1.0.10/\n"
    )
    (tmp_path / "v1.3.3/chat").mkdir(parents=True)
    (tmp_path / "v1.3.3/chat/index.html").write_text(_page(f"{ORIGIN}chat/"))
    return tmp_path


def test_repair_points_every_self_url_at_latest(site: Path) -> None:
    counts = repair_canonicals.repair(site)

    assert counts == {"v0.2.0": 6, "v1.0.1": 8}
    for version in ("v0.2.0", "v1.0.1"):
        assert (site / version / "index.html").read_text() == _page(ORIGIN)
        assert (site / version / "chat/index.html").read_text() == _page(f"{ORIGIN}chat/")
        sitemap = _sitemap(ORIGIN, f"{ORIGIN}chat/")
        assert (site / version / "sitemap.xml").read_text() == sitemap
        assert gzip.decompress((site / version / "sitemap.xml.gz").read_bytes()).decode() == sitemap
    assert (site / "v1.0.1/llms-full.txt").read_text() == (
        f"Browsable at {ORIGIN}\nSource: {ORIGIN}chat/\nNot this: {ORIGIN}v1.0.10/\n"
    )


def test_repair_leaves_unlisted_versions_alone_and_is_idempotent(site: Path) -> None:
    before = (site / "v1.3.3/chat/index.html").read_bytes()
    repair_canonicals.repair(site)

    assert (site / "v1.3.3/chat/index.html").read_bytes() == before
    assert repair_canonicals.repair(site) == {"v0.2.0": 0, "v1.0.1": 0}


def test_repair_fails_when_a_listed_version_is_missing(site: Path) -> None:
    (site / "_redirects").write_text("/latest/v9.9.9/* /latest/:splat 301\n")
    with pytest.raises(FileNotFoundError, match=r"v9\.9\.9"):
        repair_canonicals.repair(site)


def test_repair_fails_when_redirects_name_no_version(site: Path) -> None:
    (site / "_redirects").write_text("# no rules\n")
    with pytest.raises(ValueError, match="no /latest/<version>"):
        repair_canonicals.repair(site)


def test_shipped_redirects_name_the_early_versions() -> None:
    assert repair_canonicals.broken_versions(REDIRECTS.read_text()) == [
        "v0.1.0",
        "v0.1.1",
        "v0.2.0",
        "v0.2.1",
        "v1.0.0",
        "v1.0.1",
    ]


def test_publish_repairs_after_root_files_and_before_upload() -> None:
    steps = yaml.safe_load(DOCS_WORKFLOW.read_text())["jobs"]["publish"]["steps"]
    runs = [step.get("run", "") + str(step.get("with", {}).get("command", "")) for step in steps]

    def index_of(needle: str) -> int:
        return next(i for i, run in enumerate(runs) if needle in run)

    root_files = index_of('"origin/main:docs/user-docs/root"')
    repair = index_of("docs_repair_canonicals.py")
    upload = index_of("pages deploy _site")
    assert root_files < repair < upload
