"""A minimal OpenAI ``/v1/images/generations`` server wrapping a local image-gen CLI.

Unlike chat / vision / audio — which each had a ready OpenAI-compatible sibling server to launch
(llama-server, whisper-server, the mlx servers) — the local image generators are **CLIs with no
server** (stable-diffusion.cpp's ``sd-cli``, ``mflux-generate``). So this thin process is the
sibling OpenAI server the ``(engine, images.generation)`` launcher spawns and the gateway proxies
to: it shells out to the chosen CLI per request and returns the OpenAI images shape (base64).

Weights load per request (the CLIs are one-shot), so generation carries the model-load cost each
call. Stdlib only (no heavy deps): it reads
the produced PNG and base64-encodes it. The engine binary is resolved from the CLI (or its env
override); if missing, ``/readyz``-style startup fails loudly.

A render lives only as long as someone is waiting for it: a caller that hangs up has its render
killed (or, still queued, never started), and terminating this server kills the render in flight
rather than orphaning a CLI that holds the model in memory.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Per-engine defaults. stable-diffusion (base SD1.5) is prompt-faithful only at a real guidance
# scale (~7) and ~20 steps — a low CFG makes it mostly ignore the prompt. FLUX-schnell is a
# distilled few-step model (guidance baked in → cfg 0). Both overridable per request via ``steps``.
_ENGINE_DEFAULTS = {
    "sdcpp": {"steps": 20, "cfg": 7.0},
    "mflux": {"steps": 3, "cfg": 0.0},
}

# The per-image CLI wall-clock cap. Kept UNDER the callers' HTTP timeouts (the inference gateway and
# gateway-client both allow ~1200s) so a stuck render is killed here — surfacing a clean structured
# error — before the HTTP layer times out and drops the connection with no response.
_GENERATION_TIMEOUT_SEC = 1000

# Generation is serialized: each render forks a CLI that loads the full multi-GB model into memory,
# so N concurrent requests to this per-model server would load the weights N times and exhaust
# memory. One at a time keeps memory bounded (a single accelerator can't run two diffusions in
# parallel usefully anyway); concurrent callers queue on this lock.
_GEN_LOCK = threading.Lock()

# How often a render — or a request queued for one — checks that its caller is still connected.
# The plane closes the connection the moment its own client hangs up, so this bounds how long an
# abandoned render keeps the lock (and the accelerator) after nobody is left to read the image.
_HANGUP_POLL_SEC = 0.25


class _Renders:
    """The CLI processes rendering right now.

    An engine teardown SIGTERMs this server, and a process that dies does not take its children
    with it: an orphaned CLI keeps rendering — holding the full model in memory — for nobody,
    while the plane may already be loading another engine into that memory. Tracking the live
    renders lets the SIGTERM handler kill them before the server exits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: set[subprocess.Popen[bytes]] = set()
        self._stopping = False

    def spawn(self, cmd: list[str]) -> subprocess.Popen[bytes]:
        with self._lock:
            # Checked under the lock so a render can never start after kill_all has run.
            if self._stopping:
                raise _GenerationError("the image server is shutting down")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self._live.add(proc)
            return proc

    def discard(self, proc: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._live.discard(proc)

    def kill_all(self) -> None:
        with self._lock:
            self._stopping = True
            live = list(self._live)
        for proc in live:
            proc.kill()
            # Reaped here, so the render is gone (not a zombie) by the time the server exits.
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


_RENDERS = _Renders()


def _int_param(value: Any, default: int) -> int:
    """A request int knob (steps/n), tolerant of a missing or non-numeric value — a bad value must
    yield a clean 400, never an uncaught error that drops the connection."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise _BadRequest(f"expected an integer, got {value!r}") from exc


def _round64(n: int) -> int:
    """Diffusion models want dimensions that are multiples of 64."""
    return max(64, (n // 64) * 64)


def _parse_size(size: str | None) -> tuple[int, int]:
    if not size or "x" not in size:
        return 512, 512
    try:
        w, h = size.lower().split("x", 1)
        return _round64(int(w)), _round64(int(h))
    except ValueError:
        return 512, 512


#: Lines the image CLIs print while booting the GPU backend. They dominate the output by volume,
#: so a blind tail of stderr reports a successful Metal init as the reason a render failed.
_NOISE_PREFIXES = (
    "ggml_",
    "ggml-",
    "load_",
    "loading ",
    "system_info",
    "Option:",
    "    ",
)


def _failure_reason(output: bytes) -> str:
    """The actionable part of a failed render's output.

    A diffusion CLI writes hundreds of device-capability lines before the one that says what went
    wrong (``[ERROR] … get sd version from file failed``). Prefer the explicit error lines; fall
    back to the last non-boilerplate lines, and only then to a raw tail — so the caller is never
    handed a GPU feature list as an explanation.
    """
    text = output.decode("utf-8", "replace").strip()
    if not text:
        return "no output"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    flagged = [ln for ln in lines if "error" in ln.lower() or "fail" in ln.lower()]
    if flagged:
        return " | ".join(flagged[-3:])[:600]
    meaningful = [ln for ln in lines if not ln.startswith(_NOISE_PREFIXES)]
    return " | ".join((meaningful or lines)[-3:])[:600]


def _build_command(
    engine: str, binary: str, model: str, prompt: str, out: str, req: dict
) -> list[str]:
    d = _ENGINE_DEFAULTS[engine]
    width, height = _parse_size(req.get("size"))
    steps = _int_param(req.get("steps"), d["steps"])
    if engine == "sdcpp":
        return [
            binary,
            "-m",
            model,
            "-p",
            prompt,
            "-o",
            out,
            "--steps",
            str(steps),
            "-W",
            str(width),
            "-H",
            str(height),
            "--cfg-scale",
            str(d["cfg"]),
            "--sampling-method",
            "euler_a",
        ]
    # mflux — model may be a base-model name ("schnell"/"dev"), an hf repo, or a local path.
    return [
        binary,
        "--model",
        model,
        "-q",
        "4",
        "--steps",
        str(steps),
        "--width",
        str(width),
        "--height",
        str(height),
        "--prompt",
        prompt,
        "--output",
        out,
    ]


class _Handler(BaseHTTPRequestHandler):
    engine: str = "sdcpp"
    binary: str = "sd-cli"
    model: str = ""

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path in ("/health", "/healthz"):
            self._json(200, {"status": "ok"})
        elif self.path.rstrip("/") == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": self.model, "object": "model"}]})
        else:
            self._json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/images/generations":
            self._json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": {"message": "invalid json body"}})
            return
        prompt = (req.get("prompt") or "").strip()
        if not prompt:
            self._json(400, {"error": {"message": "prompt is required", "code": "prompt_required"}})
            return
        try:
            n = max(1, min(_int_param(req.get("n"), 1), 4))
            images = [{"b64_json": self._generate(prompt, req)} for _ in range(n)]
        except _CallerGone:
            # Nobody is left to answer, and the render it was waiting on is already stopped.
            self.close_connection = True
            return
        except _BadRequest as exc:
            self._json(
                400,
                {
                    "error": {
                        "message": str(exc),
                        "type": "invalid_request_error",
                        "code": "invalid_request",
                    }
                },
            )
            return
        except _GenerationError as exc:
            self._json(
                502,
                {
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": "image_generation_failed",
                    }
                },
            )
            return
        self._json(200, {"created": int(time.time()), "data": images})

    def _generate(self, prompt: str, req: dict) -> str:
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "image.png")
            cmd = _build_command(self.engine, self.binary, self.model, prompt, out, req)
            # Serialize + bound the render: one model-loading CLI at a time (memory), and a
            # wall-clock cap under the callers' HTTP timeout so a stuck render fails cleanly,
            # never a dropped connection. A caller that hangs up while queued gives its turn up
            # without ever starting a render.
            while not _GEN_LOCK.acquire(timeout=_HANGUP_POLL_SEC):
                if self._caller_gone():
                    raise _CallerGone
            try:
                returncode, output = self._render(cmd)
            finally:
                _GEN_LOCK.release()
            if returncode != 0 or not os.path.exists(out):
                raise _GenerationError(
                    f"{self.engine} failed to generate: {_failure_reason(output)}"
                )
            with open(out, "rb") as f:
                return base64.b64encode(f.read()).decode()

    def _render(self, cmd: list[str]) -> tuple[int, bytes]:
        """Run the CLI to completion: its exit code and its combined stderr + stdout.

        Killed early if the caller hangs up (``_CallerGone``) or the wall-clock cap passes
        (``_GenerationError``) — an abandoned render otherwise holds the lock, and every caller
        queued behind it, for minutes."""
        if self._caller_gone():
            raise _CallerGone
        proc = _RENDERS.spawn(cmd)
        deadline = time.monotonic() + _GENERATION_TIMEOUT_SEC
        with proc:
            try:
                while True:
                    try:
                        stdout, stderr = proc.communicate(timeout=_HANGUP_POLL_SEC)
                    except subprocess.TimeoutExpired:
                        if self._caller_gone():
                            raise _CallerGone from None
                        if time.monotonic() >= deadline:
                            raise _GenerationError(
                                f"{self.engine} timed out after {_GENERATION_TIMEOUT_SEC}s"
                            ) from None
                    else:
                        return proc.returncode, stderr + b"\n" + stdout
            finally:
                proc.kill()  # a no-op once the CLI has exited
                _RENDERS.discard(proc)

    def _caller_gone(self) -> bool:
        """Whether the caller has closed its connection.

        The request body is fully read before any render starts, and an HTTP client sends
        nothing more while it waits for the answer, so the socket turning readable means the
        peer closed it (end of stream, or a reset)."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def log_message(self, *args: Any) -> None:  # keep the plane's logs clean
        pass


class _GenerationError(RuntimeError):
    pass


class _CallerGone(Exception):
    """The caller hung up before its image was rendered."""


class _BadRequest(ValueError):
    pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True, choices=["sdcpp", "mflux"])
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--bin", default=None, help="the engine CLI (else resolved from PATH)")
    args = parser.parse_args()

    default_bin = "sd-cli" if args.engine == "sdcpp" else "mflux-generate"
    binary = (
        args.bin
        or os.environ.get("THEYGENT_SDCPP_BIN" if args.engine == "sdcpp" else "THEYGENT_MFLUX_BIN")
        or shutil.which(default_bin)
    )
    if not binary:
        raise SystemExit(f"{default_bin} not found on PATH (needed for {args.engine} image gen)")

    _Handler.engine = args.engine
    _Handler.binary = binary
    _Handler.model = args.model

    def _terminate(signum: int, _frame: object) -> None:
        # The plane tears an engine down with SIGTERM. Exiting without killing the render first
        # would orphan its CLI (see _Renders).
        _RENDERS.kill_all()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _terminate)
    ThreadingHTTPServer((args.host, args.port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
