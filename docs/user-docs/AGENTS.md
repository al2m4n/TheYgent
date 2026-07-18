# AGENTS.md — user docs

Rules for `docs/user-docs`, the end-user documentation site (MkDocs Material, published
versioned per release to https://docs.theygent.ai/). The repo-wide rules in the root
[AGENTS.md](../../AGENTS.md) apply first.

- **Strict mode is non-negotiable:** `make docs-build` runs `mkdocs build --strict` —
  orphan pages and broken links fail. Every new page needs a `nav:` entry in `mkdocs.yml`.
- **Audience is users, not contributors.** Pages describe what the product does — never
  module paths or internal architecture beyond the user-visible plane split. Contributor
  material belongs in [docs/dev-docs](../dev-docs/README.md).
- **These pages are published:** no competing product names, no internal plan references.
  Brand casing is "TheYgent" in prose; lowercase `theygent` only in code blocks, URLs, and
  env vars.
- **Honesty rules mirror the product:** never overclaim privacy ("everything runs where
  you point it, and you own every hop" — not "no data ever leaves your machine"); label
  unverified paths (e.g. vLLM as experimental); document limitations plainly.
- **Examples are executable documentation:** curl examples use real endpoint paths, the
  default ports (:8080 control, :8081 inference, :5174 interface), and real error codes.
- **When product behavior changes, the matching page changes in the same PR** — the page
  map under `content/` is organized by feature area (getting-started, concepts, models,
  chat, rag, mcp, running, reference).
- This project is deliberately **not** part of the root uv workspace — its own lockfile
  isolates the docs toolchain. `make docs-serve` for live preview. Releases publish with
  `mike`; published versions are immutable and `latest` tracks the newest release.
