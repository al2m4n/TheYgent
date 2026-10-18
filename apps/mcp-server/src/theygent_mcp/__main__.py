"""``theygent-mcp``: serve TheYgent to a coding agent, or print how to connect one."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from theygent_mcp.config import DEFAULT_HTTP_PORT, Config
from theygent_mcp.server import build_server

#: The TheYgent checkout this server runs from (apps/mcp-server/src/theygent_mcp/ → the root).
_CHECKOUT = Path(__file__).resolve().parents[4]
CLIENTS = ("claude", "codex", "opencode")


def launch_command() -> list[str]:
    """How a coding tool starts this server over stdio. ``--project`` (not ``--directory``) keeps
    the tool's working directory, so relative paths given to ``add_rag_files`` resolve against the
    project the developer is working in."""
    return ["uv", "run", "--project", str(_CHECKOUT), "--package", "theygent-mcp", "theygent-mcp"]


def setup_text(client: str) -> str:
    cmd = launch_command()
    key = "THEYGENT_API_KEY=<your TheYgent API key>"
    if client == "claude":
        return (
            "Claude Code — add TheYgent for every project (drop `--scope "
            "user` for this one only):\n\n"
            f"  claude mcp add theygent --scope user -e {shlex.quote(key)} -- {shlex.join(cmd)}\n\n"
            "Then run `claude mcp list` to check it connects. Long agent runs "
            "can exceed Claude Code's\n"
            "MCP tool timeout; raise it with MCP_TOOL_TIMEOUT=600000 (milliseconds) if they do.\n"
        )
    if client == "codex":
        toml_args = ", ".join(json.dumps(a) for a in cmd[1:])
        return (
            "Codex — add TheYgent:\n\n"
            f"  codex mcp add theygent --env {shlex.quote(key)} -- {shlex.join(cmd)}\n\n"
            "Codex stops a tool call after 60 seconds by default; give agent runs room in\n"
            "~/.codex/config.toml:\n\n"
            "  [mcp_servers.theygent]\n"
            f'  command = "{cmd[0]}"\n'
            f"  args = [{toml_args}]\n"
            "  tool_timeout_sec = 600\n"
            '  env = { THEYGENT_API_KEY = "<your TheYgent API key>" }\n'
        )
    if client == "opencode":
        config = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "theygent": {
                    "type": "local",
                    "command": cmd,
                    "enabled": True,
                    "environment": {"THEYGENT_API_KEY": "<your TheYgent API key>"},
                }
            },
        }
        return (
            "OpenCode — add this to opencode.json (in the project, or "
            "~/.config/opencode/opencode.json):\n\n" + json.dumps(config, indent=2) + "\n"
        )
    raise ValueError(client)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="theygent-mcp",
        description="Serve TheYgent (agents, local models, retrieval) to coding agents over MCP.",
    )
    sub = parser.add_subparsers(dest="command")
    setup = sub.add_parser("setup", help="print how to connect a coding tool to this server")
    setup.add_argument("client", choices=(*CLIENTS, "all"), nargs="?", default="all")
    parser.add_argument(
        "--http", action="store_true", help="serve streamable HTTP instead of stdio"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    args = parser.parse_args(argv)

    if args.command == "setup":
        clients = CLIENTS if args.client == "all" else (args.client,)
        print("\n".join(setup_text(c) for c in clients), end="")
        print(
            "\nThe API key comes from TheYgent: your profile menu → API keys. Without it, the\n"
            "model tools still work; agent, run and retrieval tools ask for it.\n"
        )
        return

    server = build_server(Config.from_env())
    if args.http:
        server.run(transport="http", host=args.host, port=args.port, show_banner=False)
    else:
        server.run(show_banner=False)


if __name__ == "__main__":
    sys.exit(main())
