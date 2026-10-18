"""Measure llama.cpp's built-in mixture-of-experts offload (``--cpu-moe``) on this machine.

Starts ``llama-server`` on a GGUF model with and without ``--cpu-moe``, runs one fixed prompt at
temperature 0, and records decode and prompt speed, the server's memory over the run (resident
size and macOS physical footprint, sampled), how much of the model file the OS file cache holds
afterwards, and the generated text, so two runs can be compared for identical output.
Standard library only.

    python3 measure.py residency --model <file.gguf> [--evict]
    python3 measure.py run --model <file.gguf> --label gpu --out gpu.json
    python3 measure.py run --model <file.gguf> --label cpu-moe --cpu-moe --evict --out cold.json
    python3 measure.py compare gpu.json cold.json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PROMPT = "Explain how a mixture-of-experts model routes tokens, in three short paragraphs."

# ── the OS file cache ─────────────────────────────────────────────────────────────────────────


def cached_fraction(path: str, evict: bool = False) -> float:
    """Fraction of ``path``'s pages in the OS file cache (``mincore`` on a shared map); ``evict``
    first asks the kernel to drop the file's clean cached pages (``MS_INVALIDATE``)."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_longlong,
    ]
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    libc.msync.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    size = os.path.getsize(path)
    page = os.sysconf("SC_PAGE_SIZE")
    fd = os.open(path, os.O_RDONLY)
    try:
        prot_read, map_shared, ms_invalidate = 0x1, 0x1, 0x2
        addr = libc.mmap(None, size, prot_read, map_shared, fd, 0)
        if addr in (None, ctypes.c_void_p(-1).value):
            raise OSError(ctypes.get_errno(), f"mmap failed for {path}")
        try:
            if evict and libc.msync(addr, size, ms_invalidate) != 0:
                raise OSError(ctypes.get_errno(), f"msync failed for {path}")
            pages = (size + page - 1) // page
            vec = ctypes.create_string_buffer(pages)
            if libc.mincore(addr, size, vec) != 0:
                raise OSError(ctypes.get_errno(), f"mincore failed for {path}")
            return sum(1 for b in vec.raw if b & 1) / pages
        finally:
            libc.munmap(addr, size)
    finally:
        os.close(fd)


# ── memory sampling ───────────────────────────────────────────────────────────────────────────

_UNITS = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9}


def _footprint_bytes(pid: int) -> int | None:
    """macOS physical footprint (dirty + compressed + GPU-wired memory; clean file-backed pages
    the OS can drop are not in it)."""
    if not shutil.which("footprint"):
        return None
    out = subprocess.run(
        ["footprint", "-p", str(pid)], capture_output=True, text=True, env={"PATH": "/usr/bin"}
    ).stdout
    match = re.search(r"Footprint:\s*([\d.]+)\s*(KB|MB|GB|B)", out)
    return int(float(match.group(1)) * _UNITS[match.group(2)]) if match else None


def _rss_bytes(pid: int) -> int | None:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return int(out.stdout.strip()) * 1024 if out.stdout.strip() else None


class MemorySampler(threading.Thread):
    def __init__(self, pid: int, every: float = 0.5):
        super().__init__(daemon=True)
        self.pid, self.every = pid, every
        self.peak_rss = 0
        self.peak_footprint = 0
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            self.peak_rss = max(self.peak_rss, _rss_bytes(self.pid) or 0)
            self.peak_footprint = max(self.peak_footprint, _footprint_bytes(self.pid) or 0)
            self._stop.wait(self.every)

    def stop(self) -> None:
        self._stop.set()
        self.join()


# ── one run ───────────────────────────────────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _wait_healthy(base: str, proc: subprocess.Popen, timeout: float) -> float:
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server exited early with code {proc.returncode}")
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=2) as resp:
                if resp.status == 200:
                    return time.perf_counter() - start
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.5)
    raise TimeoutError(f"llama-server not healthy within {timeout}s")


def cmd_run(args) -> None:
    model = str(Path(args.model).expanduser())
    binary = args.bin or shutil.which("llama-server")
    if not binary:
        raise SystemExit("llama-server not found")
    if args.evict:
        cached_fraction(model, evict=True)
    cached_before = cached_fraction(model)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    cmd = [binary, "-m", model, "--host", "127.0.0.1", "--port", str(port)]
    # Verbosity 4 makes llama-server log each backend's model buffer size (the CPU/GPU split).
    cmd += ["-c", str(args.ctx), "-np", "1", "-lv", "4"]
    if args.cpu_moe:
        cmd.append("--cpu-moe")
    if args.n_cpu_moe is not None:
        cmd += ["--n-cpu-moe", str(args.n_cpu_moe)]
    cmd += args.extra
    log_path = Path(args.out).with_suffix(".log") if args.out else Path(os.devnull)
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        sampler = MemorySampler(proc.pid)
        sampler.start()
        try:
            load_seconds = _wait_healthy(base, proc, args.startup_timeout)
            started = time.perf_counter()
            body = _post(
                f"{base}/v1/chat/completions",
                {
                    "messages": [{"role": "user", "content": args.prompt}],
                    "max_tokens": args.max_tokens,
                    "temperature": 0,
                    "seed": 0,
                    "cache_prompt": False,
                },
                timeout=args.request_timeout,
            )
            wall = time.perf_counter() - started
        finally:
            sampler.stop()
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()

    message = body["choices"][0]["message"]
    timings = body.get("timings", {})
    log_text = log_path.read_text(errors="replace") if args.out else ""
    buffers = re.findall(r"(\S+) model buffer size\s*=\s*([\d.]+) MiB", log_text)
    result = {
        "label": args.label,
        "model": model,
        "flags": cmd[cmd.index("-lv") + 2 :],
        "file_gb": round(os.path.getsize(model) / 1e9, 2),
        "cached_before": round(cached_before, 3),
        "cached_after": round(cached_fraction(model), 3),
        "load_seconds": round(load_seconds, 1),
        "request_seconds": round(wall, 1),
        "prompt_tokens": timings.get("prompt_n"),
        "prompt_tps": round(timings.get("prompt_per_second") or 0, 2),
        "decode_tokens": timings.get("predicted_n"),
        "decode_tps": round(timings.get("predicted_per_second") or 0, 2),
        "peak_rss_gb": round(sampler.peak_rss / 1e9, 2),
        "peak_footprint_gb": round(sampler.peak_footprint / 1e9, 2),
        "model_buffers_mib": {name: float(size) for name, size in buffers},
        "text": (message.get("reasoning_content") or "") + (message.get("content") or ""),
    }
    print(json.dumps({k: v for k, v in result.items() if k != "text"}, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))


def cmd_compare(args) -> None:
    a = json.loads(Path(args.a).read_text())["text"]
    b = json.loads(Path(args.b).read_text())["text"]
    if a == b:
        print(f"IDENTICAL: {len(a)} characters")
        return
    first = next(
        (i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b))
    )
    print(f"DIFFER at character {first} of {len(a)} / {len(b)}")
    print(f"  a: …{a[max(0, first - 60) : first + 60]!r}")
    print(f"  b: …{b[max(0, first - 60) : first + 60]!r}")
    sys.exit(1)


def cmd_residency(args) -> None:
    model = str(Path(args.model).expanduser())
    size = os.path.getsize(model) / 1e9
    print(f"{cached_fraction(model, evict=args.evict):7.1%} cached  {size:6.2f} GB  {model}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="one llama-server run")
    run.add_argument("--model", required=True)
    run.add_argument("--label", required=True)
    run.add_argument("--bin", help="llama-server path (default: PATH)")
    run.add_argument("--cpu-moe", action="store_true")
    run.add_argument("--n-cpu-moe", type=int)
    run.add_argument("--evict", action="store_true", help="drop the model file from the cache")
    run.add_argument(
        "--extra",
        action="append",
        default=[],
        help="one further llama-server flag per use, e.g. --extra=--no-warmup",
    )
    run.add_argument("--ctx", type=int, default=4096)
    run.add_argument("--max-tokens", type=int, default=128)
    run.add_argument("--prompt", default=PROMPT)
    run.add_argument("--startup-timeout", type=float, default=600)
    run.add_argument("--request-timeout", type=float, default=1800)
    run.add_argument("--out")
    run.set_defaults(func=cmd_run)

    compare = sub.add_parser("compare", help="check two runs generated the same text")
    compare.add_argument("a")
    compare.add_argument("b")
    compare.set_defaults(func=cmd_compare)

    residency = sub.add_parser("residency", help="how much of a model file is cached")
    residency.add_argument("--model", required=True)
    residency.add_argument("--evict", action="store_true")
    residency.set_defaults(func=cmd_residency)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
