"""HTTP access to TheYgent's two planes, with failures turned into actionable tool errors."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastmcp.exceptions import ToolError

from theygent_mcp.config import Config


class Planes:
    """One shared ``httpx.AsyncClient`` for both planes. ``transport`` is injectable so the fast
    suite can answer every request in-process."""

    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._http = httpx.AsyncClient(transport=transport, timeout=60.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── control plane ────────────────────────────────────────────────────────────────────────

    async def control(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        content: bytes | AsyncIterator[bytes] | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        if not self.config.api_key:
            raise ToolError(
                "THEYGENT_API_KEY is not set. Create an API key in TheYgent (your profile menu → "
                "API keys) and add it to this MCP server's environment."
            )
        url = f"{self.config.control_plane_url}{path}"
        all_headers = {"Authorization": f"Bearer {self.config.api_key}", **(headers or {})}
        return await self._send(
            "TheYgent control plane",
            self.config.control_plane_url,
            method,
            url,
            json=json,
            params=params,
            content=content,
            headers=all_headers,
            timeout_s=timeout_s,
        )

    # ── inference plane ──────────────────────────────────────────────────────────────────────

    async def inference(
        self, method: str, path: str, *, json: Any = None, timeout_s: float | None = None
    ) -> Any:
        """``path`` under the OpenAI-compatible root, e.g. ``/chat/completions``."""
        url = f"{self.config.inference_plane_url}{path}"
        return await self._send(
            "TheYgent inference plane",
            self.config.inference_plane_url,
            method,
            url,
            json=json,
            timeout_s=timeout_s,
        )

    async def inference_admin(self, method: str, path: str) -> Any:
        """``path`` under the inference plane's management surface, e.g. ``/admin/models``."""
        url = f"{self.config.inference_admin_url}{path}"
        return await self._send(
            "TheYgent inference plane", self.config.inference_admin_url, method, url
        )

    # ── shared ───────────────────────────────────────────────────────────────────────────────

    async def _send(self, service: str, base: str, method: str, url: str, **kwargs: Any) -> Any:
        timeout_s = kwargs.pop("timeout_s", None)
        try:
            resp = await self._http.request(
                method, url, timeout=timeout_s if timeout_s is not None else 60.0, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise ToolError(f"{service} at {base} did not answer in time ({exc}).") from exc
        except httpx.HTTPError as exc:
            raise ToolError(
                f"{service} is not reachable at {base} ({exc}). Start TheYgent (`make up` in the "
                "TheYgent checkout) or point this MCP server at it with the plane URL variables."
            ) from exc
        if resp.status_code == 401:
            raise ToolError(
                f"{service} rejected the API key (401). Check THEYGENT_API_KEY: create a new key "
                "in TheYgent under your profile menu → API keys."
            )
        if resp.status_code == 403:
            raise ToolError(
                f"{service} refused this action (403): the API key's user needs the editor role."
            )
        if resp.status_code >= 400:
            raise ToolError(f"{service} error {resp.status_code}: {_error_text(resp)}")
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500] or resp.reason_phrase
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message") or ""
        return f"{code}: {message}" if code else message
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])[:500]
    return str(body)[:500]
