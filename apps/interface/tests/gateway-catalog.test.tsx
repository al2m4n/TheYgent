// Tests for the AI Gateway page's Catalog tab (RTL over a mocked control plane): the side-request
// kinds with what was seen of them, applying a suggestion, reviewing by hand, the lost-update
// guard, and that only admins get the tab at all.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CatalogTab } from "../src/components/gateway/CatalogTab";
import { AuthProvider } from "../src/lib/auth";
import { notify } from "../src/lib/notify";
import { Gateway } from "../src/routes/Gateway";

const HOUR_AGO = new Date(Date.now() - 3_600_000).toISOString();

function kind(fingerprint: string, over: Record<string, unknown> = {}) {
  return {
    fingerprint,
    status: "unreviewed",
    label: null,
    first_seen: HOUR_AGO,
    last_seen: HOUR_AGO,
    request_count: 12,
    client_versions: ["2.1.285"],
    shape: { tool_count: 0, max_tokens: 512, has_output_schema: true },
    shares_main_prefix: false,
    known_kind: null,
    suggested_status: null,
    ...over,
  };
}

const TITLE = kind("0123456789abcdef", {
  known_kind: "session_title",
  suggested_status: "allow_local",
});
const PLAIN = kind("fedcba9876543210", { status: "observe_only", label: "suggestion" });
const QUOTA = kind("1a2b3c4d5e6f7081", {
  shape: { tool_count: 1, max_tokens: 1 },
  known_kind: "quota_check",
  suggested_status: "never_route",
});

function user(role: string) {
  return {
    id: "usr_admin",
    username: "admin",
    display_name: "Admin",
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
  ifMatch: string | undefined;
}

function mockControlPlane({
  role = "admin",
  entries = () => [TITLE, PLAIN],
  version = () => 3,
  review = () => json({ config_version: 4, fingerprint: { ...TITLE, status: "allow_local" } }),
}: {
  role?: string;
  entries?: () => unknown[];
  version?: () => number | null;
  review?: () => Response;
} = {}): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const parsed = new URL(url);
      const method = init?.method ?? "GET";
      calls.push({
        path: parsed.pathname,
        method,
        body: init?.body ? JSON.parse(init.body as string) : undefined,
        ifMatch: (init?.headers as Record<string, string> | undefined)?.["If-Match"],
      });
      if (parsed.pathname === "/auth/status") {
        return json({ setup_required: false, user: user(role), providers: [] });
      }
      if (parsed.pathname === "/gateway/fingerprints") {
        return json({ config_version: version(), fingerprints: entries() });
      }
      if (parsed.pathname.startsWith("/gateway/fingerprints/") && method === "PUT") {
        return review();
      }
      if (parsed.pathname === "/gateway/config") {
        return json({ error: { message: "none", code: "gateway_config_not_found" } }, 404);
      }
      if (parsed.pathname === "/gateway/instances") return json({ instances: [] });
      return json({});
    }),
  );
  return calls;
}

function renderWith(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider>{ui}</AuthProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  notify.dismiss();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AI Gateway catalog", () => {
  it("lists each kind with its status and what was seen of it", async () => {
    mockControlPlane({ entries: () => [TITLE, PLAIN, QUOTA] });
    renderWith(<CatalogTab />);
    const list = await screen.findByRole("list", { name: "Side-request kinds" });
    const [title, plain, quota] = within(list).getAllByRole("listitem");
    expect(title).toHaveTextContent("0123456789abcdef");
    expect(title).toHaveTextContent("unreviewed");
    expect(title).toHaveTextContent("Looks like the session title");
    expect(title).toHaveTextContent("no tools · structured output · max 512 tokens");
    expect(title).toHaveTextContent("12 seen");
    expect(title).toHaveTextContent("Claude Code 2.1.285");
    expect(plain).toHaveTextContent("suggestion");
    expect(plain).toHaveTextContent("observe only");
    expect(quota).toHaveTextContent("Looks like Claude Code's startup quota check");
    expect(quota).toHaveTextContent(/1 tool · max 1 token(?!s)/);
    // No suggestion for a kind its shape doesn't identify.
    expect(within(plain).queryByRole("button", { name: /Apply suggestion/ })).toBeNull();
  });

  it("applies a suggestion on top of the version the page read", async () => {
    const calls = mockControlPlane();
    const ui = userEvent.setup();
    renderWith(<CatalogTab />);
    await ui.click(await screen.findByRole("button", { name: "Apply suggestion: local model" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    const put = calls.find((c) => c.method === "PUT");
    expect(put?.path).toBe("/gateway/fingerprints/0123456789abcdef");
    expect(put?.body).toEqual({ status: "allow_local" });
    expect(put?.ifMatch).toBe('"3"');
  });

  it("reviews a kind by hand: a status and a label", async () => {
    const calls = mockControlPlane();
    const ui = userEvent.setup();
    renderWith(<CatalogTab />);
    await ui.click(await screen.findByRole("button", { name: "Review fedcba9876543210" }));
    const dialog = await screen.findByRole("dialog", { name: "Review fedcba9876543210" });
    await ui.selectOptions(within(dialog).getByLabelText("Status"), "never_route");
    expect(within(dialog).getByText(/Always stays on the model/)).toBeInTheDocument();
    const label = within(dialog).getByLabelText("Label");
    await ui.clear(label);
    await ui.type(label, "prompt suggestion");
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeDisabled();
    await ui.clear(label);
    await ui.type(label, "prompt-suggestion");
    await ui.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
      status: "never_route",
      label: "prompt-suggestion",
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("refuses to save over a config someone changed meanwhile, and reloads", async () => {
    let version = 3;
    const calls = mockControlPlane({
      version: () => version,
      review: () => {
        version = 4;
        return json({ error: { message: "stale", code: "config_version_conflict" } }, 412);
      },
    });
    const error = vi.spyOn(notify, "error");
    const ui = userEvent.setup();
    renderWith(<CatalogTab />);
    await ui.click(await screen.findByRole("button", { name: "Apply suggestion: local model" }));
    await waitFor(() => expect(error).toHaveBeenCalledWith(expect.stringMatching(/changed since/)));
    await waitFor(() =>
      expect(calls.filter((c) => c.path === "/gateway/fingerprints").length).toBe(2),
    );

    await ui.click(screen.getByRole("button", { name: "Apply suggestion: local model" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "PUT").length).toBe(2));
    expect(calls.filter((c) => c.method === "PUT")[1].ifMatch).toBe('"4"');
  });

  it("says so when no kinds have been seen", async () => {
    mockControlPlane({ entries: () => [], version: () => null });
    renderWith(<CatalogTab />);
    expect(await screen.findByText("No side-request kinds seen yet")).toBeInTheDocument();
  });
});

describe("AI Gateway admin tabs", () => {
  it("are the admin's alone", async () => {
    mockControlPlane({ role: "editor" });
    const first = renderWith(<Gateway />);
    await screen.findByRole("tab", { name: "Setup & keys" });
    await waitFor(() => expect(screen.queryByRole("tab", { name: "Catalog" })).toBeNull());
    expect(screen.queryByRole("tab", { name: "Settings" })).toBeNull();
    first.unmount();

    mockControlPlane({ role: "admin" });
    renderWith(<Gateway />);
    expect(await screen.findByRole("tab", { name: "Catalog" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Settings" })).toBeInTheDocument();
  });
});
