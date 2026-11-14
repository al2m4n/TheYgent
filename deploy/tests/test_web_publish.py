"""Marketing-site publish guards — content-versioned asset URLs in the deploy tree."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from contract_utils import REPO

SCRIPT = REPO / ".github/scripts/web_asset_versions.py"
ASSET_CHECK = REPO / ".github/scripts/check_web_assets.py"
WEB_WORKFLOW = REPO / ".github/workflows/web.yml"

_spec = importlib.util.spec_from_file_location("web_asset_versions", SCRIPT)
assert _spec and _spec.loader
asset_versions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(asset_versions)


def _v(path: Path) -> str:
    return "?v=" + hashlib.sha256(path.read_bytes()).hexdigest()[:10]


INDEX = """<html><head>
<link rel="stylesheet" href="/styles.css">
<link rel="icon" href="/favicon.ico">
<meta property="og:image" content="https://ertir.dev/assets/og.png">
<meta name="viewport" content="width=device-width">
</head><body>
<img src="assets/logo.svg" srcset="assets/logo.svg 1x, assets/logo@2x.png 2x">
<a href="/blog/">Blog</a> <a href="/llms.txt">llms</a> <a href="https://example.com/x.png">ext</a>
<a href="#top">top</a> <img src="/assets/missing.svg"> <img src="/assets/logo.svg?v=pinned">
<svg><use href="#mark" /></svg>
<div style="background: url('/assets/og.png')"></div>
</body></html>
"""


@pytest.fixture
def site(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "blog").mkdir()
    (tmp_path / "assets/logo.svg").write_text("<svg>E</svg>")
    (tmp_path / "assets/logo@2x.png").write_bytes(b"png-2x")
    (tmp_path / "assets/og.png").write_bytes(b"og-card")
    (tmp_path / "assets/font.woff2").write_bytes(b"font")
    (tmp_path / "favicon.ico").write_bytes(b"ico")
    (tmp_path / "llms.txt").write_text("# Ertir\n")
    (tmp_path / "styles.css").write_text(
        '@font-face { src: url("/assets/font.woff2") format("woff2"); }\n'
        ".hero { background: url(assets/og.png); }\n"
    )
    (tmp_path / "index.html").write_text(INDEX)
    (tmp_path / "blog/index.html").write_text('<img src="../assets/logo.svg">')
    return tmp_path


def test_every_local_static_reference_gets_its_content_hash(site: Path) -> None:
    count = asset_versions.stamp(site)

    css = (site / "styles.css").read_text()
    assert f'url("/assets/font.woff2{_v(site / "assets/font.woff2")}")' in css
    assert f"url(assets/og.png{_v(site / 'assets/og.png')})" in css

    page = (site / "index.html").read_text()
    # The page names the stylesheet as uploaded: hashed after its own url()s were stamped.
    assert f'href="/styles.css{_v(site / "styles.css")}"' in page
    assert f'href="/favicon.ico{_v(site / "favicon.ico")}"' in page
    assert f'content="https://ertir.dev/assets/og.png{_v(site / "assets/og.png")}"' in page
    logo, logo2x = _v(site / "assets/logo.svg"), _v(site / "assets/logo@2x.png")
    srcset = f"assets/logo.svg{logo} 1x, assets/logo@2x.png{logo2x} 2x"
    assert f'src="assets/logo.svg{logo}" srcset="{srcset}"' in page
    assert f"url('/assets/og.png{_v(site / 'assets/og.png')}')" in page
    assert (site / "blog/index.html").read_text() == f'<img src="../assets/logo.svg{logo}">'
    assert count == 10


def test_pages_anchors_external_and_pinned_references_are_left_alone(site: Path) -> None:
    asset_versions.stamp(site)

    page = (site / "index.html").read_text()
    for untouched in (
        'content="width=device-width"',
        'href="/blog/"',
        'href="/llms.txt"',
        'href="https://example.com/x.png"',
        'href="#top"',
        'src="/assets/missing.svg"',  # the asset check, not this, fails a missing file
        'src="/assets/logo.svg?v=pinned"',
        'href="#mark"',
    ):
        assert untouched in page


def test_stamping_twice_changes_nothing(site: Path) -> None:
    asset_versions.stamp(site)
    before = {p: p.read_bytes() for p in site.rglob("*") if p.is_file()}

    assert asset_versions.stamp(site) == 0
    assert {p: p.read_bytes() for p in site.rglob("*") if p.is_file()} == before


def test_a_changed_file_gets_a_new_url(
    site: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    other = tmp_path_factory.mktemp("other") / "site"
    shutil.copytree(site, other)
    (other / "assets/logo.svg").write_text("<svg>new E</svg>")

    asset_versions.stamp(site)
    asset_versions.stamp(other)

    old_page, new_page = (
        (site / "blog/index.html").read_text(),
        (other / "blog/index.html").read_text(),
    )
    assert old_page != new_page
    assert (site / "styles.css").read_text() == (other / "styles.css").read_text()


def test_the_stamped_site_still_resolves_every_reference(tmp_path: Path) -> None:
    site = tmp_path / "web"
    shutil.copytree(REPO / "apps/web", site)

    assert asset_versions.stamp(site) > 0
    home = (site / "index.html").read_text()
    for asset in (
        "/styles.css",
        "/assets/logo/theygent-lockup.svg",
        "https://ertir.dev/assets/og-image.png",
    ):
        assert f"{asset}?v=" in home, asset
    result = subprocess.run(
        [sys.executable, str(ASSET_CHECK), str(site)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def _steps(job: str) -> list[str]:
    workflow = yaml.safe_load(WEB_WORKFLOW.read_text())
    return [step.get("run", "") or step.get("name", "") for step in workflow["jobs"][job]["steps"]]


def _index(steps: list[str], needle: str) -> int:
    hits = [i for i, s in enumerate(steps) if needle in s]
    assert hits, f"no step mentions {needle!r}"
    return hits[0]


def test_production_deploys_the_versioned_tree() -> None:
    steps = _steps("publish")
    assert _index(steps, "web_asset_versions.py") < _index(steps, "Deploy to Cloudflare Pages")


def test_previews_deploy_the_versioned_tree_and_check_it() -> None:
    steps = _steps("validate")
    stamp = _index(steps, "web_asset_versions.py")
    assert stamp < _index(steps, "Deploy preview")
    assert any("check_web_assets.py" in s for s in steps[stamp + 1 :]), (
        "the stamped tree is not re-checked"
    )
