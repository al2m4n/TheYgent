"""theygent gateway-client — the shared, transport-only inference-plane seam.

A thin async OpenAI-compatible client: ``baseUrl`` + a *logical* model id +
messages/params -> streamed chunks. It is **stateless and transport-only** — no run
logic, no business logic, no model registry. Both the control-plane and the durable
worker reuse it. It is "just an OpenAI client with a baseUrl":
talking to the inference plane's OpenAI-compatible ``/v1/*`` data plane validates the
OpenAI-compat claim end to end.
"""

from __future__ import annotations

from theygent_gateway_client.client import GatewayClient

__all__ = ["GatewayClient"]
