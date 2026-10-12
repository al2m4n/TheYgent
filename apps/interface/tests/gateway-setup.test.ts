// The setup snippets for pointing Claude Code at the AI gateway. What must hold for every client:
// the routing hints and tool search are always on; a passthrough developer keeps their own login
// and sends the gateway key in its own header, an org-billed one uses the gateway key as their
// credential; the managed settings never carry a key; the desktop app is offered only to
// org-billed developers.

import { describe, expect, it } from "vitest";
import { KEY_PLACEHOLDER, compactWindowFor, setupFor } from "../src/lib/gatewaySetup";

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

describe("the auto-compact window", () => {
  const model = (name: string, context: number, tools = true) => ({
    model: name,
    context_tokens: context,
    max_output_tokens: 16384,
    tools,
    images: false,
    rates: null,
  });
  const policy = (
    models: ReturnType<typeof model>[],
    main: { share: number; model?: string | null },
  ) => ({
    subagent_target: null,
    main_target: null,
    local_target: null,
    warm_switch_margin: 1.5,
    shadow_local: true,
    shadow_cloud: false,
    shadow_cloud_daily_usd: 1,
    local_models: models,
    local_routing: {
      side_requests: { share: 1, trial: 0, model: null, agent_types: [] },
      subagents: { share: 0, trial: 0, model: null, agent_types: [] },
      main: { trial: 0, agent_types: [], model: null, ...main },
    },
  });
  const medium = model("coder-medium", 131072);
  const large = model("coder-large", 262144);

  it("is three quarters of the largest model a main conversation can reach", () => {
    expect(compactWindowFor(policy([medium, large], { share: 1 }))).toEqual({
      window: 196000,
      model: "coder-large",
      context: 262144,
      tight: false,
    });
    // From the dial's model on, larger models only; a model without tool calls takes no turn.
    const vision = model("vision-huge", 1_000_000, false);
    expect(
      compactWindowFor(policy([medium, large, vision], { share: 1, model: "coder-large" })),
    ).toMatchObject({ window: 196000, model: "coder-large" });
  });

  it("is Claude Code's earliest for a 128K model, and says so", () => {
    expect(compactWindowFor(policy([medium], { share: 0.5 }))).toEqual({
      window: 100000,
      model: "coder-medium",
      context: 131072,
      tight: true,
    });
  });

  it("is Claude Code's latest at most", () => {
    expect(compactWindowFor(policy([model("coder-huge", 2_000_000)], { share: 1 }))).toMatchObject({
      window: 1_000_000,
    });
  });

  it("is none when Anthropic serves main conversations, and a warning when no model holds enough", () => {
    expect(compactWindowFor(policy([large], { share: 0 }))).toBeNull();
    expect(compactWindowFor(policy([model("coder-small", 32768)], { share: 1 }))).toEqual({
      tooSmall: true,
      model: "coder-small",
      context: 32768,
    });
  });

  it("goes into every Claude Code setup: the variable in a shell, the setting in a file", () => {
    const setup = setupFor({
      gatewayUrl: "http://gw:8082",
      profile: "passthrough",
      key: null,
      autoCompactWindow: 196000,
    });
    expect(setup.shell).toContain("export CLAUDE_CODE_AUTO_COMPACT_WINDOW='196000'");
    expect(JSON.parse(setup.settingsJson).autoCompactWindow).toBe(196000);
    const managed = JSON.parse(setup.managedJson);
    expect(managed.autoCompactWindow).toBe(196000);
    expect(JSON.stringify(managed.env)).not.toContain("tyg_");
    const without = setupFor({ gatewayUrl: "http://gw:8082", profile: "passthrough", key: null });
    expect(without.shell).not.toContain("AUTO_COMPACT");
    expect(JSON.parse(without.settingsJson)).not.toHaveProperty("autoCompactWindow");
  });
});
