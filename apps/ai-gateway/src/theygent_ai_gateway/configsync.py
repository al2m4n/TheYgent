"""The configuration in effect, where it comes from, and the heartbeat.

The control plane serves versioned, immutable configs at ``GET /gateway/config``. The gateway asks
with ``If-None-Match`` (the version is the ETag, so an unchanged config costs a ``304``), applies a
newer one live, and keeps it on disk as the last known good: a restart with the control plane down
serves under the same config. Every interval the gateway also reports itself at
``POST /gateway/heartbeat``; the acknowledgement names the latest config version, and the gateway
pulls only when that differs from the one it applied (or when the heartbeat failed).

Until any config has arrived, a gateway attached to a control plane forwards in passthrough mode:
an organization's traffic is recorded only once an admin has chosen observer mode. A standalone
personal gateway has no one to configure it, so it observes from the start with the default
policy.

Swapping the config is atomic: an exchange reads one ``Snapshot`` (config, mode, prices) when it
starts, and the record it produces names that snapshot's version.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import ValidationError
from theygent_ir.gateway import (
    GatewayConfig,
    GatewayHeartbeat,
    GatewayHeartbeatAck,
    GatewayMode,
)

from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.observe import Observation

log = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
#: Failures a sync survives: the control plane unreachable or answering nonsense, a bad disk.
SYNC_ERRORS = (httpx.HTTPError, ValueError, ValidationError, OSError)


def software_version() -> str:
    try:
        return metadata.version("theygent-ai-gateway")
    except metadata.PackageNotFoundError:
        return "unknown"


@dataclass(frozen=True)
class Snapshot:
    """One consistent view of the configuration."""

    config: GatewayConfig | None
    mode: GatewayMode
    #: List prices with the config's contracted-rate overrides applied.
    prices: PriceBook

    @property
    def version(self) -> int | None:
        return self.config.version if self.config is not None else None


class ConfigState:
    """The config in effect. Versions only move forward; each adopted config replaces the
    snapshot in one assignment."""

    def __init__(
        self, prices: PriceBook, *, unconfigured_mode: GatewayMode, path: Path | None = None
    ) -> None:
        self._prices = prices
        self._path = path
        self._snapshot = Snapshot(None, unconfigured_mode, prices)

    @property
    def current(self) -> Snapshot:
        return self._snapshot

    def apply(self, config: GatewayConfig, *, persist: bool = True) -> bool:
        """Adopt ``config`` if it is newer than the one in effect; ``persist`` keeps it on disk as
        the last known good."""
        current = self._snapshot.config
        if current is not None and config.version <= current.version:
            return False
        self._snapshot = Snapshot(
            config, config.mode, self._prices.with_overrides(config.pricing_overrides)
        )
        if persist:
            self._persist(config)
        return True

    def load_from_disk(self) -> bool:
        """Adopt the last known good config. A missing or unreadable file is no config, never a
        crash."""
        if self._path is None or not self._path.exists():
            return False
        try:
            config = GatewayConfig.model_validate_json(self._path.read_bytes())
        except (OSError, ValueError, ValidationError) as exc:
            log.warning("ignoring the persisted config: %s", type(exc).__name__)
            return False
        return self.apply(config, persist=False)

    def _persist(self, config: GatewayConfig) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(config.model_dump_json())
            temporary.replace(self._path)
        except OSError as exc:
            log.warning("could not persist the config: %s", type(exc).__name__)


class ConfigSource(Protocol):
    async def fetch(self, since_version: int | None) -> GatewayConfig | None:
        """The current config, or ``None`` when it is unchanged since ``since_version`` or none
        has been saved yet."""
        ...

    async def heartbeat(self, beat: GatewayHeartbeat) -> GatewayHeartbeatAck: ...


class ControlPlaneConfigSource:
    """The control plane's ``GET /gateway/config`` (``304`` when unchanged, ``404`` before any
    config exists) and ``POST /gateway/heartbeat``, authenticated with the gateway's service
    key."""

    def __init__(self, client: httpx.AsyncClient, control_plane_url: str, service_key: str) -> None:
        self._client = client
        self._config_url = f"{control_plane_url}/gateway/config"
        self._heartbeat_url = f"{control_plane_url}/gateway/heartbeat"
        self._auth = {"authorization": f"Bearer {service_key}"}

    async def fetch(self, since_version: int | None) -> GatewayConfig | None:
        headers = dict(self._auth)
        if since_version is not None:
            headers["if-none-match"] = f'"{since_version}"'
        response = await self._client.get(self._config_url, headers=headers)
        if response.status_code in (304, 404):
            return None
        response.raise_for_status()
        return GatewayConfig.model_validate_json(response.content)

    async def heartbeat(self, beat: GatewayHeartbeat) -> GatewayHeartbeatAck:
        response = await self._client.post(
            self._heartbeat_url,
            content=beat.model_dump_json(),
            headers={**self._auth, "content-type": "application/json"},
        )
        response.raise_for_status()
        return GatewayHeartbeatAck.model_validate_json(response.content)


class StaticConfigSource:
    """A config source whose config a caller sets directly; it keeps the heartbeats it receives
    (tests, and single-machine setups)."""

    def __init__(self, config: GatewayConfig | None = None) -> None:
        self.config = config
        self.beats: list[GatewayHeartbeat] = []

    async def fetch(self, since_version: int | None) -> GatewayConfig | None:
        if self.config is None or self.config.version == since_version:
            return None
        return self.config

    async def heartbeat(self, beat: GatewayHeartbeat) -> GatewayHeartbeatAck:
        self.beats.append(beat)
        return GatewayHeartbeatAck(config_version=self.config.version if self.config else None)


class UpstreamHealth:
    """Whether the upstream answered the last exchange that reached it, for the heartbeat.
    ``None`` until one has."""

    def __init__(self) -> None:
        self.reachable: bool | None = None

    def observe(self, observation: Observation) -> None:
        if observation.outcome == "transport_error":
            self.reachable = False
        elif observation.status is not None and observation.outcome != "rejected":
            self.reachable = True


async def sync_config_once(
    state: ConfigState,
    source: ConfigSource,
    beat: Callable[[], Awaitable[GatewayHeartbeat]],
) -> None:
    """One heartbeat and, when its acknowledgement names another config version (or there was no
    acknowledgement), one config pull. A failure keeps the config in effect."""
    try:
        ack = await source.heartbeat(await beat())
    except SYNC_ERRORS as exc:
        log.warning("heartbeat failed: %s", type(exc).__name__)
        pull = True
    else:
        pull = ack.config_version != state.current.version
    if not pull:
        return
    try:
        config = await source.fetch(state.current.version)
    except SYNC_ERRORS as exc:
        log.warning("config sync failed: %s", type(exc).__name__)
        return
    if config is not None and state.apply(config):
        log.info("config version %d applied (mode %s)", config.version, config.mode)


async def sync_config_forever(
    state: ConfigState,
    source: ConfigSource,
    beat: Callable[[], Awaitable[GatewayHeartbeat]],
    interval_seconds: float,
) -> None:
    """Sync every interval until cancelled. An error no sync expects is logged and the loop
    carries on: a sync that stopped would leave the gateway on a stale config without a sign."""
    while True:
        try:
            await sync_config_once(state, source, beat)
        except Exception as exc:
            log.error("config sync failed unexpectedly: %s", type(exc).__name__)
        await asyncio.sleep(interval_seconds)
