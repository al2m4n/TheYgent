# AGENTS.md — .github

CI, publishing pipelines, the PR template, and the small scripts the workflows run. The repo-wide
rules in the root [AGENTS.md](../AGENTS.md) apply first; CI gates are described in
[deployment.md](../docs/dev-docs/deployment.md) ("CI gates", "Publish pipelines") and
[testing.md](../docs/dev-docs/testing.md).

## Map

<!-- agents-map: covers=. -->
<!-- agents-map: covers=workflows -->
<!-- agents-map: covers=scripts -->

| Path | Runs on | What it does |
|------|---------|--------------|
| [`workflows/ci.yml`](workflows/ci.yml) | Every PR, pushes to `main` | `fast`: the context-file check (`scripts/check_agent_docs.py`), ruff lint and format, ty (non-blocking), the fast pytest suites of `packages/ir`, inference plane, control plane, AI gateway and MCP server, and `deploy/tests`. `frontend`: regenerate `packages/ir-types` and fail on drift, type-check it, then Biome, build and Vitest for the interface. `integration` (non-blocking, macOS): real llama.cpp and MLX, plus the cross-process control plane → inference → MLX test |
| [`workflows/docs.yml`](workflows/docs.yml) | PRs touching the user docs; `main`; releases | Strict MkDocs build on PRs; publishes the tagged version (moving `latest`) on release via mike; on `main` and on release, deploys the `gh-pages` tree plus `docs/user-docs/root/` to Cloudflare Pages after repairing the early versions' canonicals; pings IndexNow for changed pages on release |
| [`workflows/web.yml`](workflows/web.yml) | PRs touching `apps/web`; `main`; releases | Asset and page checks, preview deploys on PRs, production on `main` and on release; stamps sitemap `lastmod` |
| [`workflows/ai-gateway-pricing.yml`](workflows/ai-gateway-pricing.yml) | Weekly, manual | Regenerates the AI gateway's `pricing.json`; opens a PR when prices changed |
| [`workflows/cla.yml`](workflows/cla.yml) | `pull_request_target` | The CLA check; signatures live on the `cla-signatures` branch. Never checks out or runs PR code |
| [`scripts/check_web_assets.py`](scripts/check_web_assets.py) | `web.yml`, pre-commit | Every local href/src/url() in `apps/web` resolves |
| [`scripts/check_web_pages.py`](scripts/check_web_pages.py) | `web.yml`, pre-commit | Every web page is indexable: one `<h1>`, canonical URL, sitemap entry, FAQ markup equal to the visible FAQ |
| [`scripts/check_agent_docs.py`](scripts/check_agent_docs.py) | `ci.yml`, pre-commit | The context files are in step with the tree: `CLAUDE.md` stubs, root links, relative links and anchors, and every file in each `agents-map: covers=` directory named in its map |
| [`scripts/web_sitemap.py`](scripts/web_sitemap.py) | `web.yml` | Stamps sitemap `lastmod` from git history; lists changed URLs |
| [`scripts/docs_indexnow.py`](scripts/docs_indexnow.py) | `docs.yml` | Lists the docs URLs a release changed |
| [`scripts/docs_repair_canonicals.py`](scripts/docs_repair_canonicals.py) | `docs.yml` | Rewrites the `/latest/<version>/` self-URLs of the versions `_redirects` names to `/latest/` in the deploy tree; guarded by `deploy/tests/test_docs_publish.py` |
| [`pull_request_template.md`](pull_request_template.md) | Every PR | The checklist every PR fills in, including the context-docs line |

## Rules

- **Keep CI and pre-commit in step.** [`.pre-commit-config.yaml`](../.pre-commit-config.yaml)
  mirrors the `fast` and `frontend` gates so `make lint` catches locally what CI would. A gate
  added here gets its hook there, and the other way round.
- **Least privilege.** Workflows run with read-only tokens unless a job needs more; a
  `pull_request_target` workflow never checks out PR code.
- **The default suites stay runnable anywhere.** Real-engine jobs are separate and
  non-blocking; a new gate must not require hardware or secrets on PRs from forks.
- Publishing secrets: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`.
