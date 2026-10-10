// The setup snippets for pointing Claude Code at the AI gateway. What must hold for every client:
// the routing hints and tool search are always on; a passthrough developer keeps their own login
// and sends the gateway key in its own header, an org-billed one uses the gateway key as their
// credential; the managed settings never carry a key; the desktop app is offered only to
// org-billed developers.

import { describe, expect, it } from "vitest";
import { KEY_PLACEHOLDER, setupFor } from "../src/lib/gatewaySetup";

const URL_ = "https://gateway.example.com";
const KEY = "tyg_abc123secret";

function envOf(json: string): Record<string, string> {
  return JSON.parse(json).env;
}

describe("gateway setup", () => {
  it("keeps a passthrough developer's own login and sends the key in its own header", () => {
    const setup = setupFor({ gatewayUrl: URL_, profile: "passthrough", key: KEY });
    expect(envOf(setup.settingsJson)).toEqual({
      ANTHROPIC_BASE_URL: URL_,
      ANTHROPIC_CUSTOM_HEADERS: `X-TheYgent-Gateway-Key: ${KEY}`,
      CLAUDE_CODE_GATEWAY_HINT_HEADERS: "1",
      ENABLE_TOOL_SEARCH: "true",
    });
    expect(setup.shell).toContain(`export ANTHROPIC_BASE_URL='${URL_}'`);
    expect(setup.shell).toContain(
      `export ANTHROPIC_CUSTOM_HEADERS='X-TheYgent-Gateway-Key: ${KEY}'`,
    );
    expect(setup.shell).not.toContain("ANTHROPIC_AUTH_TOKEN");
    expect(setup.desktop).toBeNull();
  });

  it("makes the gateway key an org-billed developer's only credential", () => {
    const setup = setupFor({ gatewayUrl: URL_, profile: "org_key", key: KEY });
    const env = envOf(setup.settingsJson);
    expect(env.ANTHROPIC_AUTH_TOKEN).toBe(KEY);
    expect(env).not.toHaveProperty("ANTHROPIC_CUSTOM_HEADERS");
    expect(setup.shell).toContain(`export ANTHROPIC_AUTH_TOKEN='${KEY}'`);
    expect(setup.desktop).toEqual([
      { label: "Inference provider", value: "Gateway" },
      { label: "Gateway base URL", value: URL_ },
      { label: "API key", value: KEY },
      { label: "Tool search", value: "On" },
    ]);
  });

  it.each(["passthrough", "org_key"] as const)(
    "always turns on the routing hints and tool search (%s)",
    (profile) => {
      const setup = setupFor({ gatewayUrl: URL_, profile, key: null });
      for (const text of [setup.shell, setup.settingsJson, setup.managedJson]) {
        expect(text).toContain("CLAUDE_CODE_GATEWAY_HINT_HEADERS");
        expect(text).toContain("ENABLE_TOOL_SEARCH");
      }
    },
  );

  it("never puts a key in the managed settings everyone receives", () => {
    for (const profile of ["passthrough", "org_key"] as const) {
      const managed = setupFor({ gatewayUrl: URL_, profile, key: KEY }).managedJson;
      expect(managed).not.toContain(KEY);
      expect(envOf(managed)).toEqual({
        ANTHROPIC_BASE_URL: URL_,
        CLAUDE_CODE_GATEWAY_HINT_HEADERS: "1",
        ENABLE_TOOL_SEARCH: "true",
      });
    }
  });

  it("shows a placeholder once the key is no longer on screen", () => {
    const setup = setupFor({ gatewayUrl: URL_, profile: "org_key", key: null });
    expect(envOf(setup.settingsJson).ANTHROPIC_AUTH_TOKEN).toBe(KEY_PLACEHOLDER);
    expect(setup.desktop?.find((v) => v.label === "API key")?.value).toBe(KEY_PLACEHOLDER);
  });

  it("quotes values safely for the shell", () => {
    const setup = setupFor({ gatewayUrl: "http://x/it's", profile: "passthrough", key: KEY });
    expect(setup.shell).toContain(`export ANTHROPIC_BASE_URL='http://x/it'"'"'s'`);
  });
});
