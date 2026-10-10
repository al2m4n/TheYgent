"""Gateway settings: empty means unset, bad values fail loudly by name, and the two safety
rules — personal mode stays on loopback, and the gateway never forwards to itself."""

from __future__ import annotations

from pathlib import Path

import pytest
from theygent_ai_gateway.settings import (
    DEFAULT_PORT,
    DEFAULT_UPSTREAM_URL,
    ENV_AUTH_MODE,
    ENV_CONTROL_PLANE_URL,
    ENV_GATEWAY_ID,
    ENV_HEARTBEAT_SECONDS,
    ENV_HOST,
    ENV_KEY_SYNC_SECONDS,
    ENV_ORG_API_KEY,
    ENV_PORT,
    ENV_SERVICE_KEY,
    ENV_STATE_DIR,
    ENV_UPSTREAM_URL,
    GatewaySettings,
    SettingsError,
    load_settings,
)

ALL_VARS = [
    ENV_SERVICE_KEY,
    ENV_ORG_API_KEY,
    ENV_KEY_SYNC_SECONDS,
    ENV_HEARTBEAT_SECONDS,
    ENV_HOST,
    ENV_PORT,
    ENV_UPSTREAM_URL,
    ENV_CONTROL_PLANE_URL,
    ENV_AUTH_MODE,
    ENV_STATE_DIR,
    ENV_GATEWAY_ID,
]


def test_defaults_with_an_empty_environment() -> None:
    settings = load_settings({})
    assert settings.host == "127.0.0.1"
    assert settings.port == DEFAULT_PORT == 8082
    assert settings.upstream_url == DEFAULT_UPSTREAM_URL
    assert settings.auth_mode == "personal"
    assert settings.control_plane_url is None
    assert settings.state_dir == Path.home() / ".theygent" / "ai-gateway"
    assert settings.gateway_id.startswith("gw-")
    assert settings.heartbeat_seconds == 30.0


def test_empty_values_mean_unset() -> None:
    # Manifests pass knobs through as ${VAR:-}; an empty string must behave like absence.
    assert load_settings({name: "" for name in ALL_VARS}) == load_settings({})
    assert load_settings({name: "  " for name in ALL_VARS}) == load_settings({})


def test_values_are_read(tmp_path: Path) -> None:
    settings = load_settings(
        {
            ENV_HOST: "0.0.0.0",
            ENV_PORT: "9000",
            ENV_UPSTREAM_URL: "https://api.anthropic.com/",
            ENV_CONTROL_PLANE_URL: "http://control-plane:8080/",
            ENV_AUTH_MODE: "team",
            ENV_STATE_DIR: str(tmp_path),
            ENV_GATEWAY_ID: "gw-cluster-1",
            ENV_SERVICE_KEY: "tygs_service_example",
            ENV_ORG_API_KEY: "sk-ant-api03-org-example",
            ENV_KEY_SYNC_SECONDS: "5",
            ENV_HEARTBEAT_SECONDS: "12.5",
        }
    )
    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.upstream_url == "https://api.anthropic.com"  # trailing slash dropped
    assert settings.control_plane_url == "http://control-plane:8080"
    assert settings.auth_mode == "team"
    assert settings.state_dir == tmp_path
    assert settings.gateway_id == "gw-cluster-1"
    assert settings.service_key == "tygs_service_example"
    assert settings.org_api_key == "sk-ant-api03-org-example"
    assert settings.key_sync_seconds == 5.0
    assert settings.heartbeat_seconds == 12.5


def test_state_dir_expands_the_home_directory() -> None:
    assert load_settings({ENV_STATE_DIR: "~/gw"}).state_dir == Path.home() / "gw"


@pytest.mark.parametrize("value", ["abc", "8082.5"])
def test_a_non_integer_port_fails_by_name(value: str) -> None:
    with pytest.raises(SettingsError, match=ENV_PORT):
        load_settings({ENV_PORT: value})


@pytest.mark.parametrize("value", ["0", "70000", "-1"])
def test_an_out_of_range_port_fails_by_name(value: str) -> None:
    with pytest.raises(SettingsError, match=ENV_PORT):
        load_settings({ENV_PORT: value})


def test_an_unknown_auth_mode_fails_by_name() -> None:
    with pytest.raises(SettingsError, match=ENV_AUTH_MODE):
        load_settings({ENV_AUTH_MODE: "open"})


def test_personal_mode_refuses_a_non_loopback_address() -> None:
    with pytest.raises(SettingsError, match="loopback"):
        load_settings({ENV_HOST: "0.0.0.0"})


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_personal_mode_accepts_loopback(host: str) -> None:
    assert load_settings({ENV_HOST: host}).host == host


def test_team_mode_needs_the_control_plane() -> None:
    with pytest.raises(SettingsError, match=ENV_CONTROL_PLANE_URL):
        load_settings({ENV_AUTH_MODE: "team", ENV_HOST: "0.0.0.0"})


def test_team_mode_needs_a_service_key() -> None:
    with pytest.raises(SettingsError, match=ENV_SERVICE_KEY):
        load_settings(
            {ENV_AUTH_MODE: "team", ENV_HOST: "0.0.0.0", ENV_CONTROL_PLANE_URL: "http://cp:8080"}
        )


def test_personal_mode_refuses_an_org_key() -> None:
    # Personal mode forwards the caller's own credential; an org key there would be ignored.
    with pytest.raises(SettingsError, match=ENV_ORG_API_KEY):
        load_settings({ENV_ORG_API_KEY: "sk-ant-api03-org"})


@pytest.mark.parametrize("name", [ENV_KEY_SYNC_SECONDS, ENV_HEARTBEAT_SECONDS])
@pytest.mark.parametrize("value", ["0", "0.5", "7200"])
def test_sync_intervals_are_bounded(name: str, value: str) -> None:
    with pytest.raises(SettingsError, match=name):
        load_settings({name: value})


@pytest.mark.parametrize("name", [ENV_KEY_SYNC_SECONDS, ENV_HEARTBEAT_SECONDS])
def test_a_non_numeric_sync_interval_fails_by_name(name: str) -> None:
    with pytest.raises(SettingsError, match=name):
        load_settings({name: "often"})


def test_secrets_never_appear_in_repr() -> None:
    settings = GatewaySettings(
        auth_mode="team",
        host="0.0.0.0",
        control_plane_url="http://cp:8080",
        service_key="tygs_SECRET_SERVICE",
        org_api_key="sk-ant-api03-SECRET-ORG",
    )
    assert "SECRET" not in repr(settings)


@pytest.mark.parametrize("url", ["api.anthropic.com", "ftp://api.anthropic.com", "https://"])
def test_upstream_must_be_an_http_url(url: str) -> None:
    with pytest.raises(SettingsError, match=ENV_UPSTREAM_URL):
        load_settings({ENV_UPSTREAM_URL: url})


def test_control_plane_must_be_an_http_url() -> None:
    with pytest.raises(SettingsError, match=ENV_CONTROL_PLANE_URL):
        load_settings({ENV_CONTROL_PLANE_URL: "control-plane:8080"})


@pytest.mark.parametrize("url", ["http://127.0.0.1:8082", "http://localhost:8082/"])
def test_the_gateway_never_forwards_to_itself(url: str) -> None:
    # The easy mistake: copying Claude Code's ANTHROPIC_BASE_URL into the gateway's upstream.
    with pytest.raises(SettingsError, match="own address"):
        load_settings({ENV_UPSTREAM_URL: url})


def test_another_local_port_is_a_valid_upstream() -> None:
    # e.g. a local Anthropic-format server, or a test double.
    assert load_settings({ENV_UPSTREAM_URL: "http://127.0.0.1:18099"}).upstream_url == (
        "http://127.0.0.1:18099"
    )


@pytest.mark.parametrize("gateway_id", ["has space", "-leading-dash", "x" * 129])
def test_an_invalid_gateway_id_fails_by_name(gateway_id: str) -> None:
    with pytest.raises(SettingsError, match=ENV_GATEWAY_ID):
        load_settings({ENV_GATEWAY_ID: gateway_id})


def test_the_default_gateway_id_is_always_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "Dev's Laptop (2).local")
    gateway_id = GatewaySettings().gateway_id
    assert gateway_id == "gw-Dev-s-Laptop-2-.local"


def test_settings_are_immutable() -> None:
    settings = GatewaySettings()
    with pytest.raises(AttributeError):
        setattr(settings, "port", 1)  # noqa: B010 — assigning a frozen field on purpose
