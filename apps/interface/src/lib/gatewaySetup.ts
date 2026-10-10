// Copy-ready setup for pointing a client at the AI gateway, per client and credential profile.
//
// What every Claude Code setup carries, whatever the profile:
// - ANTHROPIC_BASE_URL — the gateway;
// - CLAUDE_CODE_GATEWAY_HINT_HEADERS=1 — Claude Code labels each request (main, subagent,
//   compaction…); without it nothing is a routing candidate;
// - ENABLE_TOOL_SEARCH=true — Claude Code turns MCP tool search off for any custom base URL, and
//   every request then carries every tool's schema, several times the input tokens.
//
// How the developer is identified depends on how they are billed:
// - passthrough: they keep their own claude.ai login or API key, and the gateway key rides in a
//   header of its own (ANTHROPIC_CUSTOM_HEADERS);
// - org_key: the gateway swaps in the organization's key, so the gateway key is their only
//   credential (ANTHROPIC_AUTH_TOKEN).
//
// The desktop app ignores ANTHROPIC_BASE_URL and settings files; it reaches a gateway only through
// its own third-party-inference settings, which carry an API key and no claude.ai login — so only
// an org-billed developer can use it through the gateway.

import type { CredentialProfile } from "./api";

/** Shown where the real key would go, after the one moment it is displayed. */
export const KEY_PLACEHOLDER = "tyg_…your gateway key";

export const GATEWAY_KEY_HEADER = "X-TheYgent-Gateway-Key";

export interface SetupInput {
  gatewayUrl: string;
  profile: CredentialProfile;
  /** The developer's raw key while it is still on screen; the placeholder otherwise. */
  key: string | null;
}

export interface DesktopValue {
  label: string;
  value: string;
}

export interface Setup {
  /** Claude Code's environment, for a shell profile (CLI, and IDEs launched from it). */
  shell: string;
  /** The same, as the ``env`` block of ~/.claude/settings.json. */
  settingsJson: string;
  /** What an admin distributes to everyone (managed settings): the gateway and the flags, never
   *  a developer's key — each developer adds their own. */
  managedJson: string;
  /** The desktop app's third-party inference values; null when the profile cannot use it. */
  desktop: DesktopValue[] | null;
}

function claudeCodeEnv(input: SetupInput, withKey: boolean): Record<string, string> {
  const env: Record<string, string> = { ANTHROPIC_BASE_URL: input.gatewayUrl };
  if (withKey) {
    const key = input.key ?? KEY_PLACEHOLDER;
    if (input.profile === "org_key") env.ANTHROPIC_AUTH_TOKEN = key;
    else env.ANTHROPIC_CUSTOM_HEADERS = `${GATEWAY_KEY_HEADER}: ${key}`;
  }
  env.CLAUDE_CODE_GATEWAY_HINT_HEADERS = "1";
  env.ENABLE_TOOL_SEARCH = "true";
  return env;
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

export function setupFor(input: SetupInput): Setup {
  const env = claudeCodeEnv(input, true);
  const shell = Object.entries(env)
    .map(([name, value]) => `export ${name}=${shellQuote(value)}`)
    .join("\n");
  return {
    shell,
    settingsJson: JSON.stringify({ env }, null, 2),
    managedJson: JSON.stringify({ env: claudeCodeEnv(input, false) }, null, 2),
    desktop:
      input.profile === "org_key"
        ? [
            { label: "Inference provider", value: "Gateway" },
            { label: "Gateway base URL", value: input.gatewayUrl },
            { label: "API key", value: input.key ?? KEY_PLACEHOLDER },
            { label: "Tool search", value: "On" },
          ]
        : null,
  };
}
