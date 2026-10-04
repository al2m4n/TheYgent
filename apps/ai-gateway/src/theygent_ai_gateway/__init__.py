"""TheYgent AI gateway — an Anthropic-Messages-compatible proxy that sits between a coding agent
(Claude Code) and its models, observing or routing each request.

The gateway lives in the user's trust domain: it sees prompts, code, and credentials, so it never
persists credentials and never sends prompt content to the control plane — only request metadata.
"""
