"""The ``McpClient`` lifecycle seam + the stdio AND http transport implementations.

``McpClient`` is the protocol the control-plane depends on; ``StdioMcpClient`` (the server is a
subprocess, JSON-RPC over stdin/stdout) and ``HttpMcpClient`` (streamable-HTTP / SSE to a remote
server) are the two transports — both wrap the official ``mcp`` SDK. **The SDK is wrapped here and
nowhere else** (same discipline as ``gateway-client`` wrapping the OpenAI SDK): the rest of the
control-plane imports only this module's protocol + dataclasses, never ``mcp`` directly. (Stdio
shipped first; http was added against the SAME protocol — the node contract is identical.)

The hard part this module owns: a **persistent** connection that survives across request tasks. The
SDK's transport / ``ClientSession`` are anyio context managers whose cancel scopes must be entered
and exited on the *same* task. So the connection is owned by a **dedicated background task**
(``_run``) that enters the contexts once, then serves ``call_tool`` off a queue until close — the
actor pattern. Callers (request tasks) never touch the contexts directly; they enqueue work, so no
cross-task cancel-scope violation is possible. Both transports share that machinery
(``_ActorMcpClient``); only ``_open_transport`` differs, so stdio and http behave identically."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
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
    catches this to drive its one reconnect-retry."""


class McpToolNotFound(KeyError):
    """A ``call_tool`` named a tool the connected server doesn't expose. The walker binds ``err``
    (runtime miss — caught at validation only if the server was already connected)."""


class McpServerConfig(BaseModel):
    """A registered MCP server (the ``/admin/mcp/servers/{name}`` payload).
    ``transport`` is ``stdio`` (a local subprocess — ``command``/``args``/``env``/``cwd``) or
    ``http`` (a remote streamable-HTTP/SSE server — ``url``/``headers``). For stdio, ``env`` carries
    the user's secrets/paths INTO the subprocess; for http, an auth header is built SERVER-SIDE from
    a connection secret and lands in ``headers`` — never in the IR, never logged with values,
    never resolved in theygent cloud (the sovereignty invariant)."""

    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None  # stdio
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    url: str | None = None  # http (streamable-HTTP / SSE)
    headers: dict[str, str] | None = None  # http — incl auth, built server-side from a connection


class McpClient(Protocol):
    """The lifecycle seam. Transport-agnostic; ``StdioMcpClient`` is the stdio-only
    implementation."""

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


def _call_timeout_s() -> float:
    """Per-call ceiling for one MCP tool invocation. The actor serves calls SERIALLY off its
    queue, so a single wedged server call would otherwise block every later call — and the run
    (or an unattended trigger fire) awaiting it — forever. Generous by default (tools can be
    genuinely slow); tunable via ``THEYGENT_MCP_CALL_TIMEOUT_S``."""
    raw = os.environ.get("THEYGENT_MCP_CALL_TIMEOUT_S")
    try:
        return float(raw) if raw else 120.0
    except ValueError:
        return 120.0


class _ActorMcpClient:
    """A persistent MCP connection owned by a single background task.
    Transport-agnostic: a subclass provides :meth:`_open_transport`; everything else (the queue /
    serve / reconnect contract) is shared so stdio and http behave identically."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[_Call | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._tools: list[McpToolDescriptor] = []

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        """Open the transport on the actor task and return its ``(read, write)`` streams. Stdio
        spawns a subprocess; http opens a streamable-HTTP session. The contexts are entered on
        ``stack`` so they tear down on the SAME task (the anyio cancel-scope rule)."""
        raise NotImplementedError

    async def connect(self) -> None:
        """Open the transport and initialize the session on a dedicated task. Raises
        :class:`McpConnectionError` if the transport won't open or the handshake fails."""

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
                read, write = await self._open_transport(stack)
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
        timeout = _call_timeout_s()
        while True:
            item = await self._queue.get()
            if item is None:  # close sentinel
                return
            name, args, fut = item
            try:
                result = await asyncio.wait_for(session.call_tool(name, args), timeout=timeout)
                if not fut.done():
                    res = McpResult(value=_result_value(result), is_error=bool(result.isError))
                    fut.set_result(res)
            except TimeoutError:
                # A wedged server: fail the call AND end the task (a session whose in-flight
                # request never answered is not trustworthy for later calls) — the manager's
                # retry reconnects fresh. Without a ceiling here the serial queue wedges forever.
                if not fut.done():
                    fut.set_exception(
                        McpConnectionError(f"MCP call {name!r} timed out after {timeout:.0f}s")
                    )
                raise
            except BaseException as exc:  # incl. cancellation of the actor task (close/shutdown):
                # the item was already dequeued, so the post-loop drain can't reach this future —
                # fail it here or its caller awaits forever.
                if not fut.done():
                    fut.set_exception(
                        exc
                        if isinstance(exc, Exception)
                        else McpConnectionError("MCP connection closed")
                    )
                raise

    async def list_tools(self) -> list[McpToolDescriptor]:
        # Cached on connect for the connection lifetime (capability caching).
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


class StdioMcpClient(_ActorMcpClient):
    """A persistent stdio MCP connection — the server is a local subprocess."""

    def __init__(
        self,
        *,
        command: str,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        super().__init__()
        self._params = StdioServerParameters(
            command=command, args=list(args), env=dict(env) if env else None, cwd=cwd
        )

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        read, write = await stack.enter_async_context(stdio_client(self._params))
        return read, write


class HttpMcpClient(_ActorMcpClient):
    """A persistent streamable-HTTP / SSE MCP connection — a remote server by url.
    ``headers`` carries the auth built SERVER-SIDE from a connection secret; the secret never
    appears in the IR. Same actor machinery as stdio — only the transport differs."""

    def __init__(self, *, url: str, headers: Mapping[str, str] | None = None) -> None:
        super().__init__()
        self._url = url
        self._headers = dict(headers) if headers else None

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        # streamablehttp_client yields (read, write, get_session_id); we don't need the session-id
        # callback (the actor owns one persistent session for the connection lifetime).
        read, write, _get_session_id = await stack.enter_async_context(
            streamablehttp_client(self._url, headers=self._headers)
        )
        return read, write
