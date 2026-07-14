"""The MCP registry browse/plan module, exercised over CAPTURED registry payloads.

Pure unit tests: the fixtures under ``fixtures/mcp_registry/`` are real responses from the two
public registries, so the mapping is asserted against what registries actually return, not a
hand-written idealization. The client runs against ``httpx.MockTransport`` serving the same
fixtures — no network, no database, no app.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from theygent_control_plane.mcp.registry import (
    CatalogDetail,
    InstallCandidate,
    McpRegistryClient,
    McpRegistryError,
    RegistryInfo,
    build_install,
    default_registries,
    entry_from_payload,
    install_candidates,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "mcp_registry"


@pytest.fixture(autouse=True)
def clean_db() -> Iterator[None]:
    # Overrides the conftest autouse fixture: this suite is pure unit tests over fixture
    # payloads — no store, no app — so the ephemeral Postgres container is never started.
    yield


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text())


def _candidates(fixture: str) -> list[InstallCandidate]:
    return install_candidates(_load(fixture)["server"])


class _Clock:
    """A hand-advanced monotonic clock for the TTL cache."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _client(handler: Any, *, registry_id: str = "official", **kwargs: Any) -> McpRegistryClient:
    return McpRegistryClient(
        registries=[RegistryInfo(id=registry_id, label="Test", url="https://registry.test")],
        http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


# ── mapping: list payloads → entries ──────────────────────────────────────────────────────────────


def test_official_list_page_maps_to_entries() -> None:
    payload = _load("official_list.json")
    entries = [entry_from_payload("official", item) for item in payload["servers"]]
    assert len(entries) == 5
    first = entries[0]
    assert first.registry == "official"
    assert first.name == "ac.inference.sh/mcp"
    assert first.title == "inference.sh"
    assert first.version == "1.0.1"
    assert first.status == "active"
    assert first.is_latest is True
    assert first.transports == ["http"]  # remotes-only entry: two streamable-http remotes
    assert first.package_types == []
    assert first.stars is None  # the official registry carries no publisher star metadata
    second = entries[1]
    assert second.repository_url == "https://github.com/frumu-ai/tandem"
    assert second.website_url == "https://tandem.ac/docs-mcp"
    assert second.updated_at == "2026-04-22T21:06:34.500049Z"


def test_github_list_page_maps_stars_from_server_meta() -> None:
    # This registry nests publisher-provided metadata under server._meta (the official one
    # would put it at the item's top level) — both locations must map.
    payload = _load("github_list.json")
    entries = [entry_from_payload("github", item) for item in payload["servers"]]
    by_name = {e.name: e for e in entries}
    markitdown = by_name["microsoft/markitdown"]
    assert markitdown.stars == 165683
    assert markitdown.package_types == ["pypi"]
    assert markitdown.transports == ["stdio"]
    assert all(e.stars is not None and e.stars > 0 for e in entries)


# ── mapping: server.json → install candidates ─────────────────────────────────────────────────────


def test_docker_secret_arg_is_rewritten_to_env() -> None:
    stdio = next(c for c in _candidates("github_mcp_server.json") if c.kind == "stdio")
    assert stdio.id == "pkg-0"
    assert stdio.command == "docker"
    assert stdio.label == "oci package (via docker)"
    # run -i --rm prepended; the secret -e value became a bare env NAME; literal args intact;
    # the image ref (already tagged) closes the argv.
    assert stdio.args == [
        "run",
        "-i",
        "--rm",
        "-p",
        "127.0.0.1:8085:8085",
        "-e",
        "GITHUB_OAUTH_CALLBACK_PORT=8085",
        "-e",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "ghcr.io/github/github-mcp-server:1.5.0",
    ]
    (token,) = stdio.inputs
    assert token.name == "GITHUB_PERSONAL_ACCESS_TOKEN"
    assert token.target == "env"
    assert token.secret is True
    assert token.required is False  # the entry marks the token optional
    assert stdio.env_templates == {"GITHUB_PERSONAL_ACCESS_TOKEN": "{GITHUB_PERSONAL_ACCESS_TOKEN}"}
    assert stdio.warnings == []  # the rewrite moved the secret to env — nothing to warn about


def test_remote_secret_header_without_template_becomes_header_input() -> None:
    remote = next(c for c in _candidates("github_mcp_server.json") if c.kind == "http")
    assert remote.id == "remote-0"
    assert remote.url == "https://api.githubcopilot.com/mcp/"
    assert remote.supports_oauth is True  # a streamable-http remote may be user-authorized
    # The header declares no value template — the user supplies the whole header value.
    (auth,) = remote.inputs
    assert (auth.name, auth.target, auth.secret) == ("Authorization", "header", True)
    assert remote.header_templates == {"Authorization": "{Authorization}"}
    assert remote.header_literals == {}


def test_npm_package_env_vars_become_inputs() -> None:
    (candidate,) = _candidates("npm_filesystem.json")
    assert candidate.kind == "stdio"
    assert candidate.command == "npx"  # from the entry's runtimeHint
    assert candidate.label == "npm package (via npx)"
    assert candidate.args == ["-y", "remote-filesystem-mcp-server@0.1.5"]
    by_name = {i.name: i for i in candidate.inputs}
    assert all(i.target == "env" for i in candidate.inputs)
    assert by_name["GCS_BUCKET"].required is True
    assert by_name["GCS_BUCKET"].secret is False
    assert by_name["GCS_PRIVATE_KEY"].secret is True
    assert by_name["GCS_PRIVATE_KEY"].required is False
    assert by_name["GCS_MAKE_PUBLIC"].default == "false"
    assert candidate.env_literals == {}  # every declared env var is user-supplied here


def test_pypi_package_ref_pins_the_version() -> None:
    (candidate,) = _candidates("pypi_postgres.json")
    assert candidate.command == "uvx"  # pypi default when no runtimeHint
    assert candidate.args == ["postgres-aiops==0.2.0"]
    assert candidate.inputs == []


def test_remote_only_server_with_bearer_template() -> None:
    (candidate,) = _candidates("remote_smithery.json")
    assert candidate.kind == "http"
    assert candidate.url == "https://server.smithery.ai/@smithery-ai/github/mcp"
    assert candidate.header_templates == {"Authorization": "Bearer {smithery_api_key}"}
    (key,) = candidate.inputs
    # The variable declares no metadata of its own — flags come from the enclosing header.
    assert (key.name, key.target) == ("smithery_api_key", "header")
    assert key.secret is True
    assert key.required is True
    entry = entry_from_payload("official", _load("remote_smithery.json"))
    assert entry.transports == ["http"]


def test_mcpb_only_server_yields_no_candidates() -> None:
    server = {
        "name": "x/bundle-only",
        "description": "",
        "version": "1.0.0",
        "packages": [
            {
                "registryType": "mcpb",
                "identifier": "https://example.test/server.mcpb",
                "transport": {"type": "stdio"},
            }
        ],
    }
    assert install_candidates(server) == []


def test_mcpb_skip_warning_lands_on_a_surviving_candidate() -> None:
    server = {
        "name": "x/mixed",
        "description": "",
        "version": "1.0.0",
        "packages": [
            {"registryType": "mcpb", "identifier": "bundle", "transport": {"type": "stdio"}},
            {
                "registryType": "npm",
                "identifier": "some-server",
                "version": "1.0.0",
                "transport": {"type": "stdio"},
            },
        ],
    }
    (candidate,) = install_candidates(server)
    assert candidate.args == ["some-server@1.0.0"]
    assert any("mcpb" in w for w in candidate.warnings)


def test_secret_variable_stuck_in_args_warns_but_still_maps() -> None:
    # Not a docker -e pair, so the secret cannot be moved to env: it stays an arg input with a
    # loud warning, never a hard failure.
    server = {
        "name": "x/arg-secret",
        "description": "",
        "version": "1.0.0",
        "packages": [
            {
                "registryType": "npm",
                "identifier": "arg-secret-server",
                "transport": {"type": "stdio"},
                "packageArguments": [
                    {
                        "type": "named",
                        "name": "--token",
                        "value": "{api_token}",
                        "variables": {"api_token": {"isSecret": True, "isRequired": True}},
                    }
                ],
            }
        ],
    }
    (candidate,) = install_candidates(server)
    (token,) = candidate.inputs
    assert (token.target, token.secret) == ("arg", True)
    assert any("launch args" in w for w in candidate.warnings)
    plan = build_install(candidate, {"api_token": "tok-1"})
    assert plan.config["args"] == ["arg-secret-server", "--token", "tok-1"]
    assert any("launch args" in w for w in plan.warnings)


# ── build_install ─────────────────────────────────────────────────────────────────────────────────


def test_build_install_stdio_secret_goes_to_env_and_secret_map() -> None:
    stdio = next(c for c in _candidates("github_mcp_server.json") if c.kind == "stdio")
    plan = build_install(stdio, {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_secret_value"})
    assert plan.secret_map == {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_secret_value"}
    assert plan.config["auth"] == {"type": "env"}
    assert plan.config["transport"] == "stdio"
    assert plan.config["command"] == "docker"
    assert "-e" in plan.config["args"] and "GITHUB_PERSONAL_ACCESS_TOKEN" in plan.config["args"]
    # The secret value itself never appears anywhere in the non-secret config.
    assert "ghp_secret_value" not in json.dumps(plan.config)


def test_build_install_optional_secret_omitted_when_unset() -> None:
    stdio = next(c for c in _candidates("github_mcp_server.json") if c.kind == "stdio")
    plan = build_install(stdio, {})
    assert plan.secret_map == {}
    assert "auth" not in plan.config
    assert plan.config["env"] == {}


def test_build_install_env_defaults_and_omissions() -> None:
    (candidate,) = _candidates("npm_filesystem.json")
    plan = build_install(candidate, {"GCS_BUCKET": "my-bucket", "GCS_PRIVATE_KEY": "pk-material"})
    assert plan.config["env"]["GCS_BUCKET"] == "my-bucket"
    assert plan.config["env"]["GCS_MAKE_PUBLIC"] == "false"  # declared default applies
    assert "GCS_PROJECT_ID" not in plan.config["env"]  # optional + unset ⇒ omitted, not empty
    assert plan.secret_map == {"GCS_PRIVATE_KEY": "pk-material"}
    assert plan.config["auth"] == {"type": "env"}
    assert "pk-material" not in json.dumps(plan.config)


def test_build_install_remote_renders_bearer_template_into_secret_map() -> None:
    (candidate,) = _candidates("remote_smithery.json")
    plan = build_install(candidate, {"smithery_api_key": "sk-123"})
    assert plan.config == {
        "transport": "http",
        "url": "https://server.smithery.ai/@smithery-ai/github/mcp",
        "headers": {},
        "auth": {"type": "headers"},
    }
    assert plan.secret_map == {"Authorization": "Bearer sk-123"}


def test_build_install_missing_required_input_is_named() -> None:
    (candidate,) = _candidates("remote_smithery.json")
    with pytest.raises(McpRegistryError, match="smithery_api_key"):
        build_install(candidate, {})
    (npm,) = _candidates("npm_filesystem.json")
    with pytest.raises(McpRegistryError, match="GCS_BUCKET"):
        build_install(npm, {"GCS_PRIVATE_KEY": "pk"})


# ── the client (mock transport, injected clock) ───────────────────────────────────────────────────


async def test_list_sends_version_latest_and_passes_cursor_through() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=_load("official_list.json"))

    client = _client(handler)
    page = await client.list_servers("official", search="mcp", limit=5, cursor="cur-1")
    (url,) = seen
    assert url.params["version"] == "latest"
    assert url.params["cursor"] == "cur-1"
    assert url.params["search"] == "mcp"
    assert url.params["limit"] == "5"
    assert page.next_cursor == "ai.1325/mcp:0.1.0"
    # The local substring fallback filter applied on top of the served page.
    assert all("mcp" in (e.name + (e.title or "") + e.description).lower() for e in page.entries)


async def test_unknown_registry_is_a_loud_error() -> None:
    client = _client(lambda request: httpx.Response(200, json={}))
    with pytest.raises(McpRegistryError, match="unknown registry"):
        await client.list_servers("nope")


async def test_upstream_failure_maps_to_registry_error() -> None:
    client = _client(lambda request: httpx.Response(503))
    with pytest.raises(McpRegistryError, match="503"):
        await client.list_servers("official")


async def test_list_is_ttl_cached_until_the_clock_passes_it() -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200, json=_load("official_list.json"))

    clock = _Clock()
    client = _client(handler, cache_ttl_s=300.0, clock=clock)
    await client.list_servers("official", limit=5)
    await client.list_servers("official", limit=5)
    assert len(hits) == 1  # second identical request served from cache
    clock.now = 300.5
    await client.list_servers("official", limit=5)
    assert len(hits) == 2  # TTL elapsed ⇒ refetched


async def test_github_search_is_filtered_locally_and_cached_long() -> None:
    hits: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url)
        return httpx.Response(200, json=_load("github_list.json"))

    clock = _Clock()
    client = _client(handler, registry_id="github", cache_ttl_s=1.0, clock=clock)
    page = await client.list_servers("github", search="markitdown", limit=5)
    # The term IS sent upstream (harmless there), and the served page — which ignored it — is
    # narrowed locally on name/title/description.
    assert hits[0].params["search"] == "markitdown"
    assert [e.name for e in page.entries] == ["microsoft/markitdown"]
    # The rate-limited registry keeps a floor on its TTL even when the configured TTL is tiny.
    clock.now = 2.0
    await client.list_servers("github", search="markitdown", limit=5)
    assert len(hits) == 1


async def test_get_server_urlencodes_the_name_and_maps_detail() -> None:
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path)
        return httpx.Response(200, json=_load("github_mcp_server.json"))

    client = _client(handler)
    detail = await client.get_server("official", "io.github.github/github-mcp-server")
    assert seen == [b"/v0.1/servers/io.github.github%2Fgithub-mcp-server/versions/latest"]
    assert isinstance(detail, CatalogDetail)
    assert detail.entry.name == "io.github.github/github-mcp-server"
    assert detail.entry.transports == ["stdio", "http"]
    assert {c.id for c in detail.candidates} == {"pkg-0", "remote-0"}


# ── registries from the environment ───────────────────────────────────────────────────────────────


def test_default_registries_extend_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "THEYGENT_MCP_REGISTRIES",
        '[{"id": "corp", "label": "Corp Mirror", "url": "https://mcp.corp.internal"}]',
    )
    registries = {r.id: r for r in default_registries()}
    assert set(registries) == {"official", "github", "corp"}
    assert registries["corp"].url == "https://mcp.corp.internal"


def test_malformed_env_registries_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THEYGENT_MCP_REGISTRIES", "{not json")
    with pytest.raises(McpRegistryError, match="THEYGENT_MCP_REGISTRIES"):
        default_registries()
