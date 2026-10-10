// Tests for the AI Gateway page's Settings tab (RTL over a mocked control plane and gateway): the
// config editor (what it loads, what it saves, what it refuses), the lost-update guard, version
// history, and the organization's key — written to the gateway itself, never to the control
// plane, and never shown.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SettingsTab } from "../src/components/gateway/SettingsTab";
import { AI_GATEWAY_URL, CONTROL_PLANE_URL } from "../src/lib/api";
import { AuthProvider } from "../src/lib/auth";
import { notify } from "../src/lib/notify";

const HOUR_AGO = new Date(Date.now() - 3_600_000).toISOString();
const ORG_KEY = "sk-ant-api03-canary-org-key-value";

const RULES = [{ fingerprint: "0123456789abcdef", status: "allow_local", label: "title" }];

function config(version: number, over: Record<string, unknown> = {}) {
  return {
    version,
    mode: "observer",
    policy: {
      subagent_target: "claude-sonnet-5-5",
      main_target: null,
      local_target: null,
      warm_switch_margin: 1.5,
    },
    fingerprints: RULES,
    pricing_overrides: {
      "claude-opus-5-5": {
        input: 4,
        output: 20,
        cache_write_5m: 5,
        cache_write_1h: 8,
        cache_read: 0.4,
      },
    },
    updated_at: HOUR_AGO,
    updated_by: "usr_admin",
    ...over,
  };
}

function credentials(over: Record<string, unknown> = {}) {
  const org = { set: false, source: null, updatedAt: null, readOnly: null, ...over };
  return {
    orgApiKey: { ...org, writable: org.readOnly === null },
    serviceKey: { set: true },
  };
}

const SERVICE_TOKEN = "tygs_brand-new-service-token";
const SERVICE_KEY = {
  id: "key_svc",
  user_id: "usr_admin",
  name: "team gateway",
  token_prefix: "tygs_abcdefg",
  created_at: HOUR_AGO,
  last_used_at: HOUR_AGO,
  revoked_at: null,
};

const admin = {
  id: "usr_admin",
  username: "admin",
  display_name: "Admin",
  email: null,
  role: "admin",
  disabled: false,
  has_password: true,
  avatar_url: null,
  created_at: HOUR_AGO,
  updated_at: HOUR_AGO,
  last_login_at: null,
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface Call {
  origin: string;
  path: string;
  method: string;
  raw: string;
  body: unknown;
  ifMatch: string | undefined;
}

interface Mock {
  latest?: () => Response;
  put?: (body: unknown) => Response;
  versions?: unknown[];
  serviceKeys?: unknown[];
  developerProfile?: string;
  gateway?: (path: string, method: string, body: unknown) => Response;
}

function mockPlanes({
  latest = () => json(config(3)),
  put = (body) => json({ ...(body as object), version: 4, updated_at: HOUR_AGO }, 201),
  versions = [
    { version: 3, mode: "observer", created_at: HOUR_AGO, created_by: "usr_admin" },
    { version: 2, mode: "passthrough", created_at: HOUR_AGO, created_by: "usr_ada" },
  ],
  serviceKeys = [SERVICE_KEY],
  developerProfile = "passthrough",
  gateway = () => json(credentials()),
}: Mock = {}): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const parsed = new URL(url);
      const method = init?.method ?? "GET";
      const raw = `${url} ${(init?.body as string | undefined) ?? ""}`;
      const body = init?.body ? JSON.parse(init.body as string) : undefined;
      calls.push({
        origin: parsed.origin,
        path: parsed.pathname,
        method,
        raw,
        body,
        ifMatch: (init?.headers as Record<string, string> | undefined)?.["If-Match"],
      });
      if (parsed.origin === AI_GATEWAY_URL) return gateway(parsed.pathname, method, body);
      if (parsed.pathname === "/auth/status") {
        return json({ setup_required: false, user: admin, providers: [] });
      }
      if (parsed.pathname === "/gateway/config" && method === "GET") return latest();
      if (parsed.pathname === "/gateway/config" && method === "PUT") return put(body);
      if (parsed.pathname === "/gateway/config/versions") return json({ versions });
      if (parsed.pathname === "/gateway/config/versions/2") {
        return json(
          config(2, {
            mode: "passthrough",
            policy: {
              subagent_target: null,
              main_target: null,
              local_target: null,
              warm_switch_margin: 2,
            },
            pricing_overrides: {},
          }),
        );
      }
      if (parsed.pathname === "/gateway/service-keys" && method === "GET") {
        return json({ service_keys: serviceKeys });
      }
      if (parsed.pathname === "/gateway/service-keys" && method === "POST") {
        return json(
          { token: SERVICE_TOKEN, service_key: { ...SERVICE_KEY, id: "key_svc2", name: "k8s" } },
          201,
        );
      }
      if (parsed.pathname.startsWith("/gateway/service-keys/") && method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      if (parsed.pathname.startsWith("/gateway/developers/") && method === "PUT") {
        return json({ user_id: parsed.pathname.split("/").at(-1), ...(body as object) });
      }
      if (parsed.pathname === "/gateway/developers") {
        return json({
          developers: [
            {
              user_id: "usr_ada",
              username: "ada",
              display_name: "Ada",
              disabled: false,
              credential_profile: developerProfile,
              live_gateway_keys: 0,
              updated_at: null,
              updated_by: null,
            },
          ],
        });
      }
      return json({});
    }),
  );
  return calls;
}

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <SettingsTab />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

const puts = (calls: Call[]) => calls.filter((c) => c.method === "PUT");

afterEach(() => {
  notify.dismiss();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AI Gateway settings: the config", () => {
  it("loads the latest version into the editor", async () => {
    mockPlanes();
    renderTab();
    expect(await screen.findByRole("radio", { name: /Observer/ })).toBeChecked();
    expect(screen.getByLabelText("Subagent target")).toHaveValue("claude-sonnet-5-5");
    expect(screen.getByLabelText("Main conversation target")).toHaveValue("");
    expect(screen.getByLabelText("Warm switch margin")).toHaveValue("1.5");
    expect(screen.getByLabelText("Model (row 1)")).toHaveValue("claude-opus-5-5");
    expect(screen.getByLabelText("Cache read (row 1)")).toHaveValue("0.4");
    // Nothing to save until something changes.
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("saves a new version on top of the one it read, keeping the Catalog's rules", async () => {
    const calls = mockPlanes();
    const ui = userEvent.setup();
    renderTab();
    await ui.click(await screen.findByRole("radio", { name: /Passthrough/ }));
    await ui.type(screen.getByLabelText("Local target"), "triage-fast");
    await ui.click(screen.getByRole("button", { name: /Add a model/ }));
    await ui.type(screen.getByLabelText("Model (row 2)"), "claude-sonnet-5-5");
    for (const [field, value] of [
      ["Input", "2"],
      ["Output", "10"],
      ["Cache write 5m", "2.5"],
      ["Cache write 1h", "4"],
      ["Cache read", "0.2"],
    ]) {
      await ui.type(screen.getByLabelText(`${field} (row 2)`), value);
    }
    await ui.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(puts(calls)).toHaveLength(1));
    const [put] = puts(calls);
    expect(put.origin).toBe(CONTROL_PLANE_URL);
    expect(put.ifMatch).toBe('"3"');
    expect(put.body).toEqual({
      mode: "passthrough",
      policy: {
        subagent_target: "claude-sonnet-5-5",
        main_target: null,
        local_target: "triage-fast",
        warm_switch_margin: 1.5,
      },
      fingerprints: RULES,
      pricing_overrides: {
        "claude-opus-5-5": config(3).pricing_overrides["claude-opus-5-5"],
        "claude-sonnet-5-5": {
          input: 2,
          output: 10,
          cache_write_5m: 2.5,
          cache_write_1h: 4,
          cache_read: 0.2,
        },
      },
    });
  });

  it("says what is wrong instead of saving", async () => {
    const calls = mockPlanes();
    const ui = userEvent.setup();
    renderTab();
    await ui.type(await screen.findByLabelText("Local target"), "mlx");
    await ui.clear(screen.getByLabelText("Warm switch margin"));
    await ui.type(screen.getByLabelText("Warm switch margin"), "0.5");
    await ui.clear(screen.getByLabelText("Output (row 1)"));
    await ui.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText(/"mlx" is an engine/)).toBeInTheDocument();
    expect(screen.getByText("A number of at least 1.")).toBeInTheDocument();
    expect(screen.getByLabelText("Output (row 1)")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/all five prices/)).toBeInTheDocument();
    expect(puts(calls)).toHaveLength(0);
  });

  it("removes a model's prices", async () => {
    const calls = mockPlanes();
    const ui = userEvent.setup();
    renderTab();
    await ui.click(await screen.findByRole("button", { name: "Remove row 1" }));
    await ui.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts(calls)).toHaveLength(1));
    expect((puts(calls)[0].body as { pricing_overrides: object }).pricing_overrides).toEqual({});
  });

  it("starts the first config as an observer and saves it as it is", async () => {
    const calls = mockPlanes({
      latest: () => json({ error: { message: "none", code: "gateway_config_not_found" } }, 404),
      versions: [],
    });
    const ui = userEvent.setup();
    renderTab();
    expect(await screen.findByText(/No config saved yet/)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Observer/ })).toBeChecked();
    await ui.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts(calls)).toHaveLength(1));
    expect(puts(calls)[0].ifMatch).toBeUndefined();
    expect((puts(calls)[0].body as { mode: string }).mode).toBe("observer");
  });

  it("won't silently replace a version saved meanwhile", async () => {
    let latest = config(3);
    const calls = mockPlanes({
      latest: () => json(latest),
      put: (body) => {
        if (puts(calls).length === 1) {
          latest = config(4, { fingerprints: [{ ...RULES[0], status: "never_route" }] });
          return json({ error: { message: "stale", code: "config_version_conflict" } }, 412);
        }
        return json({ ...(body as object), version: 5, updated_at: HOUR_AGO }, 201);
      },
    });
    const ui = userEvent.setup();
    renderTab();
    await ui.type(await screen.findByLabelText("Main conversation target"), "claude-sonnet-5-5");
    await ui.click(screen.getByRole("button", { name: "Save" }));

    // The edit survives, and the page says what saving now would do.
    expect(await screen.findByText(/Version 4 was saved since you started/)).toBeInTheDocument();
    expect(screen.getByLabelText("Main conversation target")).toHaveValue("claude-sonnet-5-5");
    await ui.click(screen.getByRole("button", { name: "Save over version 4" }));
    await waitFor(() => expect(puts(calls)).toHaveLength(2));
    const second = puts(calls)[1];
    expect(second.ifMatch).toBe('"4"');
    // The newer version's Catalog rules are kept, not the ones this edit started from.
    expect((second.body as { fingerprints: unknown }).fingerprints).toEqual([
      { ...RULES[0], status: "never_route" },
    ]);
  });

  it("discards an edit in favour of the version saved meanwhile", async () => {
    let latest = config(3);
    const calls = mockPlanes({
      latest: () => json(latest),
      put: () => {
        latest = config(4, { mode: "passthrough" });
        return json({ error: { message: "stale", code: "config_version_conflict" } }, 412);
      },
    });
    const ui = userEvent.setup();
    renderTab();
    await ui.type(await screen.findByLabelText("Main conversation target"), "claude-sonnet-5-5");
    await ui.click(screen.getByRole("button", { name: "Save" }));
    await ui.click(await screen.findByRole("button", { name: "discard your changes" }));
    expect(screen.getByRole("radio", { name: /Passthrough/ })).toBeChecked();
    expect(screen.getByLabelText("Main conversation target")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(puts(calls)).toHaveLength(1);
  });

  it("lists the history and starts an edit from an older version", async () => {
    const calls = mockPlanes();
    const ui = userEvent.setup();
    renderTab();
    const history = await screen.findByRole("list", { name: "Config versions" });
    const [v3, v2] = within(history).getAllByRole("listitem");
    expect(v3).toHaveTextContent("current");
    expect(v3).toHaveTextContent("by you");
    expect(await within(v2).findByText(/by ada/)).toBeInTheDocument();
    expect(within(v3).queryByRole("button", { name: /Load/ })).toBeNull();

    await ui.click(within(v2).getByRole("button", { name: "Load version 2" }));
    expect(await screen.findByText(/Showing version 2's settings/)).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Passthrough/ })).toBeChecked();
    expect(screen.getByLabelText("Warm switch margin")).toHaveValue("2");
    await ui.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(puts(calls)).toHaveLength(1));
    // Restoring is a new version on top of the latest, never a rewrite of history.
    expect(puts(calls)[0].ifMatch).toBe('"3"');
    expect((puts(calls)[0].body as { fingerprints: unknown }).fingerprints).toEqual(RULES);
  });
  it("goes back to the current version from an older one", async () => {
    mockPlanes();
    const ui = userEvent.setup();
    renderTab();
    await ui.click(await screen.findByRole("button", { name: "Load version 2" }));
    await ui.click(await screen.findByRole("button", { name: "go back to version 3" }));
    expect(screen.getByRole("radio", { name: /Observer/ })).toBeChecked();
    expect(screen.getByLabelText("Warm switch margin")).toHaveValue("1.5");
    expect(screen.queryByText(/Showing version 2/)).toBeNull();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("leads the history with the latest ten versions", async () => {
    mockPlanes({
      versions: Array.from({ length: 14 }, (_, i) => ({
        version: 14 - i,
        mode: "observer",
        created_at: HOUR_AGO,
        created_by: "usr_admin",
      })),
      latest: () => json(config(14)),
    });
    const ui = userEvent.setup();
    renderTab();
    const history = await screen.findByRole("list", { name: "Config versions" });
    expect(within(history).getAllByRole("listitem")).toHaveLength(10);
    await ui.click(screen.getByRole("button", { name: "Show all 14 versions" }));
    expect(within(history).getAllByRole("listitem")).toHaveLength(14);
    expect(screen.queryByRole("button", { name: /Show all/ })).toBeNull();
  });
});

describe("AI Gateway settings: the org key", () => {
  it("writes the key to the gateway itself, never the control plane, and never shows it", async () => {
    let stored = false;
    const calls = mockPlanes({
      gateway: (path, method) => {
        if (path === "/admin/credentials/org-api-key" && method === "PUT") stored = true;
        return json(
          credentials(stored ? { set: true, source: "stored", updatedAt: HOUR_AGO } : {}),
        );
      },
    });
    const ui = userEvent.setup();
    renderTab();
    expect(await screen.findByText("not set")).toBeInTheDocument();
    const input = screen.getByLabelText("Anthropic API key");
    expect(input).toHaveAttribute("type", "password");
    await ui.type(input, ORG_KEY);
    await ui.click(screen.getByRole("button", { name: "Save key" }));

    expect(await screen.findByText("set")).toBeInTheDocument();
    const carrying = calls.filter((c) => c.raw.includes(ORG_KEY));
    expect(carrying).toHaveLength(1);
    expect(carrying[0].origin).toBe(AI_GATEWAY_URL);
    expect(carrying[0].path).toBe("/admin/credentials/org-api-key");
    expect(carrying[0].body).toEqual({ api_key: ORG_KEY });
    expect(AI_GATEWAY_URL).not.toBe(CONTROL_PLANE_URL);
    expect(screen.getByLabelText("Replace with")).toHaveValue("");
    expect(document.body.textContent).not.toContain(ORG_KEY);
    expect(screen.getByRole("button", { name: "Replace key" })).toBeInTheDocument();
  });

  it("removes a stored key after confirmation", async () => {
    let stored = true;
    const calls = mockPlanes({
      gateway: (path, method) => {
        if (path === "/admin/credentials/org-api-key" && method === "DELETE") stored = false;
        return json(
          credentials(stored ? { set: true, source: "stored", updatedAt: HOUR_AGO } : {}),
        );
      },
    });
    const ui = userEvent.setup();
    renderTab();
    await ui.click(await screen.findByRole("button", { name: "Remove key" }));
    await ui.click(await screen.findByRole("button", { name: "Remove" }));
    expect(await screen.findByText("not set")).toBeInTheDocument();
    const removed = calls.find((c) => c.method === "DELETE");
    expect(removed?.origin).toBe(AI_GATEWAY_URL);
  });

  it("points to the deployment when the deployment set the key", async () => {
    mockPlanes({
      gateway: () => json(credentials({ set: true, source: "env", readOnly: "env" })),
    });
    renderTab();
    expect(await screen.findByText(/by the deployment/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Anthropic API key|Replace with/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove key" })).toBeNull();
  });

  it("explains that a personal gateway has no org key", async () => {
    mockPlanes({ gateway: () => json(credentials({ readOnly: "personal_mode" })) });
    renderTab();
    expect(await screen.findByText(/runs in personal mode/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Anthropic API key")).toBeNull();
    expect(screen.queryByText(/takes the key from its deployment/)).toBeNull();
  });

  it("sends a replicated gateway's key to its deployment", async () => {
    mockPlanes({ gateway: () => json(credentials({ readOnly: "deployment_only" })) });
    renderTab();
    expect(await screen.findByText(/takes the key from its deployment only/)).toBeInTheDocument();
    expect(screen.getByText("not set")).toBeInTheDocument();
    expect(screen.queryByLabelText("Anthropic API key")).toBeNull();
    expect(screen.queryByText(/runs in personal mode/)).toBeNull();
  });

  it("shows the gateway's refusal and drops the typed key", async () => {
    mockPlanes({
      gateway: (_path, method) =>
        method === "PUT"
          ? json(
              {
                error: {
                  message: "an API key is 16-512 printable characters, no spaces",
                  code: "invalid_api_key",
                },
              },
              422,
            )
          : json(credentials()),
    });
    const ui = userEvent.setup();
    renderTab();
    await ui.type(await screen.findByLabelText("Anthropic API key"), "too short");
    await ui.click(screen.getByRole("button", { name: "Save key" }));
    expect(await screen.findByText(/16-512 printable characters/)).toBeInTheDocument();
    expect(screen.getByLabelText("Anthropic API key")).toHaveValue("");
  });

  it("stays reachable when the control plane can't serve the config", async () => {
    mockPlanes({
      latest: () => json({ error: { message: "database unavailable", code: "db_down" } }, 503),
    });
    renderTab();
    expect(await screen.findByText("database unavailable")).toBeInTheDocument();
    expect(await screen.findByLabelText("Anthropic API key")).toBeInTheDocument();
  });

  it("says where to set the gateway's address when it can't reach it", async () => {
    mockPlanes({
      gateway: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    renderTab();
    expect(await screen.findByText(/Can't reach the gateway at/)).toBeInTheDocument();
    expect(screen.getByText(/Setup & keys tab/)).toBeInTheDocument();
  });
});

describe("AI Gateway settings: service keys", () => {
  it("lists the gateways' service keys", async () => {
    mockPlanes();
    renderTab();
    const list = await screen.findByRole("list", { name: "Service keys" });
    expect(within(list).getByText("team gateway")).toBeInTheDocument();
    expect(within(list).getByText("tygs_abcdefg…")).toBeInTheDocument();
    expect(within(list).getByText(/last used/)).toBeInTheDocument();
  });

  it("shows a new service key once, ready for .env and Kubernetes", async () => {
    const calls = mockPlanes();
    const ui = userEvent.setup();
    renderTab();
    await ui.click(await screen.findByRole("button", { name: /New service key/ }));
    await ui.type(screen.getByLabelText("Service key name"), "k8s");
    await ui.click(screen.getByRole("button", { name: "Create key" }));

    const dialog = await screen.findByRole("dialog", { name: "Your new service key" });
    expect(within(dialog).getByText(/shown only once/)).toBeInTheDocument();
    expect(
      within(dialog).getByText(`THEYGENT_AI_GATEWAY_SERVICE_KEY=${SERVICE_TOKEN}`),
    ).toBeInTheDocument();
    expect(within(dialog).getByText(/create secret generic theygent-ai-gateway/)).toHaveTextContent(
      `--from-literal=THEYGENT_AI_GATEWAY_SERVICE_KEY=${SERVICE_TOKEN}`,
    );
    const created = calls.find((c) => c.path === "/gateway/service-keys" && c.method === "POST");
    expect(created?.origin).toBe(CONTROL_PLANE_URL);
    expect(created?.body).toEqual({ name: "k8s" });

    await ui.click(within(dialog).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByText(new RegExp(SERVICE_TOKEN))).not.toBeInTheDocument();
  });

  it("revokes a service key after confirmation", async () => {
    const calls = mockPlanes();
    const ui = userEvent.setup();
    renderTab();
    await ui.click(await screen.findByRole("button", { name: "Revoke team gateway" }));
    await ui.click(await screen.findByRole("button", { name: "Revoke" }));
    await waitFor(() =>
      expect(
        calls.some((c) => c.method === "DELETE" && c.path === "/gateway/service-keys/key_svc"),
      ).toBe(true),
    );
  });
});

describe("AI Gateway settings: developers' billing", () => {
  it("shows who pays for each developer's traffic", async () => {
    mockPlanes();
    renderTab();
    const list = await screen.findByRole("list", { name: "Developers" });
    expect(within(list).getByText("Ada")).toBeInTheDocument();
    expect(within(list).getByLabelText("Billing for ada")).toHaveValue("passthrough");
  });

  it("bills a developer to the organization after confirmation", async () => {
    const calls = mockPlanes();
    const ui = userEvent.setup();
    renderTab();
    await ui.selectOptions(await screen.findByLabelText("Billing for ada"), "org_key");
    const confirm = await screen.findByRole("alertdialog");
    expect(confirm).toHaveTextContent(/must have that key/);
    await ui.click(within(confirm).getByRole("button", { name: "Change billing" }));
    await waitFor(() =>
      expect(calls.some((c) => c.path === "/gateway/developers/usr_ada")).toBe(true),
    );
    const put = calls.find((c) => c.path === "/gateway/developers/usr_ada");
    expect(put?.method).toBe("PUT");
    expect(put?.origin).toBe(CONTROL_PLANE_URL);
    expect(put?.body).toEqual({ credential_profile: "org_key" });
  });

  it("warns that moving back to their own credential needs a new setup, and can be cancelled", async () => {
    const calls = mockPlanes({ developerProfile: "org_key" });
    const ui = userEvent.setup();
    renderTab();
    await ui.selectOptions(await screen.findByLabelText("Billing for ada"), "passthrough");
    const confirm = await screen.findByRole("alertdialog");
    expect(confirm).toHaveTextContent(/stops working until they copy the new one/);
    await ui.click(within(confirm).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(calls.some((c) => c.path.startsWith("/gateway/developers/"))).toBe(false);
    expect(screen.getByLabelText("Billing for ada")).toHaveValue("org_key");
  });
});
