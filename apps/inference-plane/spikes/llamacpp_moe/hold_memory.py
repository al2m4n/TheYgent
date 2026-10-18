"""Hold N GB of memory so a model no longer fits, until terminated.

The memory is filled with random bytes, because macOS compresses zero-filled or repetitive memory
to almost nothing and that would create no pressure. Each chunk is locked with ``mlock`` where the
OS allows it; otherwise every page is touched every few seconds, so the holder stays the most
recently used memory and the OS reclaims other pages first.

    python3 hold_memory.py --gb 20 --ready-file held.txt
"""

from __future__ import annotations

import argparse
import ctypes
import os
import signal
import sys
import time

CHUNK = 512 * 1024 * 1024
PAGE = os.sysconf("SC_PAGE_SIZE")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gb", type=float, required=True)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--touch-every", type=float, default=5.0)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]

    chunks: list[bytearray] = []
    locked = 0
    remaining = int(args.gb * 1e9)
    while remaining > 0:
        size = min(CHUNK, remaining)
        chunk = bytearray(os.urandom(size))
        address = ctypes.addressof(ctypes.c_char.from_buffer(chunk))
        if libc.mlock(address, size) == 0:
            locked += size
        chunks.append(chunk)
        remaining -= size

    held = sum(len(c) for c in chunks)
    with open(args.ready_file, "w") as f:
        f.write(f"held {held / 1e9:.2f} GB, locked {locked / 1e9:.2f} GB\n")

    while True:
        if locked < held:
            for chunk in chunks:
                view = memoryview(chunk)
                for offset in range(0, len(chunk), PAGE):
                    view[offset] = view[offset]
        time.sleep(args.touch_every)


if __name__ == "__main__":
    main()
