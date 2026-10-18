"""``mlx_lm.server`` for mixture-of-experts models larger than memory: routed experts stay on disk.

Every non-expert weight (attention, embeddings, router, shared experts) is loaded as usual; the
routed expert weights are never loaded. Each MoE layer waits for its router's top-k choice, reads
only the chosen experts' bytes from the safetensors files (each expert is one contiguous slice of
its stacked tensor), and runs mlx's own gather kernels on them. The experts a layer has used live
in a per-layer GPU slot bank, so a repeat use reads nothing and copies nothing; a miss evicts the
layer's least-recently-used slot. Output is identical to the fully loaded model's.

The server is mlx-lm's own (``mlx_lm.server``) with its model loader swapped, so the HTTP surface,
chat templates, tool parsing and batching are mlx-lm's. It runs in the Python environment mlx-lm
is installed in, not the plane's, and is launched by file path with ``python -I``; it therefore
imports only the standard library, mlx, mlx-lm and mlx-lm's own dependencies (numpy,
huggingface_hub) — never this package.

    python -I mlx_expert_stream.py --model <repo-or-path> --port 8080 [--expert-cache-gb 8]
    python -I mlx_expert_stream.py --selftest
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import math
import os
import struct
import sys
import tempfile
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

# mlx and mlx-lm are installed in mlx-lm's environment, not the plane's.
import mlx.core as mx  # ty: ignore[unresolved-import]
import mlx.nn as nn  # ty: ignore[unresolved-import]
import numpy as np
from mlx.utils import tree_flatten  # ty: ignore[unresolved-import]

try:
    import mlx_lm  # ty: ignore[unresolved-import]
    import mlx_lm.server  # ty: ignore[unresolved-import]
    from mlx_lm.models.switch_layers import (  # ty: ignore[unresolved-import]
        SwitchGLU,
        SwitchMLP,
        _gather_sort,
        _scatter_unsort,
    )
    from mlx_lm.utils import _download, load_model, load_tokenizer  # ty: ignore[unresolved-import]
except ImportError as exc:  # pragma: no cover - depends on the installed mlx-lm
    raise SystemExit(
        f"expert streaming needs mlx-lm's server and switch layers ({exc}); this mlx-lm "
        f"({getattr(sys.modules.get('mlx_lm'), '__version__', 'unknown')}) does not have them"
    ) from exc

log = logging.getLogger("theygent.expert_stream")

GLU_PROJECTIONS = ("gate_proj", "up_proj", "down_proj")
#: The share of the machine's memory the expert cache may use when the registration sets no
#: size, after the dense part of the model.
DEFAULT_MEMORY_SHARE = 0.5
#: Expert reads run on this many threads (pure I/O; mlx arrays are built on the calling thread).
READ_THREADS = 8
#: Streaming statistics are logged every this many MoE layer calls.
STATS_EVERY = 20_000

# safetensors dtype -> (numpy dtype the bytes are read as, the mlx dtype they hold).
# numpy has no bfloat16, so bf16 bytes are read as uint16 and viewed as bfloat16 in mlx.
_DTYPES: dict[str, tuple[type, mx.Dtype]] = {
    "U32": (np.uint32, mx.uint32),
    "U8": (np.uint8, mx.uint8),
    "F32": (np.float32, mx.float32),
    "F16": (np.float16, mx.float16),
    "BF16": (np.uint16, mx.bfloat16),
}
_DTYPE_NAMES = {mx_dtype: name for name, (_, mx_dtype) in _DTYPES.items()}


class StreamingError(RuntimeError):
    """The model cannot be expert-streamed as stored."""


# ── the checkpoint's tensors ─────────────────────────────────────────────────────────────────


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
                path, data_start + start, entry["dtype"], tuple(entry["shape"])
            )
    return index


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


@dataclass(frozen=True)
class ExpertTensor:
    """One stacked expert tensor (``[num_experts, ...]``), readable one expert range at a time."""

    fd: int
    offset: int
    expert_nbytes: int
    expert_shape: tuple[int, ...]
    np_dtype: type
    mx_dtype: mx.Dtype

    def read_host(self, first: int, count: int) -> np.ndarray:
        """Read ``count`` experts from ``first`` into host memory. Pure I/O with no mlx calls, so
        it is safe on a reader thread: mlx streams belong to the thread that made them, and
        generation runs on a stream the reader threads do not have."""
        buf = _pread_exact(
            self.fd, self.expert_nbytes * count, self.offset + first * self.expert_nbytes
        )
        return np.frombuffer(buf, dtype=self.np_dtype).reshape((count, *self.expert_shape))

    def to_mx(self, host: np.ndarray) -> mx.array:
        arr = mx.array(host)
        return arr if arr.dtype == self.mx_dtype else arr.view(self.mx_dtype)


# ── per-layer experts and the slot bank ──────────────────────────────────────────────────────


@dataclass
class Stats:
    moe_calls: int = 0
    experts_requested: int = 0
    hits: int = 0
    read_bytes: int = 0
    read_seconds: float = 0.0

    def record(self) -> None:
        self.moe_calls += 1
        if self.moe_calls % STATS_EVERY == 0:
            log.info(
                "expert streaming: %.1f%% hits, %.2f GB read at %.2f GB/s",
                100 * self.hits / max(1, self.experts_requested),
                self.read_bytes / 1e9,
                self.read_bytes / 1e9 / self.read_seconds if self.read_seconds else 0.0,
            )


class LayerExperts:
    """One MoE layer's routed experts: every (projection, parameter) tensor, read by expert id."""

    def __init__(self, tensors: dict[str, ExpertTensor], pool: ThreadPoolExecutor, stats: Stats):
        self.tensors = tensors  # "gate_proj.weight" -> ExpertTensor
        self.expert_nbytes = sum(t.expert_nbytes for t in tensors.values())
        self.pool = pool
        self.stats = stats

    def read(self, experts: list[int]) -> dict[str, mx.array]:
        """``[len(experts), ...]`` arrays for every tensor, rows in ``experts`` order (sorted ids),
        read as coalesced contiguous runs with one read per (tensor, run)."""
        runs: list[tuple[int, int]] = []
        for e in experts:
            if runs and runs[-1][0] + runs[-1][1] == e:
                runs[-1] = (runs[-1][0], runs[-1][1] + 1)
            else:
                runs.append((e, 1))
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
    """A fixed number of GPU-resident expert slots for one layer: preallocated ``[slots, ...]``
    arrays per tensor, filled on a miss, evicted least-recently-used. A hit costs nothing beyond
    the gather kernels reading the bank in place.

    A call that routes to more distinct experts than there are slots (a long prefill chunk) is
    served from a compact set (bank rows for hits, disk reads for misses) and leaves the bank as
    it is."""

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

    def resolve(self, experts: list[int]) -> tuple[dict[str, mx.array], np.ndarray]:
        """Weights to gather from, and each of ``experts``' row in them (sorted, unique ids)."""
        stats = self.experts.stats
        missing = [e for e in experts if e not in self.slot_of]
        stats.experts_requested += len(experts)
        stats.hits += len(experts) - len(missing)
        if len(experts) > self.slots:
            return self._overflow(experts, missing)

        if missing:
            in_use = set(experts)
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
            fetched = self.experts.read(missing)
            # Slice assignments, grouped into runs of consecutive slots, update the bank's buffer
            # in place (every earlier read of this bank was evaluated at an earlier layer's host
            # sync). An array-indexed assignment is a scatter that copies the whole bank instead.
            for slot, row, count in _slot_runs(victims):
                for name, bank in self.arrays.items():
                    bank[slot : slot + count] = fetched[name][row : row + count]
        for e in experts:
            self.recency[e] = None
            self.recency.move_to_end(e)
        return self.arrays, np.array([self.slot_of[e] for e in experts], dtype=np.uint32)

    def _overflow(self, experts: list[int], missing: list[int]):
        hits = [e for e in experts if e in self.slot_of]
        fetched = self.experts.read(missing) if missing else {}
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
        return out, np.array([row[e] for e in experts], dtype=np.uint32)


# ── the streaming MoE module ─────────────────────────────────────────────────────────────────


class StreamingSwitchGLU(nn.Module):
    """Drop-in for ``SwitchGLU`` that holds no expert weights: it computes exactly what
    ``SwitchGLU`` computes, on the layer's slot bank."""

    def __init__(self, source: SwitchGLU, bank: SlotBank):
        super().__init__()
        self.activation = source.activation
        self._bank = bank
        self._quant = {}
        for proj in GLU_PROJECTIONS:
            linear = getattr(source, proj)
            if "scales" in linear:
                self._quant[proj] = (linear.group_size, linear.bits, linear.mode)
        self._has_bias = "gate_proj.bias" in bank.experts.tensors

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
        self._bank.experts.stats.record()
        # The one host sync per MoE layer: the routing decision must be known before the bytes
        # it selects can be read.
        routed = np.array(indices)
        experts, local = np.unique(routed.ravel(), return_inverse=True)
        w, rows = self._bank.resolve(experts.tolist())
        idx = mx.array(rows[local].reshape(routed.shape))

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


def _moe_layers(model: nn.Module, stored: dict[str, StoredTensor]):
    """Every ``SwitchGLU`` in ``model`` with its expert tensors as stored, checked to be exactly
    the arrays the model holds. Yields (module name, module, {"gate_proj.weight": tensor})."""
    for name, module in model.named_modules():
        if isinstance(module, SwitchMLP):
            raise StreamingError(
                f"{name}: this model's experts are SwitchMLP blocks, which expert streaming does "
                "not support (it streams SwitchGLU experts)"
            )
        if not isinstance(module, SwitchGLU):
            continue
        tensors: dict[str, StoredTensor] = {}
        for proj in GLU_PROJECTIONS:
            for param, held in tree_flatten(getattr(module, proj).parameters()):
                key = f"{name}.{proj}.{param}"
                entry = stored.get(key)
                if entry is None:
                    raise StreamingError(
                        f"{key} is not stored as-is in the checkpoint (the model derives it while "
                        "loading); expert streaming needs an MLX-converted checkpoint"
                    )
                if entry.shape != tuple(held.shape) or entry.dtype != _DTYPE_NAMES.get(held.dtype):
                    raise StreamingError(
                        f"{key}: stored {entry.dtype}{list(entry.shape)} differs from the model's "
                        f"{held.dtype}{list(held.shape)}; the model transforms it while loading"
                    )
                tensors[f"{proj}.{param}"] = entry
        yield name, module, tensors


def _expert_nbytes(entry: StoredTensor) -> int:
    return int(np.prod(entry.shape[1:])) * np.dtype(_DTYPES[entry.dtype][0]).itemsize


def _parent_and_attr(model: nn.Module, dotted: str):
    parts = dotted.split(".")
    obj = model
    for part in parts[:-1]:
        obj = obj[int(part)] if part.isdigit() else obj[part]
    return obj, parts[-1]


def _top_k(config: dict) -> int:
    text = config.get("text_config") or {}
    value = config.get("num_experts_per_tok") or text.get("num_experts_per_tok")
    return int(value) if isinstance(value, int) and value > 0 else 1


def load_streaming(model_path: Path, cache_gb: float | None, model_config: dict | None = None):
    """Load ``model_path`` with its routed experts left on disk. ``cache_gb`` sizes the slot
    banks across all MoE layers; ``None`` takes ``DEFAULT_MEMORY_SHARE`` of the machine's memory
    less the dense part. Returns (model, config)."""
    model, config = load_model(model_path, lazy=True, model_config=model_config)
    stored = index_safetensors(model_path)
    layers = list(_moe_layers(model, stored))
    if not layers:
        raise StreamingError(
            f"{model_path} has no mixture-of-experts layers; expert streaming applies only to "
            "MoE models"
        )

    stats = Stats()
    pool = ThreadPoolExecutor(max_workers=READ_THREADS, thread_name_prefix="expert-read")
    fds: dict[str, int] = {}
    experts_by_layer = []
    for name, module, tensors in layers:
        meta = {}
        for tensor_name, entry in tensors.items():
            if entry.path not in fds:
                fds[entry.path] = os.open(entry.path, os.O_RDONLY)
            np_dtype, mx_dtype = _DTYPES[entry.dtype]
            meta[tensor_name] = ExpertTensor(
                fds[entry.path],
                entry.offset,
                _expert_nbytes(entry),
                entry.shape[1:],
                np_dtype,
                mx_dtype,
            )
        experts_by_layer.append((name, module, LayerExperts(meta, pool, stats)))

    per_expert = experts_by_layer[0][2].expert_nbytes
    num_experts = next(iter(layers[0][2].values())).shape[0]
    all_experts_bytes = per_expert * num_experts * len(layers)
    # Sizes come from shapes alone; nothing has been read yet.
    dense_bytes = sum(v.nbytes for _, v in tree_flatten(model.parameters())) - all_experts_bytes
    if cache_gb is None:
        memory = int(mx.device_info()["memory_size"])
        budget = max(0, min(all_experts_bytes, int(memory * DEFAULT_MEMORY_SHARE) - dense_bytes))
    else:
        budget = int(cache_gb * 1e9)
    slots = min(num_experts, max(_top_k(config), budget // (per_expert * len(layers))))

    for name, module, experts in experts_by_layer:
        parent, attr = _parent_and_attr(model, name)
        setattr(parent, attr, StreamingSwitchGLU(module, SlotBank(experts, slots)))
    # Only the dense part is read: the swapped-out expert arrays are never evaluated.
    mx.eval(model.parameters())
    log.info(
        "expert streaming: %d MoE layers x %d experts (%.2f GB) stay on disk; dense part %.2f GB "
        "resident; expert cache %.2f GB (%d of %d slots per layer)",
        len(layers),
        num_experts,
        all_experts_bytes / 1e9,
        dense_bytes / 1e9,
        slots * per_expert * len(layers) / 1e9,
        slots,
        num_experts,
    )
    return model, config


# ── mlx_lm.server integration ────────────────────────────────────────────────────────────────

_CACHE_GB: float | None = None


def streaming_load(
    path_or_hf_repo: str,
    tokenizer_config: dict | None = None,
    model_config: dict | None = None,
    adapter_path: str | None = None,
    lazy: bool = False,
    return_config: bool = False,
    revision: str | None = None,
):
    """``mlx_lm.utils.load`` with the experts streamed: same signature, same return shape."""
    del lazy  # the dense part is always evaluated; the experts are never loaded
    if adapter_path is not None:
        raise StreamingError("expert streaming does not support LoRA adapters")
    model_path = _download(path_or_hf_repo, revision=revision)
    model, config = load_streaming(model_path, _CACHE_GB, model_config=model_config)
    tokenizer = load_tokenizer(
        model_path, tokenizer_config, eos_token_ids=config.get("eos_token_id", None)
    )
    return (model, tokenizer, config) if return_config else (model, tokenizer)


def _local_model_path(model: str) -> Path | None:
    """The model's local directory when it is already on this machine, else ``None``."""
    path = Path(model)
    if path.exists():
        return path
    try:
        from huggingface_hub import snapshot_download

        return Path(snapshot_download(model, local_files_only=True))
    except Exception:  # absent from the cache, or not a repo id
        return None


def preflight(model: str) -> None:
    """Refuse a model that cannot be expert-streamed before the server starts, so a launch
    fails with the reason instead of serving a model that will never load. Reads only file
    headers and the model's structure; a model not yet downloaded is checked when it loads."""
    model_path = _local_model_path(model)
    if model_path is None:
        return
    lazy_model, _ = load_model(model_path, lazy=True)
    if not list(_moe_layers(lazy_model, index_safetensors(model_path))):
        raise StreamingError(
            f"{model} has no mixture-of-experts layers; expert streaming applies only to MoE models"
        )


def _exit_when_the_startup_load_fails() -> None:
    """mlx_lm.server loads its ``--model`` on its generation thread, outside any handler: a
    failure there ends that thread while the HTTP server keeps answering, so every request would
    wait forever. Exit instead, so the plane sees the engine die and reads why in its log."""
    load_default = mlx_lm.server.ModelProvider.load_default

    def guarded(self) -> None:
        try:
            load_default(self)
        except BaseException:
            log.exception("expert streaming: loading the model failed")
            logging.shutdown()
            os._exit(1)

    mlx_lm.server.ModelProvider.load_default = guarded


def _model_arg(args: list[str]) -> str | None:
    for i, arg in enumerate(args):
        if arg == "--model" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--model="):
            return arg.split("=", 1)[1]
    return None


def main(argv: list[str] | None = None) -> None:
    global _CACHE_GB
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--expert-cache-gb", type=float, default=None)
    parser.add_argument("--selftest", action="store_true")
    ours, rest = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    if ours.selftest:
        selftest()
        return
    if ours.expert_cache_gb is not None and not (
        math.isfinite(ours.expert_cache_gb) and ours.expert_cache_gb > 0
    ):
        raise SystemExit(f"--expert-cache-gb must be a positive number, got {ours.expert_cache_gb}")
    if not hasattr(getattr(mlx_lm.server, "ModelProvider", None), "load_default"):
        raise SystemExit(
            f"mlx-lm {getattr(mlx_lm, '__version__', '?')} has no ModelProvider.load_default; "
            "expert streaming does not support this mlx-lm"
        )
    if model := _model_arg(rest):
        try:
            preflight(model)
        except StreamingError as exc:
            raise SystemExit(f"expert streaming: {exc}") from exc
    _CACHE_GB = ours.expert_cache_gb
    mlx_lm.server.load = streaming_load
    _exit_when_the_startup_load_fails()
    sys.argv = [sys.argv[0], *rest]
    mlx_lm.server.main()


# ── self-test ────────────────────────────────────────────────────────────────────────────────


def _tiny_moe(model_dir: Path) -> None:
    """A small random, 4-bit quantized qwen3_moe checkpoint."""
    from mlx_lm.models.qwen3_moe import Model, ModelArgs  # ty: ignore[unresolved-import]

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
    mx.save_safetensors(
        str(model_dir / "model.safetensors"), dict(tree_flatten(model.parameters()))
    )
    (model_dir / "config.json").write_text(
        json.dumps({**config, "quantization": {"group_size": 64, "bits": 4}})
    )


def _logits(model, prompt: mx.array, decode_steps: int) -> list[mx.array]:
    from mlx_lm.models.cache import make_prompt_cache  # ty: ignore[unresolved-import]

    cache = make_prompt_cache(model)
    out = [model(prompt[None], cache=cache)[0, -1]]
    for _ in range(decode_steps):
        out.append(model(mx.argmax(out[-1]).reshape(1, 1), cache=cache)[0, -1])
    mx.eval(out)
    return out


def selftest() -> None:
    """Streamed logits equal the fully loaded model's, bit for bit, on a tiny random MoE: with a
    bank that holds every expert, and with one that evicts on every decode step and overflows on
    prefill. Prints one line per case and exits non-zero on any difference."""
    with tempfile.TemporaryDirectory() as tmp:
        model_dir = Path(tmp)
        _tiny_moe(model_dir)
        resident, _ = load_model(model_dir)
        # 40 prompt tokens x 4 experts = 160 routed rows, past the 64-row sorted-gather
        # threshold, so the sorted (prefill) and unsorted (decode) kernel paths both run.
        prompt = mx.array(np.random.default_rng(0).integers(0, 512, size=40))
        expected = _logits(resident, prompt, decode_steps=12)
        per_expert = sum(
            _expert_nbytes(entry)
            for key, entry in index_safetensors(model_dir).items()
            if key.startswith("model.layers.0.mlp.switch_mlp.")
        )
        # 1 GB holds all 16 experts per layer; six slots per layer (of 16) evict on every step.
        for cache_gb in (1.0, 6 * 3 * per_expert / 1e9):
            streamed, _ = load_streaming(model_dir, cache_gb)
            got = _logits(streamed, prompt, decode_steps=12)
            same = all(mx.array_equal(a, b).item() for a, b in zip(expected, got, strict=True))
            print(f"expert cache {cache_gb:.6f} GB: identical={same}")
            if not same:
                raise SystemExit(1)
    print("selftest OK")


if __name__ == "__main__":
    main()
