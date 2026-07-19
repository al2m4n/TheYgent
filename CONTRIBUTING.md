# Contributing to TheYgent

Thanks for helping build TheYgent! This page covers the mechanics of getting a change
merged. The engineering bar every change must clear lives in [AGENTS.md](AGENTS.md) —
read that too before you start.

## Before you write code

- **Bugs and small fixes:** open an issue, or go straight to a PR if it's trivial.
- **Features and behavior changes:** open an issue first so we can agree on the shape
  before you invest time. TheYgent has deliberate architectural guardrails (the
  two-plane split, the frozen binding enum, hash stability — see
  [AGENTS.md](AGENTS.md)) and PRs that cross them can't be merged, however good the
  code.

## Development setup

Everything is driven by `make`:

```bash
cp .env.example .env   # set DATABASE_URL to a reachable Postgres
make up                # inference plane :8081 · control plane :8080 · interface :5174
make test              # Python suites + interface vitest + deploy contract guards
make lint              # ruff · ty · biome · tsc · ir-types drift
```

See the [README](README.md) for engine setup (`make engines`) and the Docker/Kubernetes
run modes. Integration tests that need real engines or a real browser skip cleanly when
prerequisites are absent — the default suites run on any machine.

## What a change must satisfy

The definition of done is in [AGENTS.md](AGENTS.md). In short:

1. `make test` and `make lint` are green.
2. Docs move with behavior — user-visible changes update
   [docs/user-docs](docs/user-docs), architectural or contract changes update
   [docs/dev-docs](docs/dev-docs).
3. New behavior lands with a test; bug fixes include a regression test.
4. Frozen contracts are extended deliberately or not at all.

Each app and package has its own `AGENTS.md` with local rules.

## Pull requests

- Fork the repo, branch from `main`, and open the PR against `main`.
- Keep PRs focused — one change per PR reviews and merges faster.
- CI runs the fast Python suite, the frontend gates, and a non-blocking macOS
  integration job. On your first PR a maintainer has to approve the workflow run before
  CI starts — a standard protection on public repositories, not a judgement.

## Contributor License Agreement

Before your first PR can merge you'll be asked to sign the
[Contributor License Agreement](CONTRIBUTOR_LICENSE_AGREEMENT.md). It's short and in
plain English: you keep ownership of your work, you give the project permission to
license it (which is what makes it possible to accept contributions under the
[Sustainable Use License](LICENSE.md) at all), and you're protected from warranty and
liability claims over what you contributed.

Signing is one comment: the CLA check comments on your first PR with a sentence to reply
with, your signature is recorded once, and every later PR passes automatically.

## Security issues

Don't open public issues for vulnerabilities — see [SECURITY.md](SECURITY.md).
