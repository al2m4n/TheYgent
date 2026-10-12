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
//
// When the gateway serves main conversations on a local model, the setup also sets Claude Code's
// auto-compact window. Claude Code compacts by the context window of the model it asked for (1M
// tokens for current models behind a gateway), not the local model's; told to compact sooner, a
// conversation compacts — on its local model — while it still fits.

import type { CredentialProfile, GatewayConfigDraft } from "./api";

/** Shown where the real key would go, after the one moment it is displayed. */
export const KEY_PLACEHOLDER = "tyg_…your gateway key";

export const GATEWAY_KEY_HEADER = "X-TheYgent-Gateway-Key";

export interface SetupInput {
  gatewayUrl: string;
  profile: CredentialProfile;
  /** The developer's raw key while it is still on screen; the placeholder otherwise. */
  key: string | null;
  /** Tokens at which Claude Code should compact (see ``compactWindowFor``); none: its default. */
  autoCompactWindow?: number | null;
}

/** Claude Code compacts no earlier than this, and no later than its model's window allows. */
export const MIN_AUTO_COMPACT_WINDOW = 100_000;
export const MAX_AUTO_COMPACT_WINDOW = 1_000_000;
/** The context a model for the main conversation needs: compacting at Claude Code's earliest still
 *  leaves room for the newest turn and the compaction itself. */
export const MIN_MAIN_CONTEXT = 125_000;
/** A quarter of the local context is left for the newest turn (a file read can add 25,000
 *  tokens), the compaction request and its summary, and the gateway's size estimate. */
const COMPACT_AT = 0.75;

/** What Claude Code's auto-compact window should be, for the gateway's main-conversation dial. */
export type CompactWindow =
  /** Compact at ``window`` tokens: the largest local model a conversation can move to holds
   *  ``context``. ``tight``: Claude Code can't compact as early as a quarter's room needs. */
  | { window: number; model: string; context: number; tight: boolean }
  /** No local model for the main conversation holds enough: long conversations will outgrow it. */
  | { tooSmall: true; model: string; context: number };

/** The auto-compact window that keeps locally served main conversations within their model, or
 *  null when no main conversation is served locally. A conversation moves to larger listed models
 *  as it grows, so the window follows the largest one it can reach. */
export function compactWindowFor(policy: GatewayConfigDraft["policy"]): CompactWindow | null {
  const dial = policy.local_routing.main;
  if (dial.share <= 0) return null;
  const listed = policy.local_models;
  const from = dial.model === null ? 0 : listed.findIndex((m) => m.model === dial.model);
  if (from < 0) return null;
  const reachable = listed.slice(from).filter((m) => m.tools);
  if (reachable.length === 0) return null;
  const largest = reachable.reduce((a, b) => (b.context_tokens > a.context_tokens ? b : a));
  const context = largest.context_tokens;
  if (context < MIN_MAIN_CONTEXT) return { tooSmall: true, model: largest.model, context };
  const wanted = Math.floor((context * COMPACT_AT) / 1000) * 1000;
  return {
    window: Math.min(MAX_AUTO_COMPACT_WINDOW, Math.max(MIN_AUTO_COMPACT_WINDOW, wanted)),
    model: largest.model,
    context,
    tight: wanted < MIN_AUTO_COMPACT_WINDOW,
  };
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
  const window = input.autoCompactWindow ?? null;
  // The variable for a shell; the settings files take the setting itself.
  const shellEnv =
    window === null ? env : { ...env, CLAUDE_CODE_AUTO_COMPACT_WINDOW: String(window) };
  const shell = Object.entries(shellEnv)
    .map(([name, value]) => `export ${name}=${shellQuote(value)}`)
    .join("\n");
  const compact = window === null ? {} : { autoCompactWindow: window };
  return {
    shell,
    settingsJson: JSON.stringify({ env, ...compact }, null, 2),
    managedJson: JSON.stringify({ env: claudeCodeEnv(input, false), ...compact }, null, 2),
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
