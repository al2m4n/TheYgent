"""Expert-streaming spike for MLX mixture-of-experts models.

Every non-expert weight (attention, embeddings, router, shared experts) stays resident; the routed
expert weights are never loaded. Each MoE layer waits for its router's top-k choice, then reads
only the chosen experts' bytes from the safetensors files (each expert is one contiguous slice of
its stacked tensor) and runs mlx's own gather kernels on that compact set. The resident footprint
is the model's dense part plus an optional bounded expert cache; with no cache, the OS page cache
is the only cache.

Runs under the Python that mlx_lm is installed in, not the inference plane's environment:

    PY=~/.local/share/uv/tools/mlx-lm/bin/python
    $PY -I spike.py selftest
    $PY -I spike.py run --model <path> --mode resident --out resident.json
    $PY -I spike.py run --model <path> --mode stream --cache-gb 0 --out stream.json
    $PY -I spike.py compare resident.json stream.json

    # expert-major records: one read per expert miss instead of one per tensor
    $PY -I spike.py repack --model <path> --dest <dir>
    $PY -I spike.py run --model <path> --mode stream --repacked <dir> --out repacked.json

Pre-converted MLX checkpoints only: the expert tensors the model holds must be stored verbatim in
the files (a checkpoint whose sanitize step reshapes experts fails loudly).
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import re
import resource
import struct
import sys
import tempfile
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import mlx.core as mx
import mlx.nn as nn
import numpy as np
from mlx.utils import tree_flatten
from mlx_lm.models.switch_layers import SwitchGLU, SwitchMLP, _gather_sort, _scatter_unsort
from mlx_lm.utils import load_model, load_tokenizer

GLU_PROJECTIONS = ("gate_proj", "up_proj", "down_proj")

# safetensors dtype -> (numpy dtype the bytes are read as, mlx dtype to reinterpret them as).
# numpy has no bfloat16, so bf16 bytes are read as uint16 and viewed as bfloat16 in mlx.
_DTYPES: dict[str, tuple[type, mx.Dtype | None]] = {
    "U32": (np.uint32, None),
    "U8": (np.uint8, None),
    "F32": (np.float32, None),
    "F16": (np.float16, None),
    "BF16": (np.uint16, mx.bfloat16),
}
_MX_DTYPE_NAMES = {
    mx.uint32: "U32",
    mx.uint8: "U8",
    mx.float32: "F32",
    mx.float16: "F16",
    mx.bfloat16: "BF16",
}
_NAMED_MX_DTYPES = {name: dtype for dtype, name in _MX_DTYPE_NAMES.items()}


class StreamingError(RuntimeError):
    """The checkpoint cannot be expert-streamed as stored."""


# ── safetensors index ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StoredTensor:
    path: str
    offset: int  # absolute file offset of the tensor's first byte
    dtype: str
    shape: tuple[int, ...]


def index_safetensors(model_path: Path) -> dict[str, StoredTensor]:
    """Every tensor stored in the checkpoint, by name, with its absolute byte offset."""
    files = sorted(glob.glob(str(model_path / "model*.safetensors")))
    if not files:
        raise StreamingError(f"no model*.safetensors under {model_path}")
    index: dict[str, StoredTensor] = {}
    for path in files:
        with open(path, "rb") as f:
            (header_len,) = struct.unpack("<Q", f.read(8))
            header = json.loads(f.read(header_len))
        header.pop("__metadata__", None)
        data_start = 8 + header_len
        for name, entry in header.items():
            start, _end = entry["data_offsets"]
            index[name] = StoredTensor(
                path=path,
                offset=data_start + start,
                dtype=entry["dtype"],
                shape=tuple(entry["shape"]),
            )
    return index


# ── expert reads + cache ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExpertTensor:
    """One stacked expert tensor ([num_experts, ...]) read one expert range at a time."""

    fd: int
    offset: int
    expert_nbytes: int
    expert_shape: tuple[int, ...]
    np_dtype: type
    mx_view: mx.Dtype | None
    mx_dtype: mx.Dtype

    def read_host(self, first: int, count: int) -> np.ndarray:
        """Read ``count`` experts starting at ``first`` into host memory. Pure I/O with no mlx
        calls, so it is safe on a reader thread: mlx streams belong to the thread that made
        them, and generation runs on a stream the reader threads do not have."""
        buf = _pread_exact(
            self.fd, self.expert_nbytes * count, self.offset + first * self.expert_nbytes
        )
        return np.frombuffer(buf, dtype=self.np_dtype).reshape((count, *self.expert_shape))

    def to_mx(self, host: np.ndarray) -> mx.array:
        arr = mx.array(host)
        return arr.view(self.mx_view) if self.mx_view is not None else arr


def _pread_exact(fd: int, nbytes: int, offset: int) -> bytearray:
    buf = bytearray(nbytes)
    view = memoryview(buf)
    done = 0
    while done < nbytes:
        got = os.preadv(fd, [view[done:]], offset + done)
        if got <= 0:
            raise StreamingError(f"short read: {done} of {nbytes} bytes at offset {offset}")
        done += got
    return buf


# ── expert-major records ─────────────────────────────────────────────────────────────────────

#: Records start on a page boundary, so a run of whole records is a page-aligned read.
RECORD_ALIGN = 16384
#: Each field inside a record starts 64-byte aligned.
FIELD_ALIGN = 64
#: The most records one read covers; longer runs split across the reader threads.
MAX_RECORDS_PER_READ = 8


def _round_up(n: int, align: int) -> int:
    return (n + align - 1) // align * align


@dataclass(frozen=True)
class RecordField:
    name: str  # "gate_proj.weight"
    offset: int  # within the record
    nbytes: int
    expert_shape: tuple[int, ...]
    np_dtype: type


@dataclass(frozen=True)
class ExpertRecords:
    """One layer's experts stored expert-major: each expert's tensors back to back in one
    fixed-size record, so reading an expert is one contiguous read instead of one per tensor."""

    fd: int
    base: int
    stride: int
    fields: tuple[RecordField, ...]

    def read_host(self, first: int, count: int) -> dict[str, np.ndarray]:
        """Pure I/O plus host copies, safe on a reader thread (see ``ExpertTensor.read_host``)."""
        buf = _pread_exact(self.fd, self.stride * count, self.base + first * self.stride)
        raw = np.frombuffer(buf, dtype=np.uint8).reshape(count, self.stride)
        return {
            f.name: np.ascontiguousarray(raw[:, f.offset : f.offset + f.nbytes])
            .view(f.np_dtype)
            .reshape((count, *f.expert_shape))
            for f in self.fields
        }


@dataclass
class Stats:
    moe_calls: int = 0
    experts_requested: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    read_bytes: int = 0
    read_seconds: float = 0.0
    route_wait_seconds: float = 0.0
    streamed_layers: int = 0
    pinned_layers: int = 0
    bank_slots: int = 0

    def reset_counters(self) -> None:
        self.moe_calls = self.experts_requested = self.cache_hits = self.cache_misses = 0
        self.read_bytes = 0
        self.read_seconds = self.route_wait_seconds = 0.0

    def as_dict(self) -> dict:
        return {
            "streamed_layers": self.streamed_layers,
            "pinned_layers": self.pinned_layers,
            "bank_slots_per_layer": self.bank_slots,
            "moe_calls": self.moe_calls,
            "experts_requested": self.experts_requested,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "hit_rate": (self.cache_hits / self.experts_requested) if self.experts_requested else 0,
            "read_gb": self.read_bytes / 1e9,
            "read_seconds": round(self.read_seconds, 3),
            "read_gb_per_s": (self.read_bytes / 1e9 / self.read_seconds)
            if self.read_seconds
            else 0,
            "route_wait_seconds": round(self.route_wait_seconds, 3),
        }


@dataclass
class ExpertCache:
    """LRU of single-expert arrays across all layers, bounded in bytes. A zero budget disables
    it, leaving the OS page cache as the only cache."""

    budget_bytes: int
    entries: OrderedDict = field(default_factory=OrderedDict)
    used_bytes: int = 0

    def get(self, key):
        value = self.entries.get(key)
        if value is not None:
            self.entries.move_to_end(key)
        return value

    def put(self, key, value: dict[str, mx.array], nbytes: int) -> None:
        if self.budget_bytes <= 0 or nbytes > self.budget_bytes:
            return
        while self.used_bytes + nbytes > self.budget_bytes:
            _, (_, evicted_bytes) = self.entries.popitem(last=False)
            self.used_bytes -= evicted_bytes
        self.entries[key] = (value, nbytes)
        self.used_bytes += nbytes


class LayerExperts:
    """The routed experts of one MoE layer: every (projection, parameter) tensor, readable by
    expert id."""

    def __init__(
        self,
        layer_key: str,
        tensors: dict[str, ExpertTensor],
        cache: ExpertCache,
        pool: ThreadPoolExecutor,
        stats: Stats,
        records: ExpertRecords | None = None,
        num_experts: int = 0,
    ):
        self.layer_key = layer_key
        self.num_experts = num_experts
        self.bank: SlotBank | None = None
        self.tensors = tensors  # "gate_proj.weight" -> ExpertTensor
        self.expert_nbytes = sum(t.expert_nbytes for t in tensors.values())
        self.cache = cache
        self.pool = pool
        self.stats = stats
        # When set, experts are read from the expert-major records; ``tensors`` still describes
        # each tensor's dtype and shape.
        self.records = records

    def prewarm(self) -> None:
        """Fill the bank or the cache before generation, so a run measures hits only."""
        everything = np.arange(self.num_experts)
        if self.bank is not None:
            self.bank.resolve(everything[: self.bank.slots])
        elif self.cache.budget_bytes > 0:
            self.fetch(everything)

    def fetch(self, experts: np.ndarray) -> dict[str, mx.array]:
        """Compact [len(experts), ...] arrays for every tensor, rows in ``experts`` order."""
        self.stats.experts_requested += len(experts)
        if self.cache.budget_bytes <= 0:
            self.stats.cache_misses += len(experts)
            return self._read_runs(experts)

        rows: list[dict[str, mx.array] | None] = []
        missing: list[int] = []
        for e in experts.tolist():
            hit = self.cache.get((self.layer_key, e))
            rows.append(hit[0] if hit is not None else None)
            if hit is None:
                missing.append(e)
        self.stats.cache_hits += len(experts) - len(missing)
        self.stats.cache_misses += len(missing)
        if missing:
            fetched = self._read_runs(np.array(missing))
            for i, e in enumerate(missing):
                single = {name: arr[i : i + 1] for name, arr in fetched.items()}
                # Materialize each cached expert in its own buffer, so evicting it frees memory
                # instead of pinning the run it was read in.
                single = {name: mx.contiguous(arr) for name, arr in single.items()}
                mx.eval(list(single.values()))
                self.cache.put((self.layer_key, e), single, self.expert_nbytes)
                rows[experts.tolist().index(e)] = single
        return {name: mx.concatenate([row[name] for row in rows], axis=0) for name in self.tensors}

    def _read_runs(self, experts: np.ndarray) -> dict[str, mx.array]:
        """Read sorted expert ids as coalesced contiguous runs: one read per run of records, or
        one read per (tensor, run) from the checkpoint's own stacked tensors."""
        runs: list[tuple[int, int]] = []
        for e in experts.tolist():
            if runs and runs[-1][0] + runs[-1][1] == e:
                runs[-1] = (runs[-1][0], runs[-1][1] + 1)
            else:
                runs.append((e, 1))
        if self.records is not None:
            return self._read_record_runs(runs)
        start = time.perf_counter()
        jobs = {
            (name, i): self.pool.submit(tensor.read_host, first, count)
            for name, tensor in self.tensors.items()
            for i, (first, count) in enumerate(runs)
        }
        hosts = {key: job.result() for key, job in jobs.items()}
        self.stats.read_seconds += time.perf_counter() - start
        self.stats.read_bytes += sum(h.nbytes for h in hosts.values())

        out = {}
        for name, tensor in self.tensors.items():
            parts = [tensor.to_mx(hosts[(name, i)]) for i in range(len(runs))]
            out[name] = parts[0] if len(parts) == 1 else mx.concatenate(parts, axis=0)
        return out

    def _read_record_runs(self, runs: list[tuple[int, int]]) -> dict[str, mx.array]:
        records = self.records
        assert records is not None
        chunks = [
            (first + i, min(MAX_RECORDS_PER_READ, count - i))
            for first, count in runs
            for i in range(0, count, MAX_RECORDS_PER_READ)
        ]
        start = time.perf_counter()
        jobs = [self.pool.submit(records.read_host, first, count) for first, count in chunks]
        hosts = [job.result() for job in jobs]
        self.stats.read_seconds += time.perf_counter() - start
        self.stats.read_bytes += records.stride * sum(count for _, count in chunks)

        out = {}
        for name, tensor in self.tensors.items():
            parts = [tensor.to_mx(host[name]) for host in hosts]
            out[name] = parts[0] if len(parts) == 1 else mx.concatenate(parts, axis=0)
        return out


def _slot_runs(slots: list[int]) -> list[tuple[int, int, int]]:
    """(first slot, first row, count) runs where both slots and rows are consecutive."""
    runs: list[tuple[int, int, int]] = []
    for row, slot in enumerate(slots):
        if runs and runs[-1][0] + runs[-1][2] == slot and runs[-1][1] + runs[-1][2] == row:
            runs[-1] = (runs[-1][0], runs[-1][1], runs[-1][2] + 1)
        else:
            runs.append((slot, row, 1))
    return runs


class SlotBank:
    """A fixed number of GPU-resident expert slots for one layer: preallocated
    ``[slots, ...]`` arrays per tensor, filled on a miss, evicted least-recently-used. A hit costs
    nothing beyond the gather kernels reading the bank in place: no copy, no per-expert arrays.

    A call that routes to more distinct experts than there are slots (a long prefill chunk) is
    served from a compact set (bank rows for hits, disk for misses) and leaves the bank as is."""

    def __init__(self, experts: LayerExperts, slots: int):
        self.experts = experts
        self.slots = slots
        self.arrays = {
            name: mx.zeros((slots, *t.expert_shape), dtype=t.mx_dtype)
            for name, t in experts.tensors.items()
        }
        mx.eval(list(self.arrays.values()))
        self.slot_of: dict[int, int] = {}
        self.recency: OrderedDict[int, None] = OrderedDict()
        self.free = list(range(slots - 1, -1, -1))

    def resolve(self, experts: np.ndarray) -> tuple[dict[str, mx.array], np.ndarray]:
        """Weights to gather from, and each of ``experts``' row in them (``experts`` is sorted
        and unique)."""
        stats = self.experts.stats
        ids = experts.tolist()
        missing = [e for e in ids if e not in self.slot_of]
        stats.experts_requested += len(ids)
        stats.cache_hits += len(ids) - len(missing)
        stats.cache_misses += len(missing)
        if len(ids) > self.slots:
            return self._overflow(ids, missing)

        if missing:
            in_use = set(ids)
            victims = []
            for e in missing:
                if self.free:
                    slot = self.free.pop()
                else:
                    victim = next(v for v in self.recency if v not in in_use)
                    del self.recency[victim]
                    slot = self.slot_of.pop(victim)
                victims.append(slot)
                self.slot_of[e] = slot
            fetched = self.experts._read_runs(np.array(missing))
            # Slot writes are slice assignments, grouped into runs of consecutive slots. A slice
            # assignment updates the bank's buffer in place (every earlier read of this bank was
            # evaluated at an earlier layer's host sync); an array-indexed assignment is a scatter
            # that copies the whole bank on every miss.
            for slot, row, count in _slot_runs(victims):
                for name, bank in self.arrays.items():
                    bank[slot : slot + count] = fetched[name][row : row + count]
        for e in ids:
            self.recency[e] = None
            self.recency.move_to_end(e)
        return self.arrays, np.array([self.slot_of[e] for e in ids], dtype=np.uint32)

    def _overflow(self, ids: list[int], missing: list[int]):
        hits = [e for e in ids if e in self.slot_of]
        fetched = self.experts._read_runs(np.array(missing)) if missing else {}
        hit_rows = mx.array(np.array([self.slot_of[e] for e in hits], dtype=np.uint32))
        out = {}
        for name, bank in self.arrays.items():
            parts = ([mx.take(bank, hit_rows, axis=0)] if hits else []) + (
                [fetched[name]] if missing else []
            )
            out[name] = parts[0] if len(parts) == 1 else mx.concatenate(parts, axis=0)
        for e in hits:
            self.recency.move_to_end(e)
        row = {e: i for i, e in enumerate(hits + missing)}
        return out, np.array([row[e] for e in ids], dtype=np.uint32)


# ── the streaming MoE module ─────────────────────────────────────────────────────────────────


class StreamingSwitchGLU(nn.Module):
    """Drop-in for ``SwitchGLU`` that holds no expert weights. It computes exactly what
    ``SwitchGLU`` computes, on a compact weight set of just the routed experts or on the layer's
    slot bank."""

    def __init__(self, source: SwitchGLU, experts: LayerExperts):
        super().__init__()
        self.activation = source.activation
        self._experts = experts
        self._quant = {}
        for proj in GLU_PROJECTIONS:
            linear = getattr(source, proj)
            if "scales" in linear:
                self._quant[proj] = (linear.group_size, linear.bits, linear.mode)
        self._has_bias = "gate_proj.bias" in experts.tensors

    def _linear(self, proj: str, w: dict[str, mx.array], x, idx, sorted_indices: bool):
        if proj in self._quant:
            group_size, bits, mode = self._quant[proj]
            x = mx.gather_qmm(
                x,
                w[f"{proj}.weight"],
                w[f"{proj}.scales"],
                w.get(f"{proj}.biases"),
                rhs_indices=idx,
                transpose=True,
                group_size=group_size,
                bits=bits,
                mode=mode,
                sorted_indices=sorted_indices,
            )
        else:
            x = mx.gather_mm(
                x,
                w[f"{proj}.weight"].swapaxes(-1, -2),
                rhs_indices=idx,
                sorted_indices=sorted_indices,
            )
        if self._has_bias:
            x = x + mx.expand_dims(w[f"{proj}.bias"][idx], -2)
        return x

    def __call__(self, x, indices) -> mx.array:
        stats = self._experts.stats
        stats.moe_calls += 1
        start = time.perf_counter()
        # The one host sync per MoE layer: the routing decision must be known before the bytes
        # it selects can be read.
        routed = np.array(indices)
        stats.route_wait_seconds += time.perf_counter() - start

        # np.unique sorts, so the remapped ids keep the routed ids' order and the sorted-indices
        # fast path below stays valid.
        experts, local = np.unique(routed.ravel(), return_inverse=True)
        bank = self._experts.bank
        if bank is not None:
            w, rows = bank.resolve(experts)
            idx = mx.array(rows[local].reshape(routed.shape))
        else:
            idx = mx.array(local.reshape(routed.shape).astype(np.uint32))
            w = self._experts.fetch(experts)

        x = mx.expand_dims(x, (-2, -3))
        do_sort = indices.size >= 64
        inv_order = None
        if do_sort:
            x, idx, inv_order = _gather_sort(x, idx)
        x_up = self._linear("up_proj", w, x, idx, do_sort)
        x_gate = self._linear("gate_proj", w, x, idx, do_sort)
        x = self._linear("down_proj", w, self.activation(x_up, x_gate), idx, do_sort)
        if do_sort:
            x = _scatter_unsort(x, inv_order, indices.shape)
        return x.squeeze(-2)


# ── loading ──────────────────────────────────────────────────────────────────────────────────


def _parent_and_attr(model: nn.Module, dotted: str):
    parts = dotted.split(".")
    obj = model
    for part in parts[:-1]:
        obj = obj[int(part)] if part.isdigit() else obj[part]
    return obj, parts[-1]


def _moe_layers(model: nn.Module, stored: dict[str, StoredTensor]):
    """Every ``SwitchGLU`` in ``model`` with its expert tensors as stored, checked to be the
    exact arrays the model holds. Yields (module name, module, {"gate_proj.weight": tensor})."""
    for name, module in model.named_modules():
        if isinstance(module, SwitchMLP):
            raise StreamingError(f"{name}: SwitchMLP experts are not streamed by this spike")
        if not isinstance(module, SwitchGLU):
            continue
        tensors: dict[str, StoredTensor] = {}
        for proj in GLU_PROJECTIONS:
            for param, held in tree_flatten(getattr(module, proj).parameters()):
                key = f"{name}.{proj}.{param}"
                entry = stored.get(key)
                if entry is None:
                    raise StreamingError(
                        f"{key} is not stored verbatim in the checkpoint (the model derives it "
                        "while loading); convert the checkpoint with mlx_lm.convert first"
                    )
                if entry.shape != tuple(held.shape) or entry.dtype != _MX_DTYPE_NAMES.get(
                    held.dtype
                ):
                    raise StreamingError(
                        f"{key}: stored {entry.dtype}{list(entry.shape)} differs from the model's "
                        f"{held.dtype}{list(held.shape)}; the model transforms it while loading"
                    )
                tensors[f"{proj}.{param}"] = entry
        yield name, module, tensors


def _expert_nbytes(entry: StoredTensor) -> int:
    return int(np.prod(entry.shape[1:])) * np.dtype(_DTYPES[entry.dtype][0]).itemsize


def _record_layout(tensors: dict[str, StoredTensor]) -> tuple[list[dict], int]:
    """Field offsets inside one expert record, and the record stride."""
    fields, offset = [], 0
    for name, entry in tensors.items():
        nbytes = _expert_nbytes(entry)
        fields.append(
            {
                "name": name,
                "offset": offset,
                "nbytes": nbytes,
                "dtype": entry.dtype,
                "shape": list(entry.shape[1:]),
            }
        )
        offset = _round_up(offset + nbytes, FIELD_ALIGN)
    return fields, _round_up(offset, RECORD_ALIGN)


def repack_experts(model_path: Path, dest: Path) -> None:
    """Write every MoE layer's experts expert-major to ``dest/experts.bin`` with its layout in
    ``dest/experts.json``. Reads and writes bypass the OS file cache, so repacking does not
    evict other programs' cached files."""
    model, _ = load_model(model_path, lazy=True)
    stored = index_safetensors(model_path)
    dest.mkdir(parents=True, exist_ok=True)
    bin_path = dest / "experts.bin"
    out_fd = os.open(bin_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    fcntl.fcntl(out_fd, fcntl.F_NOCACHE, 1)
    src_fds: dict[str, int] = {}
    layers: dict[str, dict] = {}
    base = 0
    try:
        for name, _module, tensors in _moe_layers(model, stored):
            fields, stride = _record_layout(tensors)
            num_experts = {entry.shape[0] for entry in tensors.values()}
            if len(num_experts) != 1:
                raise StreamingError(f"{name}: tensors disagree on the expert count")
            (count,) = num_experts
            records = np.zeros((count, stride), dtype=np.uint8)
            for spec in fields:
                entry = tensors[spec["name"]]
                if entry.path not in src_fds:
                    src_fds[entry.path] = os.open(entry.path, os.O_RDONLY)
                    fcntl.fcntl(src_fds[entry.path], fcntl.F_NOCACHE, 1)
                raw = _pread_exact(src_fds[entry.path], spec["nbytes"] * count, entry.offset)
                records[:, spec["offset"] : spec["offset"] + spec["nbytes"]] = np.frombuffer(
                    raw, dtype=np.uint8
                ).reshape(count, spec["nbytes"])
            view = memoryview(records).cast("B")
            written = 0
            while written < len(view):
                written += os.pwrite(out_fd, view[written:], base + written)
            layers[name] = {"base": base, "stride": stride, "num_experts": count, "fields": fields}
            base += stride * count
            print(f"repacked {name}: {count} experts x {stride / 1e6:.3f} MB")
        os.fsync(out_fd)
    finally:
        os.close(out_fd)
        for fd in src_fds.values():
            os.close(fd)
    (dest / "experts.json").write_text(
        json.dumps({"format": 1, "source": str(model_path), "layers": layers}, indent=1)
    )
    print(f"wrote {base / 1e9:.2f} GB to {bin_path}")


def _layer_records(
    name: str, tensors: dict[str, StoredTensor], layout: dict, fd: int
) -> ExpertRecords:
    """The layer's records, refusing a layout that does not match the checkpoint's tensors."""
    entry = layout.get(name)
    if entry is None:
        raise StreamingError(f"{name} is missing from the repacked experts; repack again")
    expected, stride = _record_layout(tensors)
    if entry["fields"] != expected or entry["stride"] != stride:
        raise StreamingError(f"{name}: repacked layout does not match the checkpoint; repack again")
    return ExpertRecords(
        fd=fd,
        base=entry["base"],
        stride=stride,
        fields=tuple(
            RecordField(
                name=f["name"],
                offset=f["offset"],
                nbytes=f["nbytes"],
                expert_shape=tuple(f["shape"]),
                np_dtype=_DTYPES[f["dtype"]][0],
            )
            for f in entry["fields"]
        ),
    )


def load_streaming(
    model_path: Path,
    cache_gb: float = 0.0,
    read_threads: int = 8,
    no_page_cache: bool = False,
    repacked: Path | None = None,
    bank_gb: float = 0.0,
    pin_layers: int = 0,
    prewarm: bool = False,
):
    """Load ``model_path`` with routed experts left on disk. Returns (model, config, stats).

    ``cache_gb`` and ``bank_gb`` are alternative expert caches: a global LRU of per-expert arrays,
    or a per-layer GPU slot bank of ``bank_gb`` split evenly across the streamed layers.
    ``pin_layers`` keeps the first N MoE layers fully resident: no streaming and no host sync.
    ``prewarm`` fills the cache or bank before returning.

    ``no_page_cache`` reads experts with the OS file cache bypassed (macOS ``F_NOCACHE``), so
    every miss is a real SSD read — the case of a machine with no memory to spare.
    ``repacked`` reads experts from a ``repack`` output instead of the checkpoint's tensors."""
    if cache_gb > 0 and bank_gb > 0:
        raise StreamingError("choose one expert cache: cache_gb or bank_gb, not both")
    model, config = load_model(model_path, lazy=True)
    stored = index_safetensors(model_path)
    stats = Stats()
    cache = ExpertCache(budget_bytes=int(cache_gb * 1e9))
    pool = ThreadPoolExecutor(max_workers=read_threads)
    fds: dict[str, int] = {}

    def _open(path: str) -> int:
        if path not in fds:
            fds[path] = os.open(path, os.O_RDONLY)
            if no_page_cache:
                fcntl.fcntl(fds[path], fcntl.F_NOCACHE, 1)
        return fds[path]

    layout = None
    if repacked is not None:
        layout = json.loads((repacked / "experts.json").read_text())["layers"]

    layers = sorted(_moe_layers(model, stored), key=lambda layer: _layer_number(layer[0]))
    if pin_layers >= len(layers):
        raise StreamingError(f"pinning {pin_layers} of {len(layers)} MoE layers streams nothing")
    stats.pinned_layers = pin_layers
    stats.streamed_layers = len(layers) - pin_layers

    swaps = []
    for name, module, tensors in layers[pin_layers:]:
        meta = {}
        for tensor_name, entry in tensors.items():
            np_dtype, mx_view = _DTYPES[entry.dtype]
            meta[tensor_name] = ExpertTensor(
                fd=_open(entry.path),
                offset=entry.offset,
                expert_nbytes=_expert_nbytes(entry),
                expert_shape=entry.shape[1:],
                np_dtype=np_dtype,
                mx_view=mx_view,
                mx_dtype=_NAMED_MX_DTYPES[entry.dtype],
            )
        records = None
        if layout is not None:
            records = _layer_records(name, tensors, layout, _open(str(repacked / "experts.bin")))
        num_experts = next(iter(tensors.values())).shape[0]
        experts = LayerExperts(
            name, meta, cache, pool, stats, records=records, num_experts=num_experts
        )
        swaps.append((name, StreamingSwitchGLU(module, experts)))

    if not swaps:
        raise StreamingError("the model has no SwitchGLU experts to stream")
    for name, streaming in swaps:
        parent, attr = _parent_and_attr(model, name)
        setattr(parent, attr, streaming)
    # Only the dense part (and any pinned layers) is materialized: the swapped-out expert arrays
    # were never evaluated.
    mx.eval(model.parameters())

    if bank_gb > 0:
        per_expert = swaps[0][1]._experts.expert_nbytes
        slots = min(swaps[0][1]._experts.num_experts, int(bank_gb * 1e9 / len(swaps) / per_expert))
        if slots < 1:
            raise StreamingError(f"bank_gb={bank_gb} is less than one expert per streamed layer")
        stats.bank_slots = slots
        for _, streaming in swaps:
            streaming._experts.bank = SlotBank(streaming._experts, slots)
    if prewarm:
        for _, streaming in swaps:
            streaming._experts.prewarm()
            # Evaluate each layer's fill before reading the next: queued fills keep every
            # layer's read buffers alive until evaluation, doubling the footprint.
            bank = streaming._experts.bank
            if bank is not None:
                mx.eval(list(bank.arrays.values()))
            mx.clear_cache()
        stats.reset_counters()
    return model, config, stats


def _layer_number(module_name: str) -> int:
    match = re.search(r"layers\.(\d+)\.", module_name)
    if match is None:
        raise StreamingError(f"{module_name}: cannot tell which layer this MoE block is in")
    return int(match.group(1))


# ── commands ─────────────────────────────────────────────────────────────────────────────────


def _resolve_model_path(model: str) -> Path:
    path = Path(model).expanduser()
    if path.exists():
        return path
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(model, local_files_only=True))


def cmd_run(args) -> None:
    model_path = _resolve_model_path(args.model)
    if args.memory_limit_gb:
        mx.set_memory_limit(int(args.memory_limit_gb * 1e9))
    mx.reset_peak_memory()
    start = time.perf_counter()
    stats = None
    if args.mode == "resident":
        model, _ = load_model(model_path)
    else:
        model, _, stats = load_streaming(
            model_path,
            args.cache_gb,
            args.read_threads,
            args.no_page_cache,
            Path(args.repacked).expanduser() if args.repacked else None,
            bank_gb=args.bank_gb,
            pin_layers=args.pin_layers,
            prewarm=args.prewarm,
        )
    load_seconds = time.perf_counter() - start
    resident_after_load = mx.get_active_memory()
    tokenizer = load_tokenizer(model_path)

    from mlx_lm.generate import stream_generate

    messages = [{"role": "user", "content": args.prompt}]
    try:
        prompt = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, enable_thinking=False
        )
    except TypeError:
        prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True)

    tokens: list[int] = []
    text = []
    last = None
    first_token_at = None
    gen_start = time.perf_counter()
    for response in stream_generate(model, tokenizer, prompt, max_tokens=args.max_tokens):
        if first_token_at is None:
            first_token_at = time.perf_counter() - gen_start
        tokens.append(response.token)
        text.append(response.text)
        last = response
    result = {
        "mode": args.mode,
        "model": str(model_path),
        "cache_gb": args.cache_gb if args.mode == "stream" else None,
        "no_page_cache": args.no_page_cache if args.mode == "stream" else None,
        "repacked": args.repacked if args.mode == "stream" else None,
        "bank_gb": args.bank_gb if args.mode == "stream" else None,
        "pin_layers": args.pin_layers if args.mode == "stream" else None,
        "prewarm": args.prewarm if args.mode == "stream" else None,
        "load_seconds": round(load_seconds, 2),
        "resident_gb_after_load": round(resident_after_load / 1e9, 2),
        "prompt_tokens": last.prompt_tokens if last else 0,
        "prompt_tps": round(last.prompt_tps, 2) if last else 0,
        "time_to_first_token_s": round(first_token_at or 0, 2),
        "generation_tokens": last.generation_tokens if last else 0,
        "generation_tps": round(last.generation_tps, 2) if last else 0,
        "peak_mlx_gb": round(mx.get_peak_memory() / 1e9, 2),
        "max_rss_gb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9, 2),
        "streaming": stats.as_dict() if stats else None,
        "text": "".join(text),
        "tokens": tokens,
    }
    summary = {k: v for k, v in result.items() if k not in ("tokens", "text")}
    print(json.dumps(summary, indent=2))
    print("--- output ---\n" + result["text"])
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))


def cmd_inspect(args) -> None:
    """Load only the dense part and report the split, without running the model."""
    model_path = _resolve_model_path(args.model)
    stored = index_safetensors(model_path)
    total = sum(
        int(np.prod(t.shape)) * np.dtype(_DTYPES[t.dtype][0]).itemsize for t in stored.values()
    )
    mx.reset_peak_memory()
    start = time.perf_counter()
    model, _, _ = load_streaming(model_path, cache_gb=0.0)
    layers = [m for _, m in model.named_modules() if isinstance(m, StreamingSwitchGLU)]
    expert_nbytes = layers[0]._experts.expert_nbytes
    num_experts = stored[f"{layers[0]._experts.layer_key}.gate_proj.weight"].shape[0]
    print(
        json.dumps(
            {
                "model": str(model_path),
                "checkpoint_gb": round(total / 1e9, 2),
                "streamed_moe_layers": len(layers),
                "experts_per_layer": num_experts,
                "mb_per_expert": round(expert_nbytes / 1e6, 3),
                "streamed_expert_gb": round(len(layers) * num_experts * expert_nbytes / 1e9, 2),
                "resident_gb_after_load": round(mx.get_active_memory() / 1e9, 2),
                "peak_mlx_gb_during_load": round(mx.get_peak_memory() / 1e9, 2),
                "load_seconds": round(time.perf_counter() - start, 2),
            },
            indent=2,
        )
    )


def _cached_fraction(path: str, evict: bool = False) -> float:
    """Fraction of ``path``'s pages held in the OS file cache (``mincore`` on a shared map).
    ``evict`` first asks the kernel to drop the file's clean cached pages (``MS_INVALIDATE``)."""
    import ctypes

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
        prot_read, map_shared = 0x1, 0x1
        addr = libc.mmap(None, size, prot_read, map_shared, fd, 0)
        if addr in (None, ctypes.c_void_p(-1).value):
            raise OSError(ctypes.get_errno(), f"mmap failed for {path}")
        try:
            ms_invalidate = 0x2
            if evict and libc.msync(addr, size, ms_invalidate) != 0:
                raise OSError(ctypes.get_errno(), f"msync failed for {path}")
            pages = (size + page - 1) // page
            vec = ctypes.create_string_buffer(pages)
            if libc.mincore(addr, size, vec) != 0:
                raise OSError(ctypes.get_errno(), f"mincore failed for {path}")
            resident = sum(1 for b in vec.raw if b & 1)
            return resident / pages
        finally:
            libc.munmap(addr, size)
    finally:
        os.close(fd)


def cmd_residency(args) -> None:
    model_path = _resolve_model_path(args.model)
    paths = sorted(glob.glob(str(model_path / "model*.safetensors")))
    if args.repacked:
        paths.append(str(Path(args.repacked).expanduser() / "experts.bin"))
    for path in paths:
        size = os.path.getsize(path)
        cached = _cached_fraction(path, evict=args.evict)
        print(f"{cached:7.1%} cached  {size / 1e9:6.2f} GB  {Path(path).name}")


def cmd_repack(args) -> None:
    repack_experts(_resolve_model_path(args.model), Path(args.dest).expanduser())


def cmd_compare(args) -> None:
    a = json.loads(Path(args.a).read_text())
    b = json.loads(Path(args.b).read_text())
    ta, tb = a["tokens"], b["tokens"]
    first_diff = next((i for i, (x, y) in enumerate(zip(ta, tb, strict=False)) if x != y), None)
    if first_diff is None and len(ta) == len(tb):
        print(f"IDENTICAL: {len(ta)} tokens")
        return
    print(f"DIFFER at token {first_diff} (lengths {len(ta)} vs {len(tb)})")
    sys.exit(1)


def _tiny_moe(model_dir: Path) -> None:
    """Write a small random, 4-bit quantized qwen3_moe checkpoint to ``model_dir``."""
    from mlx_lm.models.qwen3_moe import Model, ModelArgs

    config = {
        "model_type": "qwen3_moe",
        "hidden_size": 128,
        "num_hidden_layers": 3,
        "intermediate_size": 256,
        "num_attention_heads": 4,
        "num_experts": 16,
        "num_experts_per_tok": 4,
        "decoder_sparse_step": 1,
        "mlp_only_layers": [],
        "moe_intermediate_size": 128,
        "rms_norm_eps": 1e-6,
        "vocab_size": 512,
        "num_key_value_heads": 2,
        "head_dim": 32,
        "rope_theta": 10000.0,
        "tie_word_embeddings": False,
        "max_position_embeddings": 512,
        "norm_topk_prob": True,
    }
    mx.random.seed(0)
    model = Model(ModelArgs.from_dict(config))
    nn.quantize(model, group_size=64, bits=4)
    config["quantization"] = {"group_size": 64, "bits": 4}
    mx.save_safetensors(
        str(model_dir / "model.safetensors"), dict(tree_flatten(model.parameters()))
    )
    (model_dir / "config.json").write_text(json.dumps(config))


def _logits(model, prompt: mx.array, decode_steps: int) -> list[mx.array]:
    """Prefill ``prompt`` then decode greedily, returning every step's logits."""
    from mlx_lm.models.cache import make_prompt_cache

    cache = make_prompt_cache(model)
    out = [model(prompt[None], cache=cache)[0, -1]]
    for _ in range(decode_steps):
        nxt = mx.argmax(out[-1]).reshape(1, 1)
        out.append(model(nxt, cache=cache)[0, -1])
    mx.eval(out)
    return out


def cmd_selftest(_args) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        model_dir = Path(tmp)
        _tiny_moe(model_dir)
        resident, _ = load_model(model_dir)
        # 40 prompt tokens x 4 experts = 160 routed rows, past the 64-row sorted-gather threshold,
        # so both the sorted (prefill) and unsorted (decode) kernel paths are compared.
        prompt = mx.array(np.random.default_rng(0).integers(0, 512, size=40))
        expected = _logits(resident, prompt, decode_steps=12)
        dense_bytes = sum(v.nbytes for _, v in tree_flatten(resident.parameters()))
        stored = index_safetensors(model_dir)
        one_expert = sum(
            _expert_nbytes(entry)
            for key, entry in stored.items()
            if key.startswith("model.layers.0.mlp.switch_mlp.")
        )
        repacked = model_dir / "repacked"
        repack_experts(model_dir, repacked)

        variants = [
            {},
            {"no_page_cache": True},
            {"cache_gb": 1.0},
            # Three experts across all layers: evictions on every layer.
            {"cache_gb": 3 * one_expert / 1e9},
            {"cache_gb": 1.0, "prewarm": True},
            # Six slots per layer: decode (4 experts) runs from the bank with evictions, while
            # prefill (up to 16 experts) overflows it and is served from hits plus disk reads.
            {"bank_gb": 6 * 3 * one_expert / 1e9},
            {"bank_gb": 1.0, "prewarm": True},
            {"bank_gb": 6 * 2 * one_expert / 1e9, "pin_layers": 1},
            {"pin_layers": 2},
        ]
        for source in (None, repacked):
            for variant in variants:
                streaming, _, stats = load_streaming(model_dir, repacked=source, **variant)
                got = _logits(streaming, prompt, decode_steps=12)
                same = all(mx.array_equal(a, b).item() for a, b in zip(expected, got, strict=True))
                held = sum(v.nbytes for _, v in tree_flatten(streaming.parameters()))
                facts = stats.as_dict()
                print(
                    f"{'repacked  ' if source else 'checkpoint'} {json.dumps(variant):48} "
                    f"identical={same} params={held / 1e6:.2f}/{dense_bytes / 1e6:.2f} MB "
                    f"slots={facts['bank_slots_per_layer']} "
                    f"hits={facts['cache_hits']}/{facts['experts_requested']}"
                )
                if not same:
                    sys.exit(1)
    print("selftest OK")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="generate once and report speed and memory")
    run.add_argument("--model", required=True, help="checkpoint path or a cached HF repo id")
    run.add_argument("--mode", choices=("resident", "stream"), required=True)
    run.add_argument("--cache-gb", type=float, default=0.0, help="expert cache budget (stream)")
    run.add_argument("--read-threads", type=int, default=8)
    run.add_argument("--repacked", help="read experts from this repack output (stream)")
    run.add_argument("--bank-gb", type=float, default=0.0, help="GPU slot bank budget (stream)")
    run.add_argument(
        "--pin-layers", type=int, default=0, help="keep the first N MoE layers resident"
    )
    run.add_argument("--prewarm", action="store_true", help="fill the cache or bank first (stream)")
    run.add_argument(
        "--no-page-cache", action="store_true", help="bypass the OS file cache (stream)"
    )
    run.add_argument("--memory-limit-gb", type=float, default=0.0)
    run.add_argument("--prompt", default="Explain how a mixture-of-experts model routes tokens.")
    run.add_argument("--max-tokens", type=int, default=128)
    run.add_argument("--out", help="write the full result (with token ids) as JSON")
    run.set_defaults(func=cmd_run)

    inspect = sub.add_parser("inspect", help="load only the dense part and report the split")
    inspect.add_argument("--model", required=True, help="checkpoint path or a cached HF repo id")
    inspect.set_defaults(func=cmd_inspect)

    residency = sub.add_parser("residency", help="how much of each weight file is cached")
    residency.add_argument("--model", required=True)
    residency.add_argument("--evict", action="store_true", help="drop the files from the cache")
    residency.add_argument("--repacked", help="also cover this repack output's experts.bin")
    residency.set_defaults(func=cmd_residency)

    repack = sub.add_parser("repack", help="write the experts expert-major for one-read misses")
    repack.add_argument("--model", required=True)
    repack.add_argument("--dest", required=True)
    repack.set_defaults(func=cmd_repack)

    compare = sub.add_parser("compare", help="check two runs produced the same tokens")
    compare.add_argument("a")
    compare.add_argument("b")
    compare.set_defaults(func=cmd_compare)

    selftest = sub.add_parser("selftest", help="streaming matches resident on a tiny random MoE")
    selftest.set_defaults(func=cmd_selftest)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
