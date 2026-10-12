# AGENTS.md — web (marketing site)

Rules for `apps/web`, the static marketing site for theygent.ai. The repo-wide rules in
the root [AGENTS.md](../../AGENTS.md) apply first.

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
