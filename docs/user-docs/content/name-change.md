# The name change: TheYgent is now Ertir

Ertir is the new name of TheYgent. It is the same project: the same code, the same maintainer and the same license ([Sustainable Use License](https://github.com/al2m4n/ertir/blob/main/LICENSE.md)). Version 2.0.0 is the first release under the new name.

## What changed

- **The name.** The app, these docs and the website say Ertir.
- **The website and docs addresses.** The website is [ertir.dev](https://ertir.dev) and the docs are at [docs.ertir.dev](https://docs.ertir.dev). Links to `theygent.ai` and `docs.theygent.ai` redirect to the matching page on the new addresses.
- **The GitHub repository.** It is [github.com/al2m4n/ertir](https://github.com/al2m4n/ertir). GitHub redirects the old `al2m4n/TheYgent` URLs, so existing clones, issues and pull requests keep working.

## What stays the same

- **Your installation and your data.** Agents, versions, runs, conversations, models, credentials and settings are untouched. There is nothing to migrate.
- **Technical names.** They keep the old spelling, so scripts and configuration you already have keep working:

| What | Name |
|------|------|
| Environment variables | `THEYGENT_*` (see [environment variables](reference/environment.md)) |
| Data folder | `~/.theygent` |
| Webhook signature header | `X-TheYgent-Signature` |
| AI gateway key header | `X-TheYgent-Gateway-Key` |
| Python packages and modules, npm packages, Docker and Kubernetes names | `theygent…` |

If a later release renames any of these, its release notes say how to switch, and the old names keep working during the change.
