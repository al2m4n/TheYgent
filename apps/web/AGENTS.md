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
  competing products.
- Deploys to Cloudflare Pages via `.github/workflows/web.yml` on pushes touching
  `apps/web/`.
