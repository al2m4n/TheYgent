# TheYgent user docs

End-user documentation, built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/)
and versioned per release with [mike](https://github.com/jimporter/mike). Content lives in
`content/`; this is a standalone uv project, isolated from the application workspace.

## Working locally

```bash
make docs-serve   # live-reload preview at http://127.0.0.1:8000
make docs-build   # strict build — broken links/nav fail
```

(Or from this directory: `uv run mkdocs serve`.)

## Publishing (automated — .github/workflows/docs.yml)

- **PRs** touching `docs/user-docs/**` run a strict build check.
- **Pushes to `main`** publish a rolling `dev` version.
- **GitHub releases** publish the tag as a new docs version, repoint the `latest`
  alias at it, and attach the built site tarball to the release.

mike maintains the version tree (one subdirectory per version + a version picker in the
header) on the `gh-pages` branch; the workflow then deploys that whole tree to Cloudflare
Pages with wrangler.

Domain-root files (`robots.txt`, `llms.txt`, `404.html`, `_redirects`, `BingSiteAuth.xml` for Bing Webmaster Tools, the IndexNow key file, and the favicons) live in `root/`
here — mike owns the `gh-pages` root, so the workflow copies everything in that directory
into the deploy tree on every publish. Two entries there are not plain copies:
`redirect-index.html` is mike's `set-default` template (it renders the root redirect page,
and is skipped by the copy loop), and the favicons must stay at the domain root because a
crawler resolving `/favicon.ico` never looks inside mike's version directories — without
them the docs get a generic globe in search results instead of the logo.
Every build also writes `llms-full.txt` at the root of its version (`/latest/llms-full.txt`
once a release ships): the `hooks/llms_full.py` MkDocs hook concatenates each page's
Markdown in nav order, so an answer engine or coding agent can read the whole documentation
in one request. The domain-root `llms.txt` links to it.
Every version's canonical links and sitemap point at `/latest/`, so search rankings and AI
citations never fragment across versions. During `mike deploy`, mike's plugin rewrites
`site_url` to `site_url` + the version name; `mkdocs.yml` therefore sets the bare domain as
`site_url` and the plugin's `canonical_version: latest`. (Versions up to v1.0.1 were built
with `/latest/` already in `site_url`, which produced `/latest/<version>/…` canonicals;
`root/_redirects` sends those URLs to the pages they meant.)

One-time setup for a fork/new deployment:

1. Create a Cloudflare Pages project (direct upload) named `theygent-docs`
   (or change `CF_PAGES_PROJECT` in the workflow).
2. Add repo secrets `CLOUDFLARE_API_TOKEN` (Pages:Edit permission) and
   `CLOUDFLARE_ACCOUNT_ID`.
3. Zone settings are load-bearing for the crawler policy in `root/robots.txt`: in the
   Cloudflare dashboard for the domain, allow AI crawlers in AI Crawl Control (the
   default block returns 403 to GPTBot/ClaudeBot/etc. before robots.txt is ever read)
   and disable the managed robots.txt / Content Signals injection, which otherwise
   prepends contradictory `Disallow` groups over the deployed file.

## Writing rules

- Audience is **users**, not contributors — describe what the product does, not how the
  code is organized.
- These pages are published: no internal milestone identifiers and no competitor product
  names (see the repo's published-code hygiene policy in `CLAUDE.md`).
- Mermaid diagrams are enabled — use ```` ```mermaid ```` fences.
- New pages must be added to `nav:` in `mkdocs.yml` (strict mode fails on orphans).

## Brand assets

The logo and favicon in `content/assets/logo/` are copied from the interface
(`apps/interface/static/logo/`): `favicon.svg` (the browser-tab icon) and the two gradient
theme marks. `theygent-mark-white.svg` is a docs-only white recolor of that same mark, used as
the header logo because the Material header uses a solid blue background in both light and dark.
If the interface mark changes, re-copy the three source files and re-apply the white recolor.
