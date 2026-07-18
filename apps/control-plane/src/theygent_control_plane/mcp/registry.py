"""Browsing MCP server registries + turning a chosen entry into an installable definition.

A *registry* (a hub of published MCP servers — the official community registry, GitHub's, or a
self-hosted mirror) speaks one REST shape: a paged ``/v0.1/servers`` listing and a per-name
``/versions/{version}`` detail, each item a ``server.json`` document plus registry metadata under
``_meta``. This module is the whole read path: ``McpRegistryClient`` fetches and TTL-caches those
payloads; the pure mapping functions normalize a payload into ``CatalogEntry`` (the browse row)
and ``InstallCandidate`` (one launchable form of the server — a package spawned over stdio, or a
remote reached over streamable-HTTP/SSE, with the inputs the user must fill); and once the inputs
are filled, ``build_install`` produces the ``mcp_server`` connection config plus a separate
``secret_map``.

**No side effects.** This module lists, normalizes, and plans — it never touches the database or
the MCP manager; the route layer owns installs (the same discipline as the model catalog: a plan
is returned, never applied). The secret rule mirrors the connection seam: a value flagged secret
NEVER lands in the returned ``config``. Stdio secrets are rewritten into environment variables —
in particular a docker ``-e VAR={secret}`` launch argument becomes ``-e VAR`` plus a secret env
input, because docker inherits the value from the spawned process env, so the token never rides
argv — and remote secret headers render into ``secret_map``; the caller stores those encrypted
and the config only records that auth exists.

Extra self-hosted registries come from ``THEYGENT_MCP_REGISTRIES`` (a JSON array of
``{"id","label","url"}``) — the air-gapped / allowlist deployment story: point the browse surface
at a mirror you control instead of the public hubs."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

#: The two registry metadata namespaces an item carries under ``_meta``.
_OFFICIAL_META = "io.modelcontextprotocol.registry/official"
_PUBLISHER_META = "io.modelcontextprotocol.registry/publisher-provided"

#: Default launch command per package registry type when the entry names no ``runtimeHint``.
_COMMAND_FOR_REGISTRY_TYPE: dict[str, str] = {
    "npm": "npx",
    "pypi": "uvx",
    "oci": "docker",
    "nuget": "dnx",
}

#: A ``{variable}`` placeholder inside an argument / env / header / url value.
_TEMPLATE_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.-]*)\}")
#: A docker ``-e`` value of the exact ``VAR={variable}`` shape (the secret-to-env rewrite).
_DOCKER_ENV_ARG_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=\{([A-Za-z_][A-Za-z0-9_.-]*)\}")

_REGISTRIES_ENV = "THEYGENT_MCP_REGISTRIES"

#: Remote wire type → the candidate/transport kind it maps to.
_REMOTE_KINDS: dict[str, Literal["http", "sse"]] = {"streamable-http": "http", "sse": "sse"}


class McpRegistryError(RuntimeError):
    """A registry could not be reached / returned an unusable payload (the route layer maps this
    to a 502), or an install plan was requested without its required inputs."""


class _Wire(BaseModel):
    """camelCase over the wire, snake_case in code."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RegistryInfo(_Wire):
    """One browsable registry: the built-ins plus any self-hosted mirror from the env."""

    id: str
    label: str
    url: str


class CatalogEntry(_Wire):
    """A normalized browse row — one server at one (latest, unless pinned) version."""

    registry: str
    name: str
    title: str | None = None
    description: str = ""
    version: str
    status: str = "active"
    is_latest: bool = True
    updated_at: str | None = None
    repository_url: str | None = None
    website_url: str | None = None
    stars: int | None = None
    #: Derived: "stdio" when any package launches locally; "http"/"sse" per remote.
    transports: list[str] = Field(default_factory=list)
    package_types: list[str] = Field(default_factory=list)
    deprecation_message: str | None = None


class CandidateInput(_Wire):
    """One value the user must (or may) supply before a candidate can be installed. ``target``
    says where the value lands: an env var, a launch argument, a request header, or the url."""

    name: str
    description: str | None = None
    required: bool = False
    secret: bool = False
    default: str | None = None
    choices: list[str] | None = None
    placeholder: str | None = None
    target: Literal["env", "arg", "header", "url"]


class InstallCandidate(_Wire):
    """One launchable form of a server, plus the raw launch shape it was derived from — enough
    that ``build_install`` works from the candidate alone. ``args`` / ``env_templates`` /
    ``header_templates`` / ``url`` may still carry ``{variable}`` placeholders; the literals
    dicts hold values that need no user input."""

    id: str
    kind: Literal["stdio", "http", "sse"]
    label: str
    inputs: list[CandidateInput] = Field(default_factory=list)
    #: A remote over streamable-HTTP may be authorized interactively (a user token flow)
    #: instead of static headers.
    supports_oauth: bool = False
    warnings: list[str] = Field(default_factory=list)
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env_literals: dict[str, str] = Field(default_factory=dict)
    env_templates: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    header_literals: dict[str, str] = Field(default_factory=dict)
    header_templates: dict[str, str] = Field(default_factory=dict)


class CatalogPage(_Wire):
    """One page of a registry listing; ``next_cursor`` is the registry's opaque continuation."""

    entries: list[CatalogEntry] = Field(default_factory=list)
    next_cursor: str | None = None


class CatalogDetail(_Wire):
    """One server resolved to a version, with every installable form derived from it."""

    entry: CatalogEntry
    candidates: list[InstallCandidate] = Field(default_factory=list)


@dataclass
class InstallPlan:
    """What an install would create — the non-secret connection ``config`` and, separately, the
    secret material (``secret_map``) the caller must store encrypted. Never applied here; the
    route layer owns the install. The caller stamps ``config["origin"]`` itself."""

    config: dict[str, Any]
    secret_map: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ── registries (built-ins + the env allowlist) ────────────────────────────────────────────────────


def builtin_registries() -> list[RegistryInfo]:
    """The two public registries every install carries. Extra self-hosted ones come from the
    env allowlist (:func:`default_registries`) or the platform settings surface."""
    return [
        RegistryInfo(
            id="official",
            label="Official MCP Registry",
            url="https://registry.modelcontextprotocol.io",
        ),
        RegistryInfo(id="github", label="GitHub MCP Registry", url="https://api.mcp.github.com"),
    ]


def default_registries() -> list[RegistryInfo]:
    """The two public registries plus any self-hosted ones from ``THEYGENT_MCP_REGISTRIES``.
    A malformed env value raises loudly — silently dropping a registry the user configured
    would defeat the allowlist it exists for."""
    registries = builtin_registries()
    raw = os.environ.get(_REGISTRIES_ENV, "").strip()
    if not raw:
        return registries
    try:
        extra = json.loads(raw)
        for item in extra:
            registries.append(RegistryInfo(id=item["id"], label=item["label"], url=item["url"]))
    except Exception as exc:
        raise McpRegistryError(
            f"invalid {_REGISTRIES_ENV}: expected a JSON array of "
            f'{{"id","label","url"}} objects ({exc})'
        ) from exc
    return registries


# ── pure mapping: payload → CatalogEntry ──────────────────────────────────────────────────────────


def _stars(payload: dict[str, Any], server: dict[str, Any]) -> int | None:
    """Publisher-provided star count. Registries disagree on where it lives — under the item's
    top-level ``_meta`` or under ``server._meta`` — so both are checked."""
    for meta in (payload.get("_meta") or {}, server.get("_meta") or {}):
        github = (meta.get(_PUBLISHER_META) or {}).get("github") or {}
        stars = github.get("stargazerCount")
        if isinstance(stars, int):
            return stars
    return None


def _transports(server: dict[str, Any]) -> list[str]:
    transports: list[str] = []
    for pkg in server.get("packages") or []:
        transport = str((pkg.get("transport") or {}).get("type") or "stdio")
        if transport == "stdio" and "stdio" not in transports:
            transports.append("stdio")
    for remote in server.get("remotes") or []:
        kind = _REMOTE_KINDS.get(str(remote.get("type") or ""))
        if kind and kind not in transports:
            transports.append(kind)
    return transports


def _package_types(server: dict[str, Any]) -> list[str]:
    types: list[str] = []
    for pkg in server.get("packages") or []:
        registry_type = str(pkg.get("registryType") or "")
        if registry_type and registry_type not in types:
            types.append(registry_type)
    return types


def entry_from_payload(registry: str, payload: dict[str, Any]) -> CatalogEntry:
    """Normalize one registry item (``{"server": ..., "_meta": ...}``) into a browse row."""
    server = payload.get("server") or {}
    official = (payload.get("_meta") or {}).get(_OFFICIAL_META) or {}
    repository = server.get("repository") or {}
    return CatalogEntry(
        registry=registry,
        name=str(server.get("name") or ""),
        title=server.get("title"),
        description=str(server.get("description") or ""),
        version=str(server.get("version") or ""),
        status=str(official.get("status") or "active"),
        is_latest=bool(official.get("isLatest", True)),
        updated_at=official.get("updatedAt"),
        repository_url=repository.get("url"),
        website_url=server.get("websiteUrl"),
        stars=_stars(payload, server),
        transports=_transports(server),
        package_types=_package_types(server),
        deprecation_message=official.get("statusMessage"),
    )


# ── pure mapping: server.json → InstallCandidate ──────────────────────────────────────────────────


def _make_input(
    name: str,
    *,
    target: Literal["env", "arg", "header", "url"],
    meta: dict[str, Any],
    enclosing: dict[str, Any],
    force_secret: bool = False,
) -> CandidateInput:
    """An input from a declared variable (``meta``) merged with its enclosing argument / env var /
    header (``enclosing``) — the enclosing flags apply when the variable declares none of its own
    (many entries flag ``isSecret``/``isRequired`` on the header, not the variable)."""
    default = meta.get("default")
    if default is None:
        default = enclosing.get("default")
    choices = meta.get("choices") or enclosing.get("choices")
    return CandidateInput(
        name=name,
        description=meta.get("description") or enclosing.get("description"),
        required=bool(meta.get("isRequired") or enclosing.get("isRequired")),
        secret=force_secret or bool(meta.get("isSecret") or enclosing.get("isSecret")),
        default=str(default) if default is not None else None,
        choices=[str(c) for c in choices] if choices else None,
        placeholder=meta.get("placeholder") or enclosing.get("placeholder"),
        target=target,
    )


class _CandidateBuilder:
    """Accumulates the launch shape of one candidate. Inputs dedupe by name (first wins — the
    same variable may appear in several templates but is supplied once)."""

    def __init__(self, *, docker: bool = False) -> None:
        self.docker = docker
        self.args: list[str] = []
        self.inputs: dict[str, CandidateInput] = {}
        self.env_literals: dict[str, str] = {}
        self.env_templates: dict[str, str] = {}
        self.warnings: list[str] = []

    def add_input(self, candidate_input: CandidateInput) -> None:
        self.inputs.setdefault(candidate_input.name, candidate_input)

    def add_template_inputs(
        self,
        template: str,
        *,
        target: Literal["env", "arg", "header", "url"],
        enclosing: dict[str, Any],
    ) -> None:
        variables = enclosing.get("variables") or {}
        for var in dict.fromkeys(_TEMPLATE_VAR_RE.findall(template)):
            self.add_input(
                _make_input(var, target=target, meta=variables.get(var) or {}, enclosing=enclosing)
            )

    def add_argument(self, argument: dict[str, Any]) -> None:
        value = argument.get("value")
        if argument.get("type") == "named":
            name = argument.get("name")
            if not name:
                return
            if value is not None and self.docker and name in ("-e", "--env"):
                match = _DOCKER_ENV_ARG_RE.fullmatch(str(value))
                if match and self._rewrite_docker_env(name, match, argument):
                    return
            self.args.append(str(name))
            if value is not None:
                self._add_value(str(value), argument)
            return
        # positional
        if value is not None:
            self._add_value(str(value), argument)
            return
        # No value ⇒ the user supplies it; the hint names the input.
        ident = argument.get("valueHint") or argument.get("name")
        if not ident:
            return
        template = "{" + str(ident) + "}"
        self.args.append(template)
        self.add_template_inputs(template, target="arg", enclosing=argument)
        self._warn_secret_args(template)

    def _rewrite_docker_env(
        self, name: str, match: re.Match[str], argument: dict[str, Any]
    ) -> bool:
        """The docker secret rewrite: ``-e VAR={secret}`` → ``-e VAR`` + a secret env input for
        ``VAR``. Docker inherits the value from the spawned process env, so the secret lands in
        env, never argv. Applies only when the variable is flagged secret."""
        env_name, var = match.group(1), match.group(2)
        meta = (argument.get("variables") or {}).get(var) or {}
        if not (meta.get("isSecret") or argument.get("isSecret")):
            return False
        self.args += [name, env_name]
        self.env_templates[env_name] = "{" + env_name + "}"
        self.add_input(
            _make_input(env_name, target="env", meta=meta, enclosing=argument, force_secret=True)
        )
        return True

    def _add_value(self, value: str, argument: dict[str, Any]) -> None:
        self.args.append(value)
        if _TEMPLATE_VAR_RE.search(value):
            self.add_template_inputs(value, target="arg", enclosing=argument)
            self._warn_secret_args(value)

    def _warn_secret_args(self, template: str) -> None:
        # A secret that cannot be moved to env stays a launch argument — allowed, but the user
        # should know the value will be stored in the launch args, not the secret store.
        for var in _TEMPLATE_VAR_RE.findall(template):
            candidate_input = self.inputs.get(var)
            if candidate_input and candidate_input.secret and candidate_input.target == "arg":
                self.warnings.append(
                    f"secret input {var!r} rides a launch argument — "
                    "its value will be stored in the launch args"
                )

    def add_key_value(
        self,
        entry: dict[str, Any],
        *,
        target: Literal["env", "header"],
        literals: dict[str, str],
        templates: dict[str, str],
    ) -> None:
        """An env var or header. A literal, non-secret value needs no input; a templated value
        yields inputs per variable; no value at all means the user supplies the whole thing —
        modeled as a template whose sole variable is the entry's own name."""
        name = entry.get("name")
        if not name:
            return
        value = entry.get("value")
        has_vars = value is not None and bool(_TEMPLATE_VAR_RE.search(str(value)))
        if value is not None and not has_vars and not entry.get("isSecret"):
            literals[str(name)] = str(value)
            return
        if has_vars:
            templates[str(name)] = str(value)
            self.add_template_inputs(str(value), target=target, enclosing=entry)
            return
        templates[str(name)] = "{" + str(name) + "}"
        self.add_input(_make_input(str(name), target=target, meta={}, enclosing=entry))


def _package_ref(registry_type: str, pkg: dict[str, Any]) -> str:
    """The argv token that names the package to the launch command. An oci identifier is already
    a full image ref (tag included), so it passes through untouched."""
    identifier = str(pkg.get("identifier") or "")
    version = pkg.get("version")
    if registry_type == "oci" or not version:
        return identifier
    if registry_type in ("npm", "nuget"):
        return f"{identifier}@{version}"
    if registry_type == "pypi":
        # An explicit --from runtime argument already names the distribution; don't pin twice.
        if any(a.get("name") == "--from" for a in pkg.get("runtimeArguments") or []):
            return identifier
        return f"{identifier}=={version}"
    return identifier


def _package_candidate(
    candidate_id: str, pkg: dict[str, Any]
) -> tuple[InstallCandidate | None, list[str]]:
    registry_type = str(pkg.get("registryType") or "")
    identifier = str(pkg.get("identifier") or "")
    transport = str((pkg.get("transport") or {}).get("type") or "stdio")
    if transport != "stdio":
        return None, [f"package {identifier!r} skipped: transport {transport!r} is not launchable"]
    if registry_type == "mcpb":
        return None, [f"package {identifier!r} skipped: mcpb bundles have no launch command"]
    command = pkg.get("runtimeHint") or _COMMAND_FOR_REGISTRY_TYPE.get(registry_type)
    if not command:
        return None, [
            f"package {identifier!r} skipped: no launch command for registry type {registry_type!r}"
        ]

    builder = _CandidateBuilder(docker=command == "docker")
    runtime_arguments = [a for a in pkg.get("runtimeArguments") or [] if isinstance(a, dict)]
    # A container must be spawned attached and disposable; entries usually declare only the
    # run *options*, so the run invocation itself is supplied unless the entry already starts one.
    starts_run = bool(
        runtime_arguments
        and runtime_arguments[0].get("type") == "positional"
        and runtime_arguments[0].get("value") == "run"
    )
    if builder.docker and not starts_run:
        builder.args += ["run", "-i", "--rm"]
    for argument in runtime_arguments:
        builder.add_argument(argument)
    builder.args.append(_package_ref(registry_type, pkg))
    for argument in pkg.get("packageArguments") or []:
        if isinstance(argument, dict):
            builder.add_argument(argument)
    for env_var in pkg.get("environmentVariables") or []:
        if isinstance(env_var, dict):
            builder.add_key_value(
                env_var,
                target="env",
                literals=builder.env_literals,
                templates=builder.env_templates,
            )
    return InstallCandidate(
        id=candidate_id,
        kind="stdio",
        label=f"{registry_type} package (via {command})",
        inputs=list(builder.inputs.values()),
        supports_oauth=False,
        warnings=builder.warnings,
        command=str(command),
        args=builder.args,
        env_literals=builder.env_literals,
        env_templates=builder.env_templates,
    ), []


def _remote_candidate(
    candidate_id: str, remote: dict[str, Any]
) -> tuple[InstallCandidate | None, list[str]]:
    remote_type = str(remote.get("type") or "")
    kind = _REMOTE_KINDS.get(remote_type)
    url = remote.get("url")
    if kind is None or not url:
        return None, [f"remote skipped: unsupported type {remote_type!r}"]
    builder = _CandidateBuilder()
    header_literals: dict[str, str] = {}
    header_templates: dict[str, str] = {}
    if _TEMPLATE_VAR_RE.search(str(url)):
        builder.add_template_inputs(str(url), target="url", enclosing=remote)
    for header in remote.get("headers") or []:
        if isinstance(header, dict):
            builder.add_key_value(
                header, target="header", literals=header_literals, templates=header_templates
            )
    return InstallCandidate(
        id=candidate_id,
        kind=kind,
        label=f"Remote server ({remote_type})",
        inputs=list(builder.inputs.values()),
        # A streamable-HTTP remote may be authorized interactively instead of static headers.
        supports_oauth=kind == "http",
        warnings=builder.warnings,
        url=str(url),
        header_literals=header_literals,
        header_templates=header_templates,
    ), []


def install_candidates(server: dict[str, Any]) -> list[InstallCandidate]:
    """Every launchable form of a ``server.json``: packages become stdio candidates, remotes
    become http/sse candidates. Ids are positional (``pkg-<i>`` / ``remote-<i>``) so a candidate
    can be re-derived from the same entry later. Skips (mcpb bundles, unlaunchable transports)
    surface as warnings on the remaining candidates; a server with no launchable form yields an
    empty list."""
    candidates: list[InstallCandidate] = []
    skipped: list[str] = []
    for i, pkg in enumerate(server.get("packages") or []):
        if not isinstance(pkg, dict):
            continue
        candidate, warnings = _package_candidate(f"pkg-{i}", pkg)
        if candidate is not None:
            candidates.append(candidate)
        skipped.extend(warnings)
    for i, remote in enumerate(server.get("remotes") or []):
        if not isinstance(remote, dict):
            continue
        candidate, warnings = _remote_candidate(f"remote-{i}", remote)
        if candidate is not None:
            candidates.append(candidate)
        skipped.extend(warnings)
    if skipped and candidates:
        candidates[0].warnings.extend(skipped)
    return candidates


# ── pure planning: candidate + user values → InstallPlan ──────────────────────────────────────────


def build_install(candidate: InstallCandidate, values: dict[str, str]) -> InstallPlan:
    """Fill a candidate's templates from user-supplied ``values`` and produce the connection
    config. Secret-flagged values never land in ``config``: stdio secrets go to
    ``secret_map[VAR]`` with ``auth: {type: env}``; secret headers go to
    ``secret_map[Header]`` (rendered) with ``auth: {type: headers}``."""
    inputs = {i.name: i for i in candidate.inputs}
    missing = [
        name
        for name, i in inputs.items()
        if i.required and not values.get(name) and i.default is None
    ]
    if missing:
        raise McpRegistryError(f"missing required input(s): {', '.join(sorted(missing))}")

    def resolve(name: str) -> str | None:
        value = values.get(name)
        if value:
            return value
        candidate_input = inputs.get(name)
        if candidate_input is not None and candidate_input.default is not None:
            return candidate_input.default
        return None

    def render(template: str) -> tuple[str, bool, bool]:
        """Substitute ``{var}`` placeholders → (rendered, any-var-secret, all-vars-resolved)."""
        secret = False
        resolved = True

        def substitute(match: re.Match[str]) -> str:
            nonlocal secret, resolved
            var = match.group(1)
            candidate_input = inputs.get(var)
            if candidate_input is not None and candidate_input.secret:
                secret = True
            value = resolve(var)
            if value is None:
                resolved = False
                return ""
            return value

        return _TEMPLATE_VAR_RE.sub(substitute, template), secret, resolved

    warnings = list(candidate.warnings)
    secret_map: dict[str, str] = {}
    config: dict[str, Any]

    if candidate.kind == "stdio":
        args: list[str] = []
        for arg in candidate.args:
            if not _TEMPLATE_VAR_RE.search(arg):
                args.append(arg)
                continue
            rendered, _, resolved = render(arg)
            # An optional, unsupplied pure-input argument simply isn't passed.
            if not resolved and _TEMPLATE_VAR_RE.fullmatch(arg):
                continue
            args.append(rendered)
        env = dict(candidate.env_literals)
        for name, template in candidate.env_templates.items():
            rendered, secret, resolved = render(template)
            if not resolved:
                continue  # an optional env var the user left unset is omitted, not set empty
            if secret:
                secret_map[name] = rendered
            else:
                env[name] = rendered
        config = {"transport": "stdio", "command": candidate.command, "args": args, "env": env}
        if secret_map:
            config["auth"] = {"type": "env"}
        return InstallPlan(config=config, secret_map=secret_map, warnings=warnings)

    url, _, _ = render(candidate.url or "")
    headers = dict(candidate.header_literals)
    for name, template in candidate.header_templates.items():
        rendered, secret, resolved = render(template)
        if not resolved:
            continue
        if secret:
            secret_map[name] = rendered
        else:
            headers[name] = rendered
    config = {"transport": candidate.kind, "url": url, "headers": headers}
    if secret_map:
        config["auth"] = {"type": "headers"}
    return InstallPlan(config=config, secret_map=secret_map, warnings=warnings)


# ── the client (fetch + TTL cache) ────────────────────────────────────────────────────────────────


class McpRegistryClient:
    """Read-only client over one or more registries. The http factory and clock are injectable
    so the fast suite runs against a mock transport with a hand-advanced clock — no network."""

    #: The GitHub-hosted registry rate-limits hard (a handful of requests per window), so its
    #: responses are held at least this long regardless of the configured TTL.
    _GITHUB_MIN_TTL_S = 600.0

    def __init__(
        self,
        registries: list[RegistryInfo] | None = None,
        *,
        http_factory: Callable[[], httpx.AsyncClient] | None = None,
        cache_ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registries = {r.id: r for r in (registries or default_registries())}
        # Generous timeout: the official registry's substring search can take 15s+ on a cold
        # query — a browse that times out right before the page arrives reads as "the hub is
        # down" when it isn't.
        self._http_factory = http_factory or (lambda: httpx.AsyncClient(timeout=45.0))
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}

    def registries(self) -> list[RegistryInfo]:
        return list(self._registries.values())

    def set_registries(self, registries: list[RegistryInfo]) -> None:
        """Replace the browsable registry set in place (the live-settings apply path — extra
        self-hosted registries can change at runtime). The response cache is dropped so a page
        cached under a removed/replaced registry can never serve again."""
        self._registries = {r.id: r for r in registries}
        self._cache.clear()

    def _registry(self, registry: str) -> RegistryInfo:
        try:
            return self._registries[registry]
        except KeyError:
            raise McpRegistryError(
                f"unknown registry {registry!r}; known: {sorted(self._registries)}"
            ) from None

    def _ttl(self, registry_id: str) -> float:
        if registry_id == "github":
            return max(self._cache_ttl_s, self._GITHUB_MIN_TTL_S)
        return self._cache_ttl_s

    async def _get_json(
        self,
        registry: RegistryInfo,
        path: str,
        params: dict[str, Any],
        *,
        key: tuple[Any, ...],
    ) -> dict[str, Any]:
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < self._ttl(registry.id):
            return cached[1]
        url = registry.url.rstrip("/") + path
        try:
            async with self._http_factory() as http:
                response = await http.get(url, params=params)
        except httpx.HTTPError as exc:
            # Some transport errors (timeouts) stringify empty — always name the type.
            reason = str(exc) or type(exc).__name__
            raise McpRegistryError(f"registry {registry.id!r} unreachable: {reason}") from exc
        if response.status_code >= 400:
            raise McpRegistryError(
                f"registry {registry.id!r} returned {response.status_code} for {path}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise McpRegistryError(
                f"registry {registry.id!r} returned non-JSON for {path}"
            ) from exc
        if not isinstance(payload, dict):
            raise McpRegistryError(f"registry {registry.id!r} returned an unexpected payload shape")
        self._cache[key] = (now, payload)
        return payload

    async def list_servers(
        self,
        registry: str,
        *,
        search: str = "",
        limit: int = 30,
        cursor: str | None = None,
    ) -> CatalogPage:
        """One page of a registry listing, always at the latest version of each server. The
        ``search`` term is sent upstream AND re-applied locally as a name/title/description
        substring filter — some registries ignore the parameter, and re-filtering a page that
        was already narrowed is harmless."""
        info = self._registry(registry)
        limit = max(1, min(100, limit))
        params: dict[str, Any] = {"version": "latest", "limit": limit}
        if search:
            params["search"] = search
        if cursor:
            params["cursor"] = cursor
        key = (info.id, "list", search, limit, cursor)
        payload = await self._get_json(info, "/v0.1/servers", params, key=key)
        servers = payload.get("servers")
        if not isinstance(servers, list):
            raise McpRegistryError(f"registry {info.id!r} returned no server list")
        entries = [entry_from_payload(info.id, item) for item in servers if isinstance(item, dict)]
        if search:
            needle = search.lower()
            entries = [
                e
                for e in entries
                if needle in e.name.lower()
                or needle in (e.title or "").lower()
                or needle in e.description.lower()
            ]
        next_cursor = (payload.get("metadata") or {}).get("nextCursor")
        return CatalogPage(
            entries=entries,
            next_cursor=next_cursor if isinstance(next_cursor, str) else None,
        )

    async def get_server(self, registry: str, name: str, version: str = "latest") -> CatalogDetail:
        """One server at one version, with its install candidates. The name contains a ``/``
        (namespace/name), so it is URL-encoded into a single path segment."""
        info = self._registry(registry)
        path = f"/v0.1/servers/{quote(name, safe='')}/versions/{quote(version, safe='')}"
        key = (info.id, "get", name, version)
        payload = await self._get_json(info, path, {}, key=key)
        server = payload.get("server")
        if not isinstance(server, dict):
            raise McpRegistryError(f"registry {info.id!r} returned no server for {name!r}")
        return CatalogDetail(
            entry=entry_from_payload(info.id, payload),
            candidates=install_candidates(server),
        )
