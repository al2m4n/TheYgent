"""The TheYgent MCP server: TheYgent's platform as tools for coding agents."""

from __future__ import annotations

import asyncio
import glob
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from theygent_mcp import guide
from theygent_mcp.config import Config
from theygent_mcp.planes import Planes

INSTRUCTIONS = """\
TheYgent is the user's own AI platform: local models (MLX / llama.cpp on their machine), saved
agents built from those models, and retrieval sources over their documents. Use it to:

- **Offload work to a local model** with `ask_model`: summaries, first drafts, classification,
  extraction, rewriting, or anything that should not leave the machine. Pass long logs or files
  as `files`: the local model reads them and only its answer reaches your context.
- **Run the user's saved agents** with `list_agents` and `run_agent` when one fits the task.
- **Build new agents** for repeatable work: read `agent_authoring_guide`, write the agent JSON,
  check it with `validate_agent`, try it with `run_graph`, then publish it with `create_agent`.
- **Search indexed documents** with `search_rag`; index files you are working with using
  `create_rag_source` and `add_rag_files`.

Model ids are TheYgent logical ids from `list_models`, never engine names.
"""

#: The largest document the control plane accepts in one upload.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
#: How many files one `add_rag_files` call indexes; a bigger tree is indexed in several calls.
MAX_FILES_PER_CALL = 500
#: Per-file results are listed in full up to this many files; past it only the misses are.
_LISTED_RESULTS = 50
#: Directories a tree walk never descends into: VCS metadata, dependencies, build output, caches.
_SKIP_DIRS = {
    "node_modules",
    "__pycache__",
    "venv",
    "dist",
    "build",
    "target",
    "out",
    "coverage",
    "site-packages",
}

#: The most file text `ask_model` inlines into one prompt: about 64k tokens, beyond which a
#: typical local model's context is exceeded anyway.
MAX_INLINE_BYTES = 256 * 1024
#: How much of a model's thinking `ask_model` returns when it produced no answer.
_REASONING_TAIL = 4000

AgentRef = Annotated[str, Field(description="The agent's id, or its exact name.")]
SourceRef = Annotated[str, Field(description="The source's id (rag_…), or its exact name.")]


def build_server(config: Config, transport: httpx.AsyncBaseTransport | None = None) -> FastMCP:
    """The server with every tool registered. ``transport`` replaces the network in tests."""
    planes = Planes(config, transport)

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield
        finally:
            await planes.aclose()

    mcp = FastMCP("TheYgent", instructions=INSTRUCTIONS, lifespan=lifespan)

    # ── models ───────────────────────────────────────────────────────────────────────────────

    async def _models() -> list[dict[str, Any]]:
        body = await planes.inference_admin("GET", "/admin/models")
        models = body.get("models", []) if isinstance(body, dict) else []
        out = []
        for m in models:
            binding = m.get("binding") or {}
            out.append(
                {
                    "id": m.get("logicalId"),
                    "engine": binding.get("binding"),
                    "modality": binding.get("modality") or "chat",
                    "model": binding.get("model"),
                }
            )
        return out

    @mcp.tool
    async def list_models() -> dict[str, Any]:
        """List the models registered in TheYgent: each one's logical `id` (what `ask_model` and
        agents use), its `engine` (mlx, llamacpp, vllm, or openai-compatible for a hosted API),
        its `modality` (chat, vision, embeddings, …) and the weights it serves. Also reports which
        chat model `ask_model` uses by default."""
        models = await _models()
        return {"models": models, "default_chat_model": _default_model(models)}

    def _default_model(models: list[dict[str, Any]]) -> str | None:
        if config.default_model:
            return config.default_model
        chat = [m["id"] for m in models if m["modality"] in ("chat", "vision") and m["id"]]
        return chat[0] if chat else None

    @mcp.tool
    async def ask_model(
        prompt: Annotated[
            str,
            Field(
                description="The full request for the model, including any text or code it needs."
            ),
        ],
        model: Annotated[
            str | None,
            Field(description="A logical id from list_models; omit for the default chat model."),
        ] = None,
        system: Annotated[str | None, Field(description="Optional system prompt.")] = None,
        max_tokens: Annotated[
            int,
            Field(
                ge=1,
                le=32768,
                description="Cap on generated tokens. Reasoning models need room to think.",
            ),
        ] = 2048,
        temperature: Annotated[float | None, Field(ge=0, le=2)] = None,
        files: Annotated[
            list[str] | None,
            Field(
                description="Local files to append to the prompt. This server reads them and "
                "sends them only to the model, so a long log or file never has to pass through "
                "your own context."
            ),
        ] = None,
        think: Annotated[
            bool | None,
            Field(
                description="false turns a reasoning model's thinking off for a faster, shorter "
                "answer; omit for the model's default."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Send one prompt to a model on the user's machine (or a hosted model they registered)
        and get its answer. Use it to offload self-contained work — summarizing a file, drafting
        text, classifying or extracting data, a second opinion — especially when the content is
        private or the task is cheap. The model sees only `prompt` and the `files` you name."""
        chosen = model or _default_model(await _models())
        if not chosen:
            raise ToolError(
                "No chat model is registered in TheYgent. Install one from Registries → Add model, "
                "or set THEYGENT_MCP_DEFAULT_MODEL."
            )
        if files:
            prompt += await asyncio.to_thread(_inline_files, files)
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        body: dict[str, Any] = {"model": chosen, "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            body["temperature"] = temperature
        if think is not None:
            # The local engines read the chat template's thinking switch from here.
            body["chat_template_kwargs"] = {"enable_thinking": think}
        result = await planes.inference(
            "POST", "/chat/completions", json=body, timeout_s=config.run_timeout_s
        )
        choice = (result.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        answer = (message.get("content") or "").strip()
        out: dict[str, Any] = {
            "model": chosen,
            "answer": answer,
            "finish_reason": choice.get("finish_reason"),
            "usage": result.get("usage"),
        }
        if not answer:
            # Only an empty answer needs the thinking that used up the budget; otherwise it would
            # just fill the caller's context.
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            if reasoning:
                out["reasoning"] = reasoning[-_REASONING_TAIL:]
            if choice.get("finish_reason") == "length":
                out["note"] = (
                    "The model used every token before answering (it may have spent them "
                    "thinking). Call again with a larger max_tokens, or think=false."
                )
        return out

    # ── agents ───────────────────────────────────────────────────────────────────────────────

    async def _agent_id(ref: str) -> str:
        """An agent id from an id or a display name: coding agents often know an agent only by
        the name the user called it."""
        body = await planes.control("GET", "/agents", params={"limit": 200})
        agents = body.get("agents", []) if isinstance(body, dict) else []
        if any(a.get("id") == ref for a in agents):
            return ref
        named = [a.get("id") for a in agents if (a.get("name") or "").lower() == ref.lower()]
        if len(named) > 1:
            raise ToolError(f"Several agents are named {ref!r} ({', '.join(named)}); pass an id.")
        return named[0] if named else ref

    @mcp.tool
    async def list_agents(
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """List the user's published TheYgent agents, newest first: `id`, `name`, latest
        `version` and how many versions exist. Run one with `run_agent`; read its graph with
        `get_agent`."""
        body = await planes.control("GET", "/agents", params={"limit": limit})
        agents = body.get("agents", []) if isinstance(body, dict) else []
        return {
            "agents": [
                {
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "latest_version": a.get("latest_version"),
                    "versions": a.get("version_count"),
                    "updated_at": a.get("updated_at"),
                }
                for a in agents
            ]
        }

    @mcp.tool
    async def get_agent(
        agent_id: AgentRef,
        version: Annotated[str | None, Field(description="A version; omit for the latest.")] = None,
    ) -> dict[str, Any]:
        """Read a published agent: its versions and the full agent document (IR) of one version —
        what you edit to publish a new version."""
        agent_id = await _agent_id(agent_id)
        detail = await planes.control("GET", f"/agents/{quote(agent_id, safe='')}")
        versions = detail.get("versions") or []
        chosen = version or (versions[0].get("version") if versions else None)
        if not chosen:
            return {"agent": detail, "ir": None}
        stored = await planes.control(
            "GET", f"/agents/{quote(agent_id, safe='')}/versions/{quote(chosen, safe='')}"
        )
        return {
            "id": detail.get("id"),
            "name": detail.get("name"),
            "versions": [v.get("version") for v in versions],
            "version": chosen,
            "ir": stored.get("ir") if isinstance(stored, dict) else stored,
        }

    @mcp.tool
    async def run_agent(
        agent_id: AgentRef,
        input: Annotated[
            Any,
            Field(
                description="The agent's input: text, or an object with the fields its "
                "prompts read ($in.in.<field>)."
            ),
        ],
        version: Annotated[
            str | None, Field(description="Pin a version; omit for the latest.")
        ] = None,
        session_id: Annotated[
            str | None, Field(description="Continue a TheYgent chat session.")
        ] = None,
    ) -> dict[str, Any]:
        """Run a published TheYgent agent and wait for its output. Returns `run_id`, `status` and
        `output`; `get_run` shows a run again later. Runs on the user's own models and tools."""
        agent_id = await _agent_id(agent_id)
        body: dict[str, Any] = {"input": input, "stream": False}
        if version:
            body["version"] = version
        if session_id:
            body["session_id"] = session_id
        result = await planes.control(
            "POST",
            f"/agents/{quote(agent_id, safe='')}/runs",
            json=body,
            timeout_s=config.run_timeout_s,
        )
        return _run_result(result)

    @mcp.tool
    async def run_graph(
        ir: Annotated[dict[str, Any], Field(description="A full agent document (IR).")],
        input: Annotated[Any, Field(description="The input to run it with.")],
    ) -> dict[str, Any]:
        """Run an agent document without publishing it — the way to test an agent you are
        building. Nothing is saved except the run itself. Returns `run_id`, `status`, `output`."""
        result = await planes.control(
            "POST",
            "/graphs/runs",
            json={"ir": ir, "input": input, "stream": False},
            timeout_s=config.run_timeout_s,
        )
        return _run_result(result)

    @mcp.tool
    async def get_run(run_id: str) -> dict[str, Any]:
        """A run's current `status` (running, completed, failed, waiting), its `output` or
        `error`, and when it started and finished."""
        run = await planes.control("GET", f"/runs/{quote(run_id, safe='')}")
        keys = (
            "id",
            "status",
            "output",
            "error",
            "graph_id",
            "graph_version",
            "awaiting_node",
            "created_at",
            "completed_at",
        )
        return {k: run.get(k) for k in keys if k in run}

    @mcp.tool
    async def agent_authoring_guide(
        node_types: Annotated[
            list[str] | None,
            Field(
                description=(
                    'Node types to detail with their full config schema, e.g. ["llm", "rag"]. '
                    "Omit for the guide plus a summary of every type."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """How to write a TheYgent agent: the document format, how values flow (`$in`), common
        patterns, a full example, and every runnable node type's kind, default ports and default
        config. Read this before `create_agent` or `run_graph`."""
        if node_types:
            return {"node_types": guide.node_types(node_types, include_schema=True)}
        return {"guide": guide.GUIDE, "node_types": guide.node_types()}

    @mcp.tool
    async def validate_agent(
        ir: Annotated[dict[str, Any], Field(description="A full agent document (IR).")],
    ) -> dict[str, Any]:
        """Check an agent document without running it: the same shape and graph rules the platform
        applies when publishing. Returns `valid` and, if not, the `problems` to fix."""
        return guide.validate(ir)

    @mcp.tool
    async def create_agent(
        ir: Annotated[
            dict[str, Any],
            Field(description="A full agent document (IR); its `id` becomes the agent id."),
        ],
        name: Annotated[
            str | None, Field(description="A display name; defaults to the document's `name`.")
        ] = None,
    ) -> dict[str, Any]:
        """Publish a new agent from a document. It then appears in the user's TheYgent Agents page
        and can be run with `run_agent`. If the id is taken, use `publish_agent_version`."""
        body: dict[str, Any] = {"ir": ir}
        if name:
            body["name"] = name
        created = await planes.control("POST", "/agents", json=body)
        return _agent_summary(created)

    @mcp.tool
    async def publish_agent_version(
        agent_id: str,
        ir: Annotated[
            dict[str, Any],
            Field(
                description="The new document; its `id` must equal agent_id and its "
                "`version` must be new."
            ),
        ],
    ) -> dict[str, Any]:
        """Publish a new version of an existing agent (bump the document's `version`). Earlier
        versions stay runnable."""
        published = await planes.control(
            "POST", f"/agents/{quote(agent_id, safe='')}/versions", json={"ir": ir}
        )
        return _agent_summary(published)

    @mcp.tool
    async def list_samples() -> dict[str, Any]:
        """List TheYgent's ready-made sample agents (title, what they show, the model slots to fill,
        whether installed). Install one with `install_samples` for a working starting point."""
        body = await planes.control("GET", "/samples")
        samples = body.get("samples", body) if isinstance(body, dict) else body
        return {"samples": samples}

    @mcp.tool
    async def install_samples(
        ids: list[str],
        models: Annotated[
            dict[str, dict[str, str]],
            Field(
                description=(
                    "Each model slot the samples need → "
                    '{"logical_id": <id from list_models>, "binding": <its engine>}.'
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Install sample agents (with their connections and retrieval sources) as the user's own,
        filling their model slots with the user's models."""
        return await planes.control(
            "POST", "/samples/install", json={"ids": ids, "models": models}, timeout_s=300
        )

    # ── retrieval sources ────────────────────────────────────────────────────────────────────

    async def _source_id(ref: str) -> str:
        """A retrieval source id (``rag_…``) from an id or a display name."""
        if ref.startswith("rag_"):
            return ref
        body = await planes.control("GET", "/rag/sources", params={"limit": 200})
        sources = body.get("sources", []) if isinstance(body, dict) else body or []
        named = [s.get("id") for s in sources if (s.get("name") or "").lower() == ref.lower()]
        if len(named) == 1:
            return named[0]
        if named:
            raise ToolError(f"Several retrieval sources are named {ref!r}; pass an id.")
        names = ", ".join(repr(s.get("name")) for s in sources) or "none yet"
        raise ToolError(
            f"No retrieval source has the id or name {ref!r}. The user's sources: {names}."
        )

    @mcp.tool
    async def list_rag_sources() -> dict[str, Any]:
        """List the user's retrieval (RAG) sources: id, name, kind (upload or crawl), embedding
        model, and ingest status. Search one with `search_rag`."""
        body = await planes.control("GET", "/rag/sources", params={"limit": 200})
        return body if isinstance(body, dict) else {"sources": body}

    @mcp.tool
    async def search_rag(
        source_id: SourceRef,
        query: str,
        top_k: Annotated[int, Field(ge=1, le=50)] = 5,
    ) -> dict[str, Any]:
        """Search a retrieval source with the same hybrid (semantic + keyword) retrieval TheYgent's
        agents use. Returns the best-matching passages with their documents and scores."""
        source_id = await _source_id(source_id)
        return await planes.control(
            "POST",
            f"/rag/sources/{quote(source_id, safe='')}/query",
            json={"query": query, "top_k": top_k},
        )

    @mcp.tool
    async def create_rag_source(
        name: str,
        embedding_model: Annotated[
            str,
            Field(
                description="An embeddings model's logical id from list_models "
                "(modality embeddings)."
            ),
        ],
        kind: Literal["upload", "crawl"] = "upload",
        root_url: Annotated[
            str | None, Field(description="For a crawl source: the site to crawl.")
        ] = None,
        max_pages: Annotated[int | None, Field(ge=1)] = None,
    ) -> dict[str, Any]:
        """Create a retrieval source. An `upload` source is filled with `add_rag_files` or
        `add_rag_text`; a `crawl` source indexes a website once you call `ingest_rag_source`."""
        config_: dict[str, Any] = {}
        if kind == "crawl":
            if not root_url:
                raise ToolError("A crawl source needs root_url.")
            config_["root_url"] = root_url
            if max_pages:
                config_["max_pages"] = max_pages
        return await planes.control(
            "POST",
            "/rag/sources",
            json={
                "name": name,
                "kind": kind,
                "embedding_model": embedding_model,
                "config": config_,
            },
        )

    async def _upload(source_id: str, filename: str, data: bytes, mime: str) -> Any:
        return await planes.control(
            "POST",
            f"/rag/sources/{quote(source_id, safe='')}/documents",
            params={"filename": filename},
            content=data,
            headers={"Content-Type": mime},
            timeout_s=300,
        )

    @mcp.tool
    async def add_rag_files(
        source_id: SourceRef,
        paths: Annotated[
            list[str],
            Field(
                description=(
                    "Local files, directories (indexed recursively, skipping hidden, dependency "
                    'and build directories) or glob patterns such as "docs/**/*.md". Relative '
                    "paths resolve against this server's working directory — normally the "
                    "project you are working in."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Index local files into an `upload` retrieval source: docs, specs, source code (code
        indexes as plain text), PDFs and office documents. TheYgent chunks and embeds them in
        the background (`get_rag_source` shows progress), after which `search_rag` and the
        user's agents can search them. Re-adding a file replaces its earlier copy. Files whose
        type TheYgent can't read are skipped."""
        source_id = await _source_id(source_id)
        files, misses = await asyncio.to_thread(_expand_paths, paths)
        results: list[dict[str, Any]] = list(misses)
        uploaded = skipped = 0
        for path, name in files:
            read = await asyncio.to_thread(_read_local_file, str(path))
            if isinstance(read, str):
                results.append({"path": name, "skipped": read})
                skipped += 1
                continue
            _, data, mime = read
            try:
                await _upload(source_id, name, data, mime)
            except ToolError as exc:
                if "unsupported_document" in str(exc):
                    results.append({"path": name, "skipped": "TheYgent can't read this type"})
                    skipped += 1
                else:
                    results.append({"path": name, "error": str(exc)})
                continue
            uploaded += 1
            results.append({"path": name, "uploaded": True, "bytes": len(data)})
        failed = sum(1 for r in results if "error" in r)
        out: dict[str, Any] = {"uploaded": uploaded, "skipped": skipped, "failed": failed}
        if len(results) > _LISTED_RESULTS:
            results = [r for r in results if not r.get("uploaded")][:_LISTED_RESULTS]
        out["results"] = results
        if len(files) == MAX_FILES_PER_CALL:
            out["note"] = (
                f"Stopped at {MAX_FILES_PER_CALL} files. Index the rest with narrower paths "
                "or patterns."
            )
        return out

    @mcp.tool
    async def add_rag_text(
        source_id: SourceRef,
        filename: Annotated[str, Field(description="A name for the document, e.g. notes.md.")],
        text: str,
    ) -> dict[str, Any]:
        """Add a document from text (notes, a generated summary, an API response) to an `upload`
        retrieval source."""
        source_id = await _source_id(source_id)
        # The control plane picks the parser from the extension; a bare name is Markdown.
        if not Path(filename).suffix:
            filename += ".md"
        await _upload(source_id, filename, text.encode(), "text/markdown")
        return {"uploaded": True, "filename": filename, "bytes": len(text.encode())}

    @mcp.tool
    async def ingest_rag_source(source_id: SourceRef) -> dict[str, Any]:
        """Start (or re-run) a `crawl` source's crawl. Unchanged pages are skipped."""
        source_id = await _source_id(source_id)
        await planes.control("POST", f"/rag/sources/{quote(source_id, safe='')}:ingest")
        return {"started": True, "source_id": source_id}

    @mcp.tool
    async def get_rag_source(source_id: SourceRef) -> dict[str, Any]:
        """A retrieval source's status and its documents, each with its ingest status, chunk count
        and any error."""
        source_id = await _source_id(source_id)
        source = await planes.control("GET", f"/rag/sources/{quote(source_id, safe='')}")
        documents = await planes.control(
            "GET", f"/rag/sources/{quote(source_id, safe='')}/documents"
        )
        return {"source": source, "documents": documents}

    return mcp


def _expand_paths(raw_paths: list[str]) -> tuple[list[tuple[Path, str]], list[dict[str, Any]]]:
    """The files named by paths, directories and glob patterns, each with the name it is indexed
    under (its path relative to the working directory when it lies inside it, so two
    `index.ts` files stay two documents), plus an entry for every path that matched nothing."""
    cwd = Path.cwd()
    files: dict[Path, str] = {}
    misses: list[dict[str, Any]] = []

    def add(path: Path) -> bool:
        if len(files) >= MAX_FILES_PER_CALL:
            return False
        path = path.resolve()
        files.setdefault(
            path, str(path.relative_to(cwd)) if path.is_relative_to(cwd) else path.name
        )
        return True

    for raw in raw_paths:
        expanded = os.path.expanduser(raw)
        if glob.has_magic(expanded):
            matches = sorted(glob.glob(expanded, recursive=True))
            found = [Path(m) for m in matches if Path(m).is_file()]
            if not found:
                misses.append({"path": raw, "error": "no files match this pattern"})
            for path in found:
                if not add(path):
                    break
            continue
        path = Path(expanded)
        if path.is_dir():
            for root, dirs, names in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS)
                for name in sorted(names):
                    if not name.startswith(".") and not add(Path(root) / name):
                        break
            continue
        if not path.is_file():
            misses.append({"path": raw, "error": "not a file"})
            continue
        add(path)
    return list(files.items()), misses


def _inline_files(paths: list[str]) -> str:
    """Local text files as fenced blocks for a prompt, within the inline budget."""
    blocks: list[str] = []
    budget = MAX_INLINE_BYTES
    for raw in paths:
        path = Path(os.path.expanduser(raw))
        if not path.is_file():
            raise ToolError(f"{raw}: not a file.")
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            raise ToolError(f"{raw}: a binary file; ask_model sends text only.")
        if len(data) > budget:
            raise ToolError(
                f"The files add up to more than {MAX_INLINE_BYTES // 1024} KiB, more than a local "
                "model's context holds. Send fewer or smaller files, or index them with "
                "add_rag_files and search_rag."
            )
        budget -= len(data)
        text = data.decode("utf-8", errors="replace")
        blocks.append(f"\n\n--- {raw} ---\n```\n{text}\n```")
    return "".join(blocks)


def _read_local_file(raw: str) -> tuple[str, bytes, str] | str:
    """A local file's name, bytes and mime type, or why it can't be uploaded."""
    path = Path(os.path.expanduser(raw)).resolve()
    if not path.is_file():
        return "not a file"
    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        return f"{size} bytes is over the 50 MiB limit"
    if size == 0:
        return "empty file"
    return path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "text/plain"


def _run_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"output": result}
    return {
        "run_id": result.get("runId") or result.get("run_id"),
        "status": result.get("status"),
        "output": result.get("output"),
    }


def _agent_summary(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"result": body}
    versions = body.get("versions") or []
    return {
        "id": body.get("id"),
        "name": body.get("name"),
        "versions": [v.get("version") for v in versions if isinstance(v, dict)],
    }
