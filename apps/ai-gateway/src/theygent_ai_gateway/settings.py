"""Gateway settings, read once from the environment at startup.

Every read treats an empty value exactly like an absent one: deployment manifests pass knobs
through as ``${VAR:-}``, and an empty string must never become a value or crash a parse. A
non-empty value that does not parse fails loudly with the variable's name.

The gateway never reads ``ANTHROPIC_BASE_URL``: that variable points Claude Code *at* the
gateway, so a gateway honouring it would forward requests to itself.
"""

from __future__ import annotations

import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast, get_args
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError
from theygent_ir.gateway import Token

AuthMode = Literal["personal", "team"]

ENV_HOST = "THEYGENT_AI_GATEWAY_HOST"
ENV_PORT = "THEYGENT_AI_GATEWAY_PORT"
ENV_UPSTREAM_URL = "THEYGENT_AI_GATEWAY_UPSTREAM_URL"
ENV_AUTH_MODE = "THEYGENT_AI_GATEWAY_AUTH_MODE"
ENV_STATE_DIR = "THEYGENT_AI_GATEWAY_STATE_DIR"
ENV_GATEWAY_ID = "THEYGENT_AI_GATEWAY_ID"
#: Authenticates the gateway to the control plane (key sync, config, records). A deployment secret.
ENV_SERVICE_KEY = "THEYGENT_AI_GATEWAY_SERVICE_KEY"
#: The organization's Anthropic API key, injected for developers on the org_key profile. A
#: deployment secret; it never transits the control plane.
ENV_ORG_API_KEY = "THEYGENT_AI_GATEWAY_ORG_API_KEY"
ENV_KEY_SYNC_SECONDS = "THEYGENT_AI_GATEWAY_KEY_SYNC_SECONDS"
#: Read by every service that reaches the control plane, as THEYGENT_INFERENCE_PLANE_URL is for
#: the inference plane.
ENV_CONTROL_PLANE_URL = "THEYGENT_CONTROL_PLANE_URL"

DEFAULT_PORT = 8082
#: How often team mode re-syncs developer keys; a revoked key is refused within one interval.
DEFAULT_KEY_SYNC_SECONDS = 30.0
DEFAULT_UPSTREAM_URL = "https://api.anthropic.com"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_TOKEN = TypeAdapter(Token)


class SettingsError(ValueError):
    """A setting is invalid. The message names the environment variable to fix."""


def _default_state_dir() -> Path:
    return Path.home() / ".theygent" / "ai-gateway"


def _default_gateway_id() -> str:
    """``gw-<hostname>``, reduced to the characters a gateway id may carry."""
    host = re.sub(r"[^A-Za-z0-9._-]+", "-", socket.gethostname()).strip("-.") or "local"
    return f"gw-{host}"[:128]


@dataclass(frozen=True)
class GatewaySettings:
    """Everything the gateway needs to start. Validated on construction, so a settings object
    that exists is a usable one."""

    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    upstream_url: str = DEFAULT_UPSTREAM_URL
    control_plane_url: str | None = None
    auth_mode: AuthMode = "personal"
    state_dir: Path = field(default_factory=_default_state_dir)
    gateway_id: str = field(default_factory=_default_gateway_id)
    service_key: str | None = field(default=None, repr=False)
    org_api_key: str | None = field(default=None, repr=False)
    key_sync_seconds: float = DEFAULT_KEY_SYNC_SECONDS

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise SettingsError(f"{ENV_PORT} must be between 1 and 65535, got {self.port}")
        if self.auth_mode not in get_args(AuthMode):
            raise SettingsError(
                f"{ENV_AUTH_MODE} must be one of {', '.join(get_args(AuthMode))}, "
                f"got {self.auth_mode!r}"
            )
        if self.auth_mode == "personal" and self.host not in LOOPBACK_HOSTS:
            raise SettingsError(
                f"personal mode accepts requests without a gateway key, so it listens on "
                f"loopback only; {ENV_HOST}={self.host!r} would expose it. Set "
                f"{ENV_AUTH_MODE}=team to serve other machines"
            )
        if self.auth_mode == "team" and self.control_plane_url is None:
            raise SettingsError(
                f"team mode authenticates developers with keys from the control plane; "
                f"set {ENV_CONTROL_PLANE_URL}"
            )
        if self.auth_mode == "team" and self.service_key is None:
            raise SettingsError(
                f"team mode fetches developer keys from the control plane as a service; "
                f"set {ENV_SERVICE_KEY}"
            )
        if self.auth_mode == "personal" and self.org_api_key is not None:
            raise SettingsError(
                f"{ENV_ORG_API_KEY} is for team mode's org_key profile; personal mode forwards "
                "the caller's own credential"
            )
        if not 1 <= self.key_sync_seconds <= 3600:
            raise SettingsError(
                f"{ENV_KEY_SYNC_SECONDS} must be between 1 and 3600, got {self.key_sync_seconds}"
            )
        object.__setattr__(self, "upstream_url", _http_url(ENV_UPSTREAM_URL, self.upstream_url))
        if self.control_plane_url is not None:
            object.__setattr__(
                self,
                "control_plane_url",
                _http_url(ENV_CONTROL_PLANE_URL, self.control_plane_url),
            )
        if _points_at_self(self.upstream_url, self.port):
            raise SettingsError(
                f"{ENV_UPSTREAM_URL}={self.upstream_url!r} is this gateway's own address; it "
                "must name the Anthropic API (or another upstream), not the gateway"
            )
        try:
            _TOKEN.validate_python(self.gateway_id)
        except ValidationError as exc:
            raise SettingsError(
                f"{ENV_GATEWAY_ID}={self.gateway_id!r} is not a valid id: up to 128 letters, "
                "digits and . _ : @ + / - characters, starting with a letter or digit"
            ) from exc
        object.__setattr__(self, "state_dir", self.state_dir.expanduser())


def _http_url(name: str, value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SettingsError(f"{name} must be an http(s) URL, got {value!r}")
    return value.rstrip("/")


def _points_at_self(url: str, port: int) -> bool:
    parts = urlsplit(url)
    return parts.hostname in LOOPBACK_HOSTS and parts.port == port


def _read(env: Mapping[str, str], name: str) -> str | None:
    value = (env.get(name) or "").strip()
    return value or None


def _read_float(env: Mapping[str, str], name: str) -> float | None:
    raw = _read(env, name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a number, got {raw!r}") from None


def _read_int(env: Mapping[str, str], name: str) -> int | None:
    raw = _read(env, name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be an integer, got {raw!r}") from None


def load_settings(env: Mapping[str, str]) -> GatewaySettings:
    """Build settings from an environment mapping (normally ``os.environ``)."""
    state_dir = _read(env, ENV_STATE_DIR)
    port = _read_int(env, ENV_PORT)
    return GatewaySettings(
        host=_read(env, ENV_HOST) or "127.0.0.1",
        # An explicit 0 is a value to reject, not a reason to fall back to the default.
        port=DEFAULT_PORT if port is None else port,
        upstream_url=_read(env, ENV_UPSTREAM_URL) or DEFAULT_UPSTREAM_URL,
        control_plane_url=_read(env, ENV_CONTROL_PLANE_URL),
        # Checked against the AuthMode vocabulary on construction.
        auth_mode=cast(AuthMode, _read(env, ENV_AUTH_MODE) or "personal"),
        state_dir=Path(state_dir) if state_dir else _default_state_dir(),
        gateway_id=_read(env, ENV_GATEWAY_ID) or _default_gateway_id(),
        service_key=_read(env, ENV_SERVICE_KEY),
        org_api_key=_read(env, ENV_ORG_API_KEY),
        key_sync_seconds=(
            DEFAULT_KEY_SYNC_SECONDS
            if (sync := _read_float(env, ENV_KEY_SYNC_SECONDS)) is None
            else sync
        ),
    )
