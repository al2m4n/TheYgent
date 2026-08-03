"""What is actually inside a local weights file — the fail-fast check for diffusion models.

Every other modality's weights are self-describing enough that the engine's own startup is a
good-enough guard: a bad chat GGUF makes ``llama-server`` exit with a clear line. Image
generation is not like that. The text-to-image world ships **components** — a bare denoiser
(the diffusion transformer) in one repo, its VAE and text encoders in others — and a component
carries the same ``.gguf`` extension, the same ``text-to-image`` pipeline tag, and the same
multi-gigabyte weight as a complete model. Handed one, ``sd-cli`` fails deep in its model-load
path with ``get sd version from file failed``: it could not decide which architecture the file
is, and says nothing about the parts that are missing.

So this module answers the one question that separates the two, without loading any weights:
does the file carry a **VAE**? Every single-file checkpoint ``sd-cli`` can load — SD 1.x/2.x,
SDXL, and the all-in-one conversions — stores it under ``first_stage_model.``; no component
conversion does. Reading the GGUF header alone is enough to tell, which turns a 502 at first
generation into a refusal at registration that names the missing pieces.

Stdlib only, no weights read: this runs on the registration path, where a multi-gigabyte file
must not be paged in to answer a metadata question.
"""

from __future__ import annotations

import os
import struct

#: GGUF versions 2 and 3 share the 64-bit count header this reader understands. v1 (32-bit
#: counts) is extinct in published weights, and a future version reads as "cannot tell" —
#: never as a defect, so an unparseable file is passed through to the engine unchanged.
_SUPPORTED_VERSIONS = (2, 3)

#: Header bytes this reader will consume before giving up. Tensor-info blocks run to a few
#: hundred KB even for the largest checkpoints; the cap is what keeps a corrupt file claiming
#: billions of tensors from being read as an allocation.
_MAX_HEADER_BYTES = 64 * 1024 * 1024

#: Read sizes, tried in order. The first covers every header seen in practice, so the cap above
#: is paid only by a file that genuinely needs it — a registration must not page in megabytes to
#: answer a metadata question, and the body behind the header is never touched either way.
_HEADER_READS = (1024 * 1024, _MAX_HEADER_BYTES)

#: How deep a metadata array may nest before the file is called unreadable. Published GGUFs nest
#: one level (an array of scalars or strings); the bound is what keeps a corrupt or hostile file
#: from turning the reader's recursion into a `RecursionError` on the registration path — this
#: module owes its callers a verdict or a "cannot tell", never an exception they don't expect.
_MAX_VALUE_DEPTH = 8

#: The VAE's tensor prefix, and the presence test for a *complete* checkpoint. sd-cli reports
#: the individual missing ``first_stage_model.encoder.*`` tensors itself, but only once it has
#: already been told which load strategy to use — too late to explain anything to the user.
_VAE_PREFIX = "first_stage_model."

#: Text-encoder tensor prefixes across the checkpoint families, used to describe what a file is
#: missing rather than to decide it: the VAE test above is the decision. SD 1.x/2.x nest the
#: encoder under ``cond_stage_model``, SDXL under ``conditioner.embedders`` (matched by its
#: leading segment, like every section), and the SD3/FLUX all-in-one conversions under
#: ``text_encoders``.
_TEXT_ENCODER_PREFIXES = ("cond_stage_model.", "conditioner.", "text_encoders.")

#: The token-embedding section every llama.cpp text model carries, and no diffusion checkpoint
#: does. It distinguishes the two ways an image binding can point at unusable weights — a
#: diffusion model missing its other halves, or a language model registered as an image model —
#: which need opposite corrections and so must not share a message.
_TEXT_MODEL_PREFIX = "token_embd."

#: GGUF metadata value type → its ``struct`` format. Types absent here are the two that carry
#: their own length (string, array) and are handled by the reader directly.
_SCALAR_FORMAT = {
    0: "B",  # uint8
    1: "b",  # int8
    2: "H",  # uint16
    3: "h",  # int16
    4: "I",  # uint32
    5: "i",  # int32
    6: "f",  # float32
    7: "?",  # bool
    10: "Q",  # uint64
    11: "q",  # int64
    12: "d",  # float64
}
_TYPE_STRING = 8
_TYPE_ARRAY = 9

#: Diffusion-transformer architectures whose GGUF conversions are published as bare denoisers.
#: A *complete* checkpoint predates this convention and carries no ``general.architecture`` at
#: all, so a name here is positive evidence of a component. Best-effort by construction — an
#: unrecognized architecture reads as "cannot tell" and reaches the authoritative local check in
#: ``diffusion_defect`` instead, so a name missing from this list costs a download, never a
#: wrongly refused install.
_COMPONENT_ARCHITECTURES = frozenset(
    {
        "aura_flow",
        "auraflow",
        "chroma",
        "cosmos",
        "flux",
        "hidream",
        "hunyuan_video",
        "hunyuan-video",
        "ltx_video",
        "ltx-video",
        "ltxv",
        "lumina2",
        "mochi",
        "qwen_image",
        "sd3",
        "wan",
    }
)


class IncompleteWeights(RuntimeError):
    """A weights file cannot serve its registered modality because parts of the model are
    absent. Distinct from a missing engine binary: the engine is installed and healthy, and
    no retry or restart changes the outcome — the file itself is a fragment."""


class _Truncated(ValueError):
    """The header ran past the bytes in hand — the file is short, or the read stopped before the
    end of a header this large. Distinct from a malformed one, because only this case is worth
    re-reading with a bigger window."""


def _section(name: str) -> str:
    """The section a tensor name belongs to: its leading dotted segment. The one definition of
    what a section is — used both to build the summary's set and to query it, so a multi-segment
    constant can never quietly match nothing."""
    return name.split(".", 1)[0]


class _Header:
    """A bounded reader over a GGUF header. Every read is length-checked, so a truncated or
    hostile file raises rather than seeking past the end or allocating on a claimed count."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self._pos + n > len(self._data):
            raise _Truncated("gguf header ended early")
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def scalar(self, fmt: str) -> int | float | bool:
        size = struct.calcsize("<" + fmt)
        return struct.unpack("<" + fmt, self.take(size))[0]

    def u32(self) -> int:
        return int(self.scalar("I"))

    def u64(self) -> int:
        return int(self.scalar("Q"))

    def string(self) -> str:
        return self.take(self.u64()).decode("utf-8", "replace")

    def skip_value(self, value_type: int, depth: int = 0) -> None:
        """Advance past one metadata value. Only ``general.architecture`` is read for real;
        everything else (tokenizer vocabularies especially) is stepped over, and arrays of
        fixed-width scalars are stepped over in one jump rather than element by element."""
        if value_type == _TYPE_STRING:
            self.take(self.u64())
            return
        if value_type == _TYPE_ARRAY:
            if depth >= _MAX_VALUE_DEPTH:
                raise ValueError("gguf metadata nested past any real file")
            element_type = self.u32()
            count = self.u64()
            if element_type in _SCALAR_FORMAT:
                self.take(count * struct.calcsize("<" + _SCALAR_FORMAT[element_type]))
                return
            for _ in range(count):
                self.skip_value(element_type, depth + 1)
            return
        if value_type not in _SCALAR_FORMAT:
            raise ValueError(f"unknown gguf value type {value_type}")
        self.take(struct.calcsize("<" + _SCALAR_FORMAT[value_type]))


class GgufSummary:
    """The header facts that decide whether a diffusion file is runnable."""

    def __init__(self, architecture: str | None, tensor_prefixes: frozenset[str]) -> None:
        self.architecture = architecture
        #: Leading dotted segment of every tensor name (``first_stage_model``, ``blocks``, …).
        #: Prefixes rather than full names: it is the *sections* of a checkpoint that matter,
        #: and a set of them stays small for a file with thousands of tensors.
        self.tensor_prefixes = tensor_prefixes

    def has_section(self, prefix: str) -> bool:
        return _section(prefix) in self.tensor_prefixes


def read_gguf_summary(path: str) -> GgufSummary | None:
    """Header facts for a GGUF file, or ``None`` when the file is not a GGUF this reader
    understands — unreadable, truncated, another format, or a future version. ``None`` always
    means "cannot tell" and never "defective": a file this reader cannot classify is handed to
    the engine exactly as before, so the guard can only ever add precision."""
    for limit in _HEADER_READS:
        try:
            with open(path, "rb") as fh:
                data = fh.read(limit)
        except OSError:
            return None
        try:
            return _parse_header(data)
        except _Truncated:
            if len(data) < limit:
                return None  # the whole file is in hand, so it really does end early
        except (ValueError, struct.error, MemoryError, OverflowError):
            return None
    return None  # a header past the cap: unreadable here, and the engine speaks for the file


def _parse_header(data: bytes) -> GgufSummary | None:
    """Parse one in-memory header. ``None`` for a file that is not a GGUF this reader supports;
    ``_Truncated`` when the bytes ran out, which is the caller's cue to read further."""
    if not data.startswith(b"GGUF"):
        return None
    header = _Header(data)
    header.take(4)  # magic
    if header.u32() not in _SUPPORTED_VERSIONS:
        return None
    tensor_count = header.u64()
    kv_count = header.u64()
    architecture: str | None = None
    for _ in range(kv_count):
        key = header.string()
        value_type = header.u32()
        if key == "general.architecture" and value_type == _TYPE_STRING:
            architecture = header.string()
        else:
            header.skip_value(value_type)
    prefixes: set[str] = set()
    for _ in range(tensor_count):
        prefixes.add(_section(header.string()))
        dimensions = header.u32()
        header.take(8 * dimensions)  # dimension extents
        header.take(4)  # ggml type
        header.take(8)  # offset into the tensor-data block
    return GgufSummary(architecture, frozenset(prefixes))


def is_component_architecture(architecture: str | None) -> bool:
    """Whether a GGUF architecture name identifies a bare denoiser rather than a whole model.

    The one check that needs no local file, so the discovery catalog can spend it on the
    architecture Hugging Face reports for a repo and refuse the install before the download.
    Best-effort in the safe direction — see ``_COMPONENT_ARCHITECTURES``."""
    return (architecture or "").strip().lower() in _COMPONENT_ARCHITECTURES


def diffusion_defect(path: str) -> str | None:
    """Why ``path`` cannot be rendered from on its own, or ``None`` if it can be.

    The authoritative check, run against the real bytes on the host that will serve them. A
    complete checkpoint bundles a denoiser, a VAE and a text encoder in one file; the returned
    string names which of those are absent, because "install the matching VAE and text encoder"
    is the only action that helps and no part of the failure says so otherwise."""
    summary = read_gguf_summary(path)
    if summary is None:  # not a GGUF this reader understands — the engine speaks for it
        return None
    if summary.has_section(_VAE_PREFIX):
        return None
    name = os.path.basename(path)
    architecture = f" ({summary.architecture})" if summary.architecture else ""
    if summary.has_section(_TEXT_MODEL_PREFIX):
        return (
            f"{name} is a language model{architecture}, not a diffusion checkpoint — it has no "
            f"VAE and no denoiser. Register it under a text modality and point this binding at "
            f"an image-generation model"
        )
    missing = ["a VAE"]
    if not any(summary.has_section(p) for p in _TEXT_ENCODER_PREFIXES):
        missing.append("a text encoder")
    return (
        f"{name} holds a diffusion model's denoiser only{architecture} and is missing "
        f"{' and '.join(missing)}. Image generation loads ONE self-contained checkpoint, so a "
        f"repository that publishes the denoiser by itself cannot be installed and run as a "
        f"model — install a complete text-to-image checkpoint instead"
    )
