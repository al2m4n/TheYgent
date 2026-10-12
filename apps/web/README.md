# apps/web — the marketing site (theygent.ai)

The public landing page for TheYgent. It is a **static, no-build site**: hand-written
`index.html` + `styles.css` + `main.js`, with the logo and other assets under `assets/`.
No framework, no bundler, no dependencies — the folder is served exactly as it sits.

This is separate from the docs (`docs/user-docs`, served at docs.theygent.ai) and from the
app interface (`apps/interface`). It shares only the logo and brand palette.

## Preview locally

From the repository root:

```bash
make web-up      # serves http://localhost:4321 in the background (log: .run/web.log)
make web-down    # stops it
```

Any static file server works too — `make web-up` is just
`python3 -m http.server 4321 --directory apps/web`. It must be a server (not `file://`)
because the page uses root-relative `/styles.css` and `/assets/...` paths.

## Deploy

Deployment is automatic via `.github/workflows/web.yml`, to a Cloudflare Pages project
named `theygent-web` (reusing the same `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`
secrets as the docs pipeline):

- **Pull request** touching `apps/web/**` → the reference-checker
  (`.github/scripts/check_web_assets.py`) verifies every local asset path resolves, then a
  per-branch **preview** deploy is published.
- **Push to `main`** touching `apps/web/**` → the **production** site is deployed.

### One-time setup (outside this repo)

1. Create a Cloudflare Pages project named `theygent-web` (direct upload / no framework).
2. Point the `theygent.ai` apex (and `www`) DNS at that Pages project.

## Files

| Path | Purpose |
|---|---|
| `index.html` | The home page. |
| `private-ai/`, `apple-silicon/` | Guide pages: private and offline use (with every network touchpoint), and MLX on a Mac. |
| `compare/` | The comparison hub and one page per compared product — dated and sourced. |
| `404.html` | The not-found page (Cloudflare Pages serves it with a 404 status). |
| `styles.css` | Tokens, nav, sections, cards, FAQ, footer (brand tokens mirror the app interface). |
| `pages.css` | Inner-page furniture (page hero, prose, comparison tables), loaded after `styles.css`. |
| `main.js` | Small progressive enhancements (nav, scroll reveal, theme). |
| `assets/logo/` | The TheYgent logo marks (light, dark, white) + favicon, copied from the interface. |
| `assets/og-image.png` | Social share card. |
| `_headers` | Cloudflare Pages caching + security headers. |
| `robots.txt`, `sitemap.xml`, `llms.txt` | Crawl hints and the machine-readable site summary; `.github/scripts/check_web_pages.py` keeps the sitemap, canonicals, and FAQ structured data in step with the pages. |

## Conventions

- **Brand name** is written **TheYgent** (capital T + Y) in all copy.
- Keep it dependency-free and self-contained; if a web font is used it degrades to a system stack.
- The published-code hygiene rules apply here too: no internal milestone tags, and no
  competitor product names outside the comparison pages under `compare/` — those name the
  products they compare, cite a source for every claim, and carry the date they were checked
  (see [AGENTS.md](AGENTS.md)). Everywhere else, describe capabilities on their own terms.
