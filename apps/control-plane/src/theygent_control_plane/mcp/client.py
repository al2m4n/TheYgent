"""The ``McpClient`` lifecycle seam + the stdio transport implementation (m7.md §3.1).

``McpClient`` is the protocol the control-plane depends on; ``StdioMcpClient`` is M7's only
transport — it wraps the official ``mcp`` Python SDK over stdio (the server is a subprocess,
JSON-RPC over stdin/stdout). **The SDK is wrapped here and nowhere else** (same discipline as
``gateway-client`` wrapping the OpenAI SDK): the rest of the control-plane imports only this
module's protocol + dataclasses, never ``mcp`` directly. HTTP/SSE is a future additive
implementation against the same protocol (m7.md §9).

The hard part this module owns: a **persistent** connection that survives across request tasks.
The SDK's ``stdio_client`` / ``ClientSession`` are anyio context managers whose cancel scopes
must be entered and exited on the *same* task. So the connection is owned by a **dedicated
background task** (``_run``) that enters the contexts once, then serves ``call_tool`` requests off
a queue until ``close`` — the actor pattern. Callers (request tasks) never touch the contexts
directly; they enqueue work, so no cross-task cancel-scope violation is possible.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, Field


@dataclass(frozen=True)
class McpToolDescriptor:
    """One tool an MCP server exposes (from ``list_tools``) — name + JSON-Schema for its args."""

    name: str
    description: str | None
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class McpResult:
    """The outcome of a ``call_tool``. ``is_error`` is a *tool-level* error the server reported
    (``CallToolResult.isError``) — a structured result, NOT a transport failure (which raises
    :class:`McpConnectionError`). The walker binds ``err`` on ``is_error``, ``ok`` otherwise."""

    value: Any
    is_error: bool


class McpConnectionError(RuntimeError):
    """A transport/connection failure: the server didn't spawn, the process died, or the session
    broke. Distinct from a tool-level error (:class:`McpResult` with ``is_error``). The manager
    catches this to drive its one reconnect-retry (m7.md §3.2)."""


class McpToolNotFound(KeyError):
    """A ``call_tool`` named a tool the connected server doesn't expose. The walker binds ``err``
    (runtime miss — caught at validation only if the server was already connected, m7.md §4)."""


class McpServerConfig(BaseModel):
    """A registered MCP server (the ``/admin/mcp/servers/{name}`` payload, m7.md §3.2). M7 ships
    only ``stdio``. ``env`` carries the user's secrets/paths INTO the subprocess they spawn — it
    stays in the user's trust domain, never logged with values, never resolved in theygent cloud
    (the §10 sovereignty invariant)."""

    transport: Literal["stdio"] = "stdio"
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None


class McpClient(Protocol):
    """The lifecycle seam (m7.md §3.1). Transport-agnostic; ``StdioMcpClient`` is M7's only impl."""

    async def connect(self) -> None: ...
    async def list_tools(self) -> list[McpToolDescriptor]: ...
    async def call_tool(self, name: str, args: dict[str, Any]) -> McpResult: ...
    async def close(self) -> None: ...


def _result_value(result: types.CallToolResult) -> Any:
    """Reduce an MCP tool result to one value: prefer the **text** content (the clean, portable
    form — a string tool returns its string, the filesystem server returns the file body), falling
    back to structured content only when there's no text. Text is preferred deliberately: many
    servers (FastMCP included) wrap a scalar return as ``structuredContent={"result": ...}``, and a
    downstream ``$in.a.b`` ref re-parses a JSON-string value anyway — so the text is the least
    surprising binding for the ``ok`` handle."""

    texts = [c.text for c in result.content if isinstance(c, types.TextContent)]
    if texts:
        return "\n".join(texts)
    return result.structuredContent


# A request queued to the actor task: (tool name, args, future to resolve). ``None`` = close.
_Call = tuple[str, dict[str, Any], "asyncio.Future[McpResult]"]


class StdioMcpClient:
    """A persistent stdio MCP connection owned by a single background task (m7.md §3.1)."""

    def __init__(
        self,
        *,
        command: str,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self._params = StdioServerParameters(
            command=command, args=list(args), env=dict(env) if env else None, cwd=cwd
        )
        self._queue: asyncio.Queue[_Call | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._tools: list[McpToolDescriptor] = []

    async def connect(self) -> None:
        """Spawn the server and initialize the session on a dedicated task. Raises
        :class:`McpConnectionError` if the process won't start or the handshake fails."""

        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._task = loop.create_task(self._run())
        try:
            await self._ready
        except Exception as exc:
            await self.close()
            raise McpConnectionError(str(exc)) from exc

    async def _run(self) -> None:
        assert self._ready is not None
        try:
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(self._params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                self._tools = [
                    McpToolDescriptor(t.name, t.description, dict(t.inputSchema or {}))
                    for t in listed.tools
                ]
                self._ready.set_result(None)
                await self._serve(session)
        except Exception as exc:
            # A failure before ready surfaces to connect(); after ready, it ends the task and the
            # next call_tool sees a dead task -> McpConnectionError -> manager reconnects.
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            # Fail any calls still queued when the task ends, so a caller never hangs on a dead
            # connection — the manager retries on the next call.
            while not self._queue.empty():
                pending = self._queue.get_nowait()
                if pending is not None and not pending[2].done():
                    pending[2].set_exception(McpConnectionError("MCP connection closed"))

    async def _serve(self, session: ClientSession) -> None:
        while True:
            item = await self._queue.get()
            if item is None:  # close sentinel
                return
            name, args, fut = item
            try:
                result = await session.call_tool(name, args)
                if not fut.done():
                    res = McpResult(value=_result_value(result), is_error=bool(result.isError))
                    fut.set_result(res)
            except Exception as exc:  # transport broke mid-call: fail this call AND end the task
                if not fut.done():  # (a dead session can't serve further calls) so the manager
                    fut.set_exception(exc)  # reconnects on the retry.
                raise

    async def list_tools(self) -> list[McpToolDescriptor]:
        # Cached on connect for the connection lifetime (m7.md §4 capability caching).
        return list(self._tools)

    async def call_tool(self, name: str, args: dict[str, Any]) -> McpResult:
        if self._task is None or self._task.done():
            raise McpConnectionError("MCP client is not connected")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[McpResult] = loop.create_future()
        await self._queue.put((name, args, fut))
        # Close the put-after-death race: if the task ended between the guard and the put, the
        # drain may have missed our item, so fail it here rather than hang.
        if self._task.done() and not fut.done():
            fut.set_exception(McpConnectionError("MCP client is not connected"))
        try:
            return await fut
        except McpConnectionError:
            raise
        except Exception as exc:  # any underlying SDK/transport error is a connection failure
            raise McpConnectionError(str(exc)) from exc

    async def close(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            with contextlib.suppress(Exception):
                self._queue.put_nowait(None)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
        if not self._task.done():
            self._task.cancel()
            with contextlib.suppress(Exception):
                await self._task
        self._task = None
