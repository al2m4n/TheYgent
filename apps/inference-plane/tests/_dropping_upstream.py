"""An engine that drops requests the way mlx_lm.server does.

mlx_lm.server is built on Python's ``http.server``: it answers HTTP/1.0, so a streamed body has no
chunked framing and ends when the server closes the connection. When its tool-call parser raises
(mlx-lm 0.31.3's qwen3_coder on a list argument that isn't JSON), the handler dies and the
connection closes — with no response at all for a non-streaming call, and after the 200 header
and keepalive comments for a stream, which then reads as a clean, empty end of stream.

This fake serves the same shapes on the same stack, so the gateway's real dispatch layer sees
real sockets closing. The last user message picks the behaviour:

* ``DROP`` — close without finishing (no response / mid-stream, as above);
* ``DROP_CHUNKED`` — a chunked HTTP/1.1 stream cut off before its last chunk, the shape of a
  framed engine (llama.cpp, uvicorn-based servers) dying mid-reply;
* ``REPLAY + name`` — a stream recorded from a real hosted server (``HOSTED_STREAMS``), sent
  whole with the chunked framing hosted APIs use; ``REPLAY_CUT + name`` sends it only up to its
  first chunk with a ``finish_reason`` and then cuts the body, the shape of that server failing
  mid-reply;
* anything else — a normal reply, streamed close-delimited like mlx_lm.server.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
from _hosted_streams import HOSTED_STREAMS
from theygent_inference_plane.launcher import EngineHandle
from theygent_ir import Capabilities, ManagedBinding

DROP = "__drop__"
DROP_CHUNKED = "__drop_chunked__"
REPLAY = "__replay__:"
REPLAY_CUT = "__replay_cut__:"
REPLY = ["hello", " world"]


def _chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
    body = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "fake-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body)}\n\n".encode()


def _first_finish(events: list[bytes]) -> int:
    """The index of the first event carrying a ``finish_reason``."""
    for i, event in enumerate(events):
        if not event.startswith(b"data: {"):
            continue
        body = json.loads(event[len(b"data: ") :])
        if any(c.get("finish_reason") for c in body.get("choices") or []):
            return i
    raise ValueError("the recorded stream has no finish_reason")


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.chat_bodies.append(body)
        prompt = body["messages"][-1]["content"]
        if body.get("stream"):
            self._stream(prompt)
        elif prompt == DROP:
            return  # the handler dies: the connection closes with no response
        else:
            self._json(
                200,
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "fake-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "".join(REPLY)},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

    def _chunked(self, events: list[bytes], *, complete: bool) -> None:
        self.protocol_version = "HTTP/1.1"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for data in events:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        if complete:
            self.wfile.write(b"0\r\n\r\n")
        # Otherwise there is no terminating zero-length chunk, and the connection closes: the
        # server died.
        self.wfile.flush()
        self.close_connection = True

    def _stream(self, prompt: str) -> None:
        if prompt.startswith(REPLAY):
            self._chunked(HOSTED_STREAMS[prompt[len(REPLAY) :]], complete=True)
            return
        if prompt.startswith(REPLAY_CUT):
            events = HOSTED_STREAMS[prompt[len(REPLAY_CUT) :]]
            self._chunked(events[: _first_finish(events)], complete=False)
            return
        if prompt == DROP_CHUNKED:
            self._chunked([_chunk({"role": "assistant", "content": REPLY[0]})], complete=False)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(b": keepalive 1/1\n\n")  # prompt processing, as mlx_lm.server reports it
        self.wfile.flush()
        if prompt == DROP:
            return  # the handler dies: the connection closes, the body just ends
        for piece in REPLY:
            self.wfile.write(_chunk({"role": "assistant", "content": piece}))
        self.wfile.write(_chunk({"role": "assistant", "content": ""}, finish="stop"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.chat_bodies: list[dict[str, Any]] = []


class DroppingUpstreamHandle:
    def __init__(self) -> None:
        self._server = _Server()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.terminated = False

    @property
    def chat_bodies(self) -> list[dict[str, Any]]:
        """Every chat request body the engine received, retries included."""
        return self._server.chat_bodies

    @property
    def exit_code(self) -> int | None:
        return None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                return (await client.get(f"{self.base_url}/health")).status_code == 200
        except httpx.HTTPError:
            return False

    async def capabilities(self) -> Capabilities:
        return Capabilities(tool_calling=True, structured_output=False, vision=False)

    async def terminate(self) -> None:
        self.terminated = True
        self._server.shutdown()
        self._server.server_close()


_: type[EngineHandle] = DroppingUpstreamHandle


class DroppingUpstreamLauncher:
    """Injected EngineLauncher whose engines are :class:`DroppingUpstreamHandle`."""

    def __init__(self) -> None:
        self.handles: list[DroppingUpstreamHandle] = []

    @property
    def ready(self) -> bool:
        return True

    @property
    def not_ready_reason(self) -> str | None:
        return None

    async def launch(self, binding: ManagedBinding) -> EngineHandle:
        handle = DroppingUpstreamHandle()
        self.handles.append(handle)
        return handle
