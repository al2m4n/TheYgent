// Tests for the AI Gateway page's Setup & keys tab (RTL over a mocked control plane): the billing
// profile, the key list, creating a key (shown once, with setups that carry it), revoking, the
// gateway address, and what only admins see.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SetupTab } from "../src/components/gateway/SetupTab";
import { AuthProvider } from "../src/lib/auth";
import { notify } from "../src/lib/notify";

const HOUR_AGO = new Date(Date.now() - 3_600_000).toISOString();
const NEW_TOKEN = "tyg_brand-new-secret-token";

function user(role: string) {
  return {
    id: "usr_dev",
    username: "dev",
    display_name: "Dev",
    email: null,
    role,
    disabled: false,
    has_password: true,
    avatar_url: null,
    created_at: HOUR_AGO,
    updated_at: HOUR_AGO,
    last_login_at: null,
  };
}

const KEY = {
  id: "key_1",
  user_id: "usr_dev",
  name: "work laptop",
  token_prefix: "tyg_abcdefgh",
  created_at: HOUR_AGO,
  last_used_at: null,
  revoked_at: null,
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface Call {
  path: string;
  method: string;
  body: unknown;
}

function mockControlPlane({
  role = "editor",
  profile = "passthrough",
  keys = [KEY],
}: { role?: string; profile?: string; keys?: unknown[] } = {}): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const parsed = new URL(url);
      const method = init?.method ?? "GET";
      calls.push({
        path: parsed.pathname + parsed.search,
        method,
        body: init?.body ? JSON.parse(init.body as string) : undefined,
      });
      if (parsed.pathname === "/auth/status") {
        return json({ setup_required: false, user: user(role), providers: [] });
      }
      if (parsed.pathname === "/gateway/me") {
        return json({
          user_id: "usr_dev",
          username: "dev",
          display_name: "Dev",
          disabled: false,
          credential_profile: profile,
          live_gateway_keys: keys.length,
          updated_at: null,
          updated_by: null,
        });
      }
      if (parsed.pathname === "/gateway/keys" && method === "GET") {
        return json({ gateway_keys: keys });
      }
      if (parsed.pathname === "/gateway/keys" && method === "POST") {
        return json(
          { token: NEW_TOKEN, gateway_key: { ...KEY, id: "key_2", name: "desktop" } },
          201,
        );
      }
      if (parsed.pathname.startsWith("/gateway/keys/") && method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      if (parsed.pathname === "/gateway/developers") {
        return json({ developers: [] });
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
        <SetupTab />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

// This test environment ships no localStorage (the app treats it as optional): an in-memory one
// lets the gateway-address override be exercised.
beforeEach(() => {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => void store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  } as Storage);
});

afterEach(() => {
  notify.dismiss();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AI Gateway setup & keys", () => {
  it("shows how the developer is billed and their keys", async () => {
    mockControlPlane();
    renderTab();
    expect(await screen.findByText("your own Claude login or API key")).toBeInTheDocument();
    const keys = await screen.findByRole("list", { name: "Gateway keys" });
    expect(within(keys).getByText("work laptop")).toBeInTheDocument();
    expect(within(keys).getByText("tyg_abcdefgh…")).toBeInTheDocument();
  });

  it("shows a new key once, with setups that already carry it", async () => {
    const calls = mockControlPlane();
    const ui = userEvent.setup();
    renderTab();
    await ui.click(await screen.findByRole("button", { name: /New key/ }));
    await ui.type(screen.getByLabelText("Key name"), "desktop");
    await ui.click(screen.getByRole("button", { name: "Create key" }));

    const dialog = await screen.findByRole("dialog", { name: "Your new gateway key" });
    expect(within(dialog).getByText(/shown only once/)).toBeInTheDocument();
    expect(within(dialog).getAllByText(new RegExp(NEW_TOKEN)).length).toBeGreaterThan(1);
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ name: "desktop" });

    await ui.click(within(dialog).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByText(new RegExp(NEW_TOKEN))).not.toBeInTheDocument();
    expect(screen.getAllByText(/tyg_…your gateway key/).length).toBeGreaterThan(0);
  });

  it("revokes a key after confirmation", async () => {
    const calls = mockControlPlane();
    const ui = userEvent.setup();
    renderTab();
    await ui.click(await screen.findByRole("button", { name: "Revoke" }));
    await ui.click(await screen.findByRole("button", { name: "Revoke" }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === "DELETE" && c.path === "/gateway/keys/key_1")).toBe(
        true,
      ),
    );
  });

  it("offers the desktop app only to org-billed developers", async () => {
    mockControlPlane({ profile: "passthrough" });
    const ui = userEvent.setup();
    const first = renderTab();
    await screen.findByText("your own Claude login or API key");
    await ui.click(screen.getByRole("tab", { name: "Desktop app" }));
    expect(
      screen.getByText(/works\s+through the gateway only when you are billed/),
    ).toBeInTheDocument();
    first.unmount();

    mockControlPlane({ profile: "org_key" });
    renderTab();
    await screen.findByText("the organization's key");
    await ui.click(screen.getByRole("tab", { name: "Desktop app" }));
    expect(screen.getByText("Gateway base URL")).toBeInTheDocument();
    expect(screen.getByText("Tool search")).toBeInTheDocument();
  });

  it("puts the gateway address into every setup and remembers it in this browser", async () => {
    mockControlPlane();
    renderTab();
    await screen.findByText("your own Claude login or API key");
    fireEvent.change(screen.getByLabelText("Gateway address"), {
      target: { value: "https://gw.example.com/" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText(/export ANTHROPIC_BASE_URL='https:\/\/gw.example.com'/),
    ).toBeInTheDocument();
    expect(localStorage.getItem("theygent.url.gateway")).toBe("https://gw.example.com");
  });

  it("gives admins everyone's keys and the managed settings", async () => {
    const calls = mockControlPlane({ role: "admin" });
    const ui = userEvent.setup();
    renderTab();
    expect(await screen.findByRole("tab", { name: "Managed settings" })).toBeInTheDocument();
    await ui.click(screen.getByRole("switch", { name: "Everyone's keys" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/gateway/keys?all=true")).toBe(true));
  });

  it("keeps the managed settings and everyone's keys from non-admins", async () => {
    mockControlPlane({ role: "viewer" });
    renderTab();
    await screen.findByText("your own Claude login or API key");
    expect(screen.queryByRole("tab", { name: "Managed settings" })).not.toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "Everyone's keys" })).not.toBeInTheDocument();
  });
});
