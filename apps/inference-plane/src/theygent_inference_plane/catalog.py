"""Discovery & install: the ``CatalogProvider`` seam + the Hugging Face model adapter.

One interface, normalized types, many pluggable providers. The HTTP routes and the install flow
speak the **normalized** ``CatalogEntry`` / ``CatalogVariant`` / ``InstallPlan`` shapes *only* —
never a provider's raw response. Adding a source later (the MCP registry → ``mcp_tools``; Apify →
``tools``) is a new class implementing ``CatalogProvider`` + a category in the taxonomy; it touches
neither the renderer nor the install flow. If "add another category" ever requires editing this
module's core, the seam has eroded.

**Plane rule.** This lives in the *inference plane* — the user's machine, their trust domain.
Listing reads a provider API; install **downloads weights here** and registers them in the
inference-plane-local registry. Nothing crosses into the control plane. This module imports
no SQLAlchemy / asyncpg / control-plane code (asserted by ``tests/test_catalog_plane.py``).

The Hugging Face adapter (the only one shipped now) filters listings by the engines the user
has ready (never surface a model that can't run here) and surfaces quant variants sized against
this machine's RAM with a fit badge.
"""

from __future__ import annotations

import json
import re
from itertools import islice
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from theygent_ir import Modality

from theygent_inference_plane.capabilities import (
    chat_template_from_config,
    detect_vision,
    template_implies_reasoning,
    template_implies_tools,
)

# The managed engines an entry can be installed for (the binding enum). ``openai-compatible``
# is a reachable passthrough — never installed — so it is not an install target.
EngineName = Literal["mlx", "llamacpp", "vllm"]
Sort = Literal["trending", "downloads", "likes"]
#: How a variant's weight size compares to this machine's RAM (the green/yellow fit badge).
Fit = Literal["fits", "tight", "too-large", "unknown"]

#: Engine → Hugging Face ``library`` tag used to filter the listing. ``vllm`` uses plain safetensors
#: (no single library tag) and is CUDA-only/unverified here, so it is intentionally absent — a
#: not-ready engine never reaches the filter anyway. Adding an engine library is a one-line change.
ENGINE_LIBRARY: dict[str, str] = {"mlx": "mlx", "llamacpp": "gguf"}

#: HF ``pipeline_tag`` → the theygent ``modality`` the model serves. A model's modality is a
#: property of the *model* (its task), not of the engine — so install derives it from the repo's
#: pipeline_tag and registers the binding with it, which is what makes the manager spawn the right
#: server (``mlx`` vision → ``mlx_vlm.server``; ``llamacpp`` embeddings → ``llama-server
#: --embeddings``). An unmapped/absent tag → ``chat`` (the default; a text-generation repo needs no
#: special handling). This is why the listing fan-out need NOT be per-modality: whichever list a
#: repo appears in, the install lands the correct ``(engine, modality)`` binding from its pipeline.
_PIPELINE_MODALITY: dict[str, Modality] = {
    "image-text-to-text": "vision",
    "feature-extraction": "embeddings",
    "sentence-similarity": "embeddings",
    "automatic-speech-recognition": "audio.transcription",
    "text-to-speech": "audio.speech",
}


def _modality_for_pipeline(pipeline_tag: str | None) -> Modality:
    mod = _PIPELINE_MODALITY.get(pipeline_tag or "")
    return mod if mod is not None else "chat"


#: HF ``sort`` keys (huggingface_hub 1.x). Our enum is the stable surface; this maps it.
_HF_SORT: dict[str, str] = {
    "trending": "trending_score",
    "downloads": "downloads",
    "likes": "likes",
}

# Quant level in a GGUF filename (e.g. ``...-Q4_K_M.gguf``, ``...-IQ4_XS.gguf``, ``...-fp16.gguf``).
# Longer/more-specific alternatives first so "bf16"/"fp16" don't get clipped to "f16".
_QUANT_RE = re.compile(r"(IQ\d+[A-Z_]*|Q\d+(?:_[A-Z0-9]+)*|BF16|FP16|FP32|F16|F32)", re.IGNORECASE)
# Files that make up the actual weights (used to size an MLX repo variant).
_WEIGHT_SUFFIXES = (".safetensors", ".npz", ".bin", ".gguf")


class _Wire(BaseModel):
    """camelCase over the wire (the ``/admin/*`` convention), snake_case in code."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CatalogQuery(BaseModel):
    """A search against a provider: free text + sort + how many + the engine filter."""

    search: str = ""
    sort: Sort = "trending"
    limit: int = 30
    #: Ready managed engines (from the launcher). Empty ⇒ nothing is installable, so nothing lists.
    engines: list[str] = Field(default_factory=list)
    #: Optional HF ``num_parameters`` filter (e.g. ``"max:3B"`` / ``"min:3B,max:15B"``) — the
    #: param-size filter. ``None`` ⇒ no size constraint.
    num_params: str | None = None


class CatalogVariant(_Wire):
    """One downloadable precision/quant of an entry — a row in the variant picker."""

    id: str  # gguf filename for llamacpp; "" for a whole MLX repo (engine-unique within an entry)
    label: str  # "Q4_K_M", "4bit", ...
    engine: EngineName
    #: The modality this model serves, derived from the repo's HF pipeline_tag. Defaults to
    #: ``chat`` so a text-generation repo is unchanged; install records it on the binding.
    modality: Modality = "chat"
    filename: str | None = None  # the specific file to fetch (gguf); None ⇒ snapshot the repo
    size_bytes: int | None = None
    fit: Fit = "unknown"
    recommended: bool = False
    quality: str | None = None  # human hint: "balanced", "high quality", "full precision", ...
    fit_reason: str | None = None  # "~5 GB needed · 16 GB RAM" (tooltip on the fit badge)


class CatalogEntry(_Wire):
    """A normalized listing item. Every provider maps its raw response onto exactly this."""

    provider: str
    ref: str  # provider-native id (HF repo id, later: server.json name, actor slug)
    title: str
    description: str = ""
    category: str = "models"  # DATA, not hardcoded UI — "models" | (later) "tools" | "mcp_tools"
    kind: Literal["model", "mcp"] = "model"
    sovereignty: Literal["in-domain", "cloud-egress"] = "in-domain"
    engines: list[str] = Field(default_factory=list)  # which managed engines can run this entry
    badges: dict[str, Any] = Field(default_factory=dict)  # downloads, likes, pipelineTag
    params: str | None = None  # "7B" / "0.5B" — model size
    license: str | None = None  # "apache-2.0" / "llama3" — from the repo's license tag
    gated: bool = False  # needs an HF token to download (show a 🔒 before the user clicks)
    updated_at: str | None = None  # ISO timestamp of the last repo update (FE shows "X ago")
    # ── browse-time capability hints (no download) ──────────────────────────────────────────────
    # Derived at list()/get() time from the small metadata HF returns WITHOUT weights: the chat
    # template (→ reasoning / tool_calling), architectures + config keys (→ vision), and the GGUF
    # header / model config (→ max_context). These are best-effort STATIC hints; the authoritative
    # source stays the post-install capability PROBE (``/admin/models/{id}/capabilities``), which
    # interrogates the running engine. A deliberate, named additive extension of the normalized
    # CatalogEntry seam — a non-HF provider simply leaves them at their defaults. Field names mirror
    # ``theygent_ir.Capabilities`` so the UI badges catalog hints and probe results the same way.
    reasoning: bool = False
    tool_calling: bool = False
    vision: bool = False
    max_context: int | None = None
    installed: bool = False  # already in the local registry (cross-referenced by the route)
    installed_as: str | None = None  # the logical id it's installed under, if any
    variants: list[CatalogVariant] = Field(
        default_factory=list
    )  # populated by get(), empty in list()


class InstallPlan(_Wire):
    """What an install will do — computed by the provider, executed by the downloader. The provider
    never performs a side effect; it returns this plan and the downloader applies it."""

    logical_id: str
    engine: EngineName
    repo: str
    #: The modality the installed model serves — recorded on the registered binding so the
    #: manager spawns the right server. Derived from the repo's pipeline_tag by ``install_plan``.
    modality: Modality = "chat"
    filename: str | None = None  # specific gguf for llamacpp; None ⇒ whole repo (mlx)
    total_bytes: int | None = None  # for the progress denominator


# The ``list`` method name (the provider seam) shadows the builtin inside the class body, so route
# the return type through a module-level alias where ``list`` is unambiguously the builtin.
EntryList = list[CatalogEntry]


@runtime_checkable
class CatalogProvider(Protocol):
    """The one seam. Every discovery source implements this; the UI + install flow speak only the
    normalized types above. ``list``/``get`` read; ``install_plan`` yields a plan, not an effect."""

    id: str
    kind: str

    def list(self, q: CatalogQuery) -> EntryList: ...

    def get(self, ref: str, q: CatalogQuery) -> CatalogEntry: ...

    def install_plan(
        self, ref: str, engine: EngineName, variant_id: str, logical_id: str
    ) -> InstallPlan: ...


class CatalogError(RuntimeError):
    """A provider could not be reached / returned an unusable response."""


# ── fit heuristic (the green/yellow fit badge) ──────────────────────────────────


def fit_for(size_bytes: int | None, ram_total: int) -> Fit:
    """Classify a variant's weight size against this machine's total RAM.

    Apple Silicon unified memory ⇒ RAM *is* the budget. We leave headroom for the OS + the KV cache,
    so "fits" is conservative (green only when it comfortably runs)."""
    if not size_bytes or ram_total <= 0:
        return "unknown"
    if size_bytes * 1.25 <= ram_total * 0.7:
        return "fits"
    if size_bytes <= ram_total * 0.85:
        return "tight"
    return "too-large"


def _mark_recommended(variants: list[CatalogVariant]) -> None:
    """Mark one variant recommended: the largest that *fits* (more quality for the headroom), else
    the smallest (best chance of running at all) — defaulting to a sane quant."""
    if not variants:
        return
    fitting = [v for v in variants if v.fit == "fits" and v.size_bytes]
    if fitting:
        max(fitting, key=lambda v: v.size_bytes or 0).recommended = True
        return
    sized = [v for v in variants if v.size_bytes]
    chosen = min(sized, key=lambda v: v.size_bytes or 0) if sized else variants[0]
    chosen.recommended = True


def _quant_label(filename: str) -> str:
    m = _QUANT_RE.search(filename)
    return m.group(1).upper() if m else "GGUF"


def _mlx_precision(repo_id: str) -> str:
    """Best-effort precision label for an MLX repo from its id (``...-4bit``, ``...-8bit``, ...)."""
    m = re.search(r"(\d+)\s*bit", repo_id, re.IGNORECASE)
    if m:
        return f"{m.group(1)}-bit"
    if re.search(r"bf16|fp16|f16", repo_id, re.IGNORECASE):
        return "16-bit"
    return "MLX"


# ── listing metadata enrichment (params / license / gated / updated) ─────────────


def _format_params(mi: Any) -> str | None:
    """Model size as "7B"/"0.5B"/"499M" — from the safetensors param count when present, else
    parsed from the repo id (``...-7B-...``). None when neither is available."""
    st = getattr(mi, "safetensors", None)
    total = getattr(st, "total", None) if st is not None else None
    if isinstance(total, int) and total > 0:
        if total >= 1_000_000_000:
            return f"{total / 1_000_000_000:.1f}".rstrip("0").rstrip(".") + "B"
        return f"{round(total / 1_000_000)}M"
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b", getattr(mi, "id", "") or "")
    return f"{m.group(1)}B" if m else None


def _license_from_tags(tags: Any) -> str | None:
    for t in tags or []:
        if isinstance(t, str) and t.startswith("license:"):
            return t.split(":", 1)[1]
    return None


def _updated_iso(mi: Any) -> str | None:
    dt = getattr(mi, "last_modified", None) or getattr(mi, "created_at", None)
    try:
        return dt.isoformat() if dt else None
    except Exception:
        return None


# ── per-variant quality hint + fit reason (the fit explainers) ───────────────────


def _variant_quality(label: str, engine: str) -> str | None:
    u = label.upper()
    if engine == "mlx":
        if "4" in u:
            return "compact · 4-bit"
        if "8" in u:
            return "high quality · 8-bit"
        if "16" in u:
            return "full precision"
        return None
    if u.startswith(("Q2", "Q3", "IQ2", "IQ3")):
        return "small · lower quality"
    if u.startswith("Q4"):
        return "balanced"
    if u.startswith(("Q5", "Q6")):
        return "high quality"
    if u.startswith("Q8"):
        return "max quality · large"
    if u in ("F16", "BF16", "FP16", "F32", "FP32"):
        return "full precision · largest"
    return None


def _fit_reason(size_bytes: int | None, ram_total: int) -> str | None:
    if not size_bytes or ram_total <= 0:
        return None
    return f"~{size_bytes / 1_000_000_000:.1f} GB needed · {ram_total / 1_000_000_000:.0f} GB RAM"


def _card_summary(ref: str) -> str | None:
    """Best-effort one-paragraph blurb from the model card (README). One network fetch, on the
    detail view only; any failure (offline, no card) returns None and the UI falls back."""
    try:
        from huggingface_hub import ModelCard

        text = (ModelCard.load(ref).text or "").strip()
    except Exception:
        return None
    for para in re.split(r"\n\s*\n", text):
        p = para.strip()
        if not p or p.startswith(("#", "<", "!", "|", "-", "*")):
            continue  # skip headings, HTML, images, tables, list items
        p = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", p)  # [text](url) → text
        p = re.sub(r"[*`_]+", "", p)  # strip bold/italic/code markers
        p = re.sub(r"\s+", " ", p).strip()
        if not p:
            continue
        return p[:280] + ("…" if len(p) > 280 else "")
    return None


# ── browse-time capability hints (reuse the probe's chat-template heuristic) ──────


def _apply_hf_capabilities(entry: CatalogEntry, mi: Any) -> None:
    """Fill an entry's capability hints from an HF item fetched with ``expand=["config","gguf"]``.

    Reads only metadata already present on ``mi`` — **never a network call**. The safetensors/MLX
    path carries ``config`` (chat template + architectures); the GGUF path carries ``gguf`` (chat
    template + architecture + ``context_length``); a repo can carry both. Absent fields leave the
    entry's defaults untouched, so an unenriched ``model_info`` (list without the expand, a non-HF
    provider) is a clean no-op. The chat-template detectors are the *same* ones the live probe runs
    (``capabilities.py``), so a browse-time hint and the post-install probe can never disagree by
    construction — only by the metadata being stale vs. the running engine."""
    config = getattr(mi, "config", None)
    gguf = getattr(mi, "gguf", None)
    template: object | None = None
    architectures: list[str] = []
    model_type: str | None = None
    config_keys: list[str] = []
    if isinstance(config, dict):
        config_keys = [str(k) for k in config]
        architectures = [str(a) for a in (config.get("architectures") or [])]
        model_type = config.get("model_type")
        template = chat_template_from_config(config)
    if isinstance(gguf, dict):
        template = template or gguf.get("chat_template")
        if arch := gguf.get("architecture"):
            architectures = [*architectures, arch]
        ctx = gguf.get("context_length")
        if isinstance(ctx, int) and ctx > 0:
            entry.max_context = ctx
    if template is None and not architectures and not config_keys:
        return  # nothing to derive from — leave defaults
    entry.reasoning = template_implies_reasoning(template)
    entry.tool_calling = template_implies_tools(template)
    entry.vision = detect_vision(
        architectures=architectures,
        model_type=model_type,
        config_keys=config_keys,
        template=template,
    )


def _hf_config_max_context(ref: str) -> int | None:
    """Best-effort real context window from an HF repo's ``config.json``
    (``max_position_embeddings``), fetching just that small file — never weights. Detail-view only
    (the ``expand=config`` metadata omits it, unlike the GGUF header); None on any failure."""
    try:
        from huggingface_hub import hf_hub_download

        with open(hf_hub_download(repo_id=ref, filename="config.json")) as fh:
            data = json.load(fh)
    except Exception:
        return None
    mpe = data.get("max_position_embeddings")
    return mpe if isinstance(mpe, int) and mpe > 0 else None


# ── the Hugging Face adapter ─────────────────────────────────────────────────────


class HuggingFaceProvider:
    """Lists + sizes Hugging Face models, normalized onto the seam.

    ``hf_api`` and ``ram_bytes`` are injectable so the fast suite runs with a fake Hub + a fixed RAM
    (no network, no machine-dependence). In production both default to the real thing.
    """

    id = "huggingface"
    kind = "model"

    def __init__(self, *, hf_api: Any | None = None, ram_bytes: int | None = None) -> None:
        self._api = hf_api
        # An injected Hub (the fast suite) means "no network" — skip the real config.json context
        # fetch in get() (the fake fixtures supply what they mean to). True only for the real HfApi.
        self._real_hub = hf_api is None
        self._ram_bytes = ram_bytes

    def _hf(self) -> Any:
        if self._api is None:
            from huggingface_hub import HfApi  # lazy: import cost only when discovery is used

            self._api = HfApi()
        return self._api

    def _ram(self) -> int:
        if self._ram_bytes is not None:
            return self._ram_bytes
        try:
            import psutil

            return int(psutil.virtual_memory().total)
        except Exception:
            return 0

    # ── list ──────────────────────────────────────────────────────────────

    def list(self, q: CatalogQuery) -> EntryList:
        # One query per ready engine's library, then merge: ``filter`` ANDs tags, so we cannot ask
        # for "gguf OR mlx" in a single call — we union the per-library results and dedupe by ref.
        libraries = {eng: ENGINE_LIBRARY[eng] for eng in q.engines if eng in ENGINE_LIBRARY}
        if not libraries:
            return []  # no runnable engine ⇒ nothing to show (never surface an unrunnable model)
        by_ref: dict[str, CatalogEntry] = {}
        for engine, library in libraries.items():
            try:
                # ``expand`` pulls the enrichment fields (gated, lastModified, safetensors→params)
                # the lightweight listing omits — verified present in huggingface_hub 1.x.
                models = self._hf().list_models(
                    filter=library,
                    search=q.search or None,
                    sort=_HF_SORT[q.sort],
                    limit=q.limit,
                    expand=[
                        "downloads",
                        "likes",
                        "tags",
                        "pipeline_tag",
                        "gated",
                        "lastModified",
                        "safetensors",
                        # ``config`` (safetensors/MLX repos) + ``gguf`` (GGUF repos) ride the SAME
                        # list call — they carry the chat template, architectures and GGUF context
                        # inline, so browse-time capability hints (reasoning / tool_calling / vision
                        # / max_context) cost zero extra per-repo fetches. Verified present together
                        # in huggingface_hub 1.x for both the ``mlx`` and ``gguf`` library filters.
                        "config",
                        "gguf",
                    ],
                    **({"num_parameters": q.num_params} if q.num_params else {}),
                )
            except Exception as exc:
                raise CatalogError(f"hugging face listing failed: {exc}") from exc
            for mi in islice(models, q.limit):
                entry = by_ref.get(mi.id)
                if entry is None:
                    entry = self._entry_from_model_info(mi)
                    by_ref[mi.id] = entry
                if engine not in entry.engines:
                    entry.engines.append(engine)
        return list(by_ref.values())

    def _entry_from_model_info(self, mi: Any) -> CatalogEntry:
        ref = mi.id
        downloads = getattr(mi, "downloads", None)
        likes = getattr(mi, "likes", None)
        badges: dict[str, Any] = {}
        if downloads is not None:
            badges["downloads"] = downloads
        if likes is not None:
            badges["likes"] = likes
        if pt := getattr(mi, "pipeline_tag", None):
            badges["pipelineTag"] = pt
        author = getattr(mi, "author", None) or (ref.split("/")[0] if "/" in ref else "")
        entry = CatalogEntry(
            provider=self.id,
            ref=ref,
            title=ref.split("/")[-1],
            description=author,
            badges=badges,
            params=_format_params(mi),
            license=_license_from_tags(getattr(mi, "tags", None)),
            gated=bool(getattr(mi, "gated", None)),
            updated_at=_updated_iso(mi),
        )
        # When the caller fetched with ``expand=["config","gguf"]`` (list, or get's caps call), this
        # fills the capability hints from inline metadata; otherwise it is a clean no-op.
        _apply_hf_capabilities(entry, mi)
        return entry

    # ── get (variants + fit) ────────────────────────────────────────────────

    def get(self, ref: str, q: CatalogQuery) -> CatalogEntry:
        try:
            info = self._hf().model_info(ref, files_metadata=True)
        except Exception as exc:
            raise CatalogError(f"hugging face model_info failed for {ref!r}: {exc}") from exc
        entry = self._entry_from_model_info(info)
        # Capability hints for the detail view. ``files_metadata`` (needed above for sibling sizes)
        # and ``expand`` are mutually exclusive on HF's model_info, so caps ride a second small
        # request — best-effort, like the model-card blurb below.
        try:
            _apply_hf_capabilities(entry, self._hf().model_info(ref, expand=["config", "gguf"]))
        except Exception:
            pass
        # The ``expand=config`` metadata omits max_position_embeddings (unlike the GGUF header), so
        # the safetensors/MLX real context comes from one tiny config.json fetch — real Hub only.
        if entry.max_context is None and self._real_hub:
            entry.max_context = _hf_config_max_context(ref)
        siblings = list(getattr(info, "siblings", None) or [])
        ram = self._ram()
        requested = set(q.engines) if q.engines else set(ENGINE_LIBRARY)
        # The modality is the model's, not the engine's — all variants of this repo share it.
        modality = _modality_for_pipeline(getattr(info, "pipeline_tag", None))
        variants: list[CatalogVariant] = []

        # llamacpp: one variant per GGUF file (the quant), sized individually.
        if "llamacpp" in requested:
            for sib in siblings:
                name = sib.rfilename
                if not name.lower().endswith(".gguf"):
                    continue
                size = getattr(sib, "size", None)
                label = _quant_label(name)
                variants.append(
                    CatalogVariant(
                        id=name,
                        label=label,
                        engine="llamacpp",
                        modality=modality,
                        filename=name,
                        size_bytes=size,
                        fit=fit_for(size, ram),
                        quality=_variant_quality(label, "llamacpp"),
                        fit_reason=_fit_reason(size, ram),
                    )
                )
                if "llamacpp" not in entry.engines:
                    entry.engines.append("llamacpp")

        # mlx: the whole repo is one variant (mlx_lm serves the snapshot directory).
        if "mlx" in requested and _is_mlx_repo(info):
            total = sum(
                s.size
                for s in siblings
                if getattr(s, "size", None) and s.rfilename.lower().endswith(_WEIGHT_SUFFIXES)
            )
            label = _mlx_precision(ref)
            variants.append(
                CatalogVariant(
                    id="",
                    label=label,
                    engine="mlx",
                    modality=modality,
                    filename=None,
                    size_bytes=total or None,
                    fit=fit_for(total or None, ram),
                    quality=_variant_quality(label, "mlx"),
                    fit_reason=_fit_reason(total or None, ram),
                )
            )
            if "mlx" not in entry.engines:
                entry.engines.append("mlx")

        _mark_recommended(variants)
        entry.variants = variants
        # The detail view gets a one-paragraph blurb from the model card (one fetch, here only).
        if summary := _card_summary(ref):
            entry.description = summary
        return entry

    # ── install plan ──────────────────────────────────────────────────────

    def install_plan(
        self, ref: str, engine: EngineName, variant_id: str, logical_id: str
    ) -> InstallPlan:
        total_bytes: int | None = None
        filename: str | None = None
        pipeline_tag: str | None = None
        try:
            info = self._hf().model_info(ref, files_metadata=True)
            siblings = list(getattr(info, "siblings", None) or [])
            pipeline_tag = getattr(info, "pipeline_tag", None)
        except Exception:
            siblings = []
        if engine == "llamacpp":
            filename = variant_id
            for sib in siblings:
                if sib.rfilename == variant_id:
                    total_bytes = getattr(sib, "size", None)
                    break
        else:  # mlx (whole repo)
            total_bytes = (
                sum(
                    s.size
                    for s in siblings
                    if getattr(s, "size", None) and s.rfilename.lower().endswith(_WEIGHT_SUFFIXES)
                )
                or None
            )
        return InstallPlan(
            logical_id=logical_id,
            engine=engine,
            repo=ref,
            modality=_modality_for_pipeline(pipeline_tag),
            filename=filename,
            total_bytes=total_bytes,
        )


def _is_mlx_repo(info: Any) -> bool:
    if getattr(info, "library_name", None) == "mlx":
        return True
    tags = getattr(info, "tags", None) or []
    if "mlx" in tags:
        return True
    repo = getattr(info, "id", "") or ""
    return "mlx-community/" in repo or repo.startswith("mlx-community/")
