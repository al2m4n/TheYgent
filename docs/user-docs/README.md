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

Domain-root files (`robots.txt`, `llms.txt`, `404.html`, and the favicons) live in `root/`
here — mike owns the `gh-pages` root, so the workflow copies everything in that directory
into the deploy tree on every publish. Two entries there are not plain copies:
`redirect-index.html` is mike's `set-default` template (it renders the root redirect page,
and is skipped by the copy loop), and the favicons must stay at the domain root because a
crawler resolving `/favicon.ico` never looks inside mike's version directories — without
them the docs get a generic globe in search results instead of the logo.
`site_url` in `mkdocs.yml` deliberately ends in `/latest/`: pages are served under mike's
version prefixes, and anchoring the URL at the stable alias makes every version's
canonical link and sitemap point at `/latest/`, so search rankings and AI citations never
fragment across versions.

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
