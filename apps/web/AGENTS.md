# AGENTS.md — web (marketing site)

Rules for `apps/web`, the static marketing site for theygent.ai. The repo-wide rules in
the root [AGENTS.md](../../AGENTS.md) apply first. It is not part of the product: nothing
imports it, and it imports nothing. Preview with `make web-up` (http://localhost:4321); see
[README.md](README.md) for deploy setup.

## Map

<!-- agents-map: covers=. -->

| Path | What it is |
|------|------------|
| [`index.html`](index.html), [`styles.css`](styles.css), [`main.js`](main.js) | The home page, the design system (tokens, faceted motif), progressive enhancement (theme, video facades, chapter seeking) |
| [`pages.css`](pages.css) | Styles shared by every inner page (loaded after `styles.css`) |
| [`ai-gateway/`](ai-gateway), [`apple-silicon/`](apple-silicon), [`coding-agents/`](coding-agents), [`crawlers/`](crawlers), [`private-ai/`](private-ai) | Feature landing pages, one `index.html` each |
| [`compare/`](compare) | Comparison pages (the one place other products are named; rules below) |
| [`templates/`](templates) | One page per shipped sample agent, mirroring `apps/control-plane/src/theygent_control_plane/samples/` |
| [`blog/`](blog) | Long-form posts |
| [`videos/`](videos) | Watch pages with VideoObject markup and chapter links |
| [`sitemap.xml`](sitemap.xml), [`robots.txt`](robots.txt), [`llms.txt`](llms.txt), [`_headers`](_headers), [`404.html`](404.html), [`favicon.ico`](favicon.ico) | Indexing and hosting files; `lastmod` is stamped at deploy by `.github/scripts/web_sitemap.py` |
| [`assets/`](assets) | Self-hosted Geist fonts, logos, images |
| `BingSiteAuth.xml`, `2c3a1d08fc0c4282bde9e66bf199a0d2.txt` | Bing site verification and the IndexNow key file; keep |

A new or renamed page also updates `sitemap.xml`, `llms.txt` and any nav that links it; a sample
added in the control plane gets a page under `templates/`.

- **No framework, no build step.** Plain HTML + CSS + a small progressive-enhancement
  `main.js`. The page must stay fully readable with JS off, and all motion respects
  `prefers-reduced-motion`.
- **Every local asset reference must resolve** — `.github/scripts/check_web_assets.py`
  walks all href/src/url() targets in CI and fails on a broken path. Run it after
  structural edits: `python3 .github/scripts/check_web_assets.py apps/web`.
- **Design system:** the page uses the app's visual grammar — the token surface ladder and
  faceted-polygon motif in `styles.css`, self-hosted Geist (sans for human prose, mono for
  anything a machine addresses: logical ids, ports, the binding enum, hashes). Both themes
  must work; theme is set before first paint.
- **Copy rules:** claims must stay honest — the promise is "you own every hop", never
  "no data ever leaves your machine". Brand casing is "TheYgent" in prose. Don't name
  competing products — except on the comparison pages below.
- **Comparison pages (`compare/`) are the one place other products are named**, because
  "alternative to X" is how people search. Every claim about another product cites that
  product's own source file or documentation in the page's Sources section and carries the
  date and version it was checked; re-check and re-date when either product changes. State
  where the other product is the better pick — a comparison that only flatters TheYgent is
  neither credible nor honest. Parity is reported as parity.
- **Every page is indexable by construction:** a page lives at `<slug>/index.html`, shares
  the nav/footer chrome, loads `styles.css` then `pages.css`, has one `<h1>` and a canonical
  URL, is listed in `sitemap.xml`, and keeps any FAQPage structured data word-for-word equal
  to its visible FAQ. `.github/scripts/check_web_pages.py` enforces all of it (CI and
  `make lint`). `web.yml` stamps each sitemap `lastmod` from its own page's git history.
- **Third-party requests:** pages make none until a visitor asks — the home page's video cards
  are facades that load YouTube only on click. The one exception is the watch pages under
  `videos/`, where the video is the page's main content (search engines index a video only
  there): they embed the youtube-nocookie player on load. Chapter links are `?t=<seconds>`,
  the same URLs the VideoObject `Clip` markup names, and `main.js` seeks the player in place.
- Deploys to Cloudflare Pages via `.github/workflows/web.yml` on pushes touching
  `apps/web/`.
