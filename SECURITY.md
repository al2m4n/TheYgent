# Security Policy

TheYgent's core promise is sovereignty over your models and data — security reports are
taken seriously and handled with priority.

## Reporting a vulnerability

Please do **not** report security vulnerabilities through public GitHub issues,
discussions, or pull requests.

Instead, use GitHub's private vulnerability reporting: open the repository's
**Security** tab and click **Report a vulnerability**. You'll get an acknowledgement
within a few days, and we'll work with you on a fix and coordinated disclosure.

If you can't use private reporting, open a plain issue saying only that you have a
security report and need a private channel — no details — and a maintainer will follow
up.

## Scope and supported versions

The latest release and the current `main` branch are supported with security fixes.

Vulnerabilities in the underlying engines (llama.cpp, MLX, vLLM), in models, or in
third-party MCP servers belong upstream with those projects — but reports about how
TheYgent wires, isolates, or authenticates them are absolutely in scope.
