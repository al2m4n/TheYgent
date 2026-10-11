// Tests for the AI Gateway page's Sessions tab (RTL over a mocked control plane): the session list
// (whose, paging), and a session's timeline — requests in order with what the policy would have
// done and why, each reason explained.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionsTab } from "../src/components/gateway/SessionsTab";
import { AuthProvider } from "../src/lib/auth";
import { REASONS } from "../src/lib/gatewayReasons";

const T0 = Date.now() - 3_600_000;
const at = (seconds: number) => new Date(T0 + seconds * 1000).toISOString();

function totals(over: Record<string, number> = {}) {
  return {
    calls: 0,
    errors: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_5m_tokens: 0,
    cache_write_1h_tokens: 0,
    cache_write_tokens: 0,
    cost_usd: 0,
    unpriced_calls: 0,
    with_routing_usd: 0,
    saving_usd: 0,
    routed_calls: 0,
    applied_calls: 0,
    routed_tokens: 0,
    routed_local_calls: 0,
    routed_local_tokens: 0,
    ...over,
    // Without applied routes, what it would cost as requested is what it cost.
    baseline_usd: over.baseline_usd ?? over.cost_usd ?? 0,
  };
}

function session(id: string, lastSeconds: number, over: Record<string, unknown> = {}) {
  return {
    session_id: id,
    developer_id: "usr_dev",
    client: "cli",
    first_at: at(0),
    last_at: at(lastSeconds),
    totals: totals({ calls: 4, cost_usd: 0.42, with_routing_usd: 0.3, saving_usd: 0.12 }),
    ...over,
  };
}

function entry(id: string, seconds: number, over: Record<string, unknown> = {}) {
  return {
    id,
    started_at: at(seconds),
    request_class: "main",
    agent_id: null,
    agent_type: null,
    prompt_id: null,
    endpoint: "messages",
    status: 200,
    outcome: "complete",
    error_type: null,
    requested_model: "claude-opus-5-5",
    served_model: "claude-opus-5-5",
    stop_reason: "end_turn",
    compaction: null,
    shares_main_prefix: null,
    usage: {
      input_tokens: 2,
      output_tokens: 400,
      cache_read_tokens: 20_000,
      cache_write_5m_tokens: 0,
      cache_write_1h_tokens: 900,
    },
    cost_usd: 0.0154,
    baseline_usd: null,
    timing: {},
    decision: {
      action: "stay",
      target: null,
      reasons: ["mid_conversation"],
      counterfactual_usd: null,
      applied: false,
    },
    ...over,
  };
}

const TIMELINE = [
  entry("rec_1", 0, {
    decision: {
      action: "stay",
      target: null,
      reasons: ["cold_cache_boundary", "no_target_configured"],
      counterfactual_usd: null,
      applied: false,
    },
  }),
  entry("rec_2", 42, {
    request_class: "subagent",
    agent_type: "Explore",
    decision: {
      action: "route",
      target: { kind: "anthropic", model: "claude-sonnet-5-5" },
      reasons: ["subagent_at_spawn"],
      counterfactual_usd: 0.0011,
      applied: false,
    },
  }),
  entry("rec_3", 95, { status: 429, error_type: "rate_limit_error", cost_usd: null }),
  entry("rec_routed", 160, {
    request_class: "subagent",
    served_model: "claude-sonnet-5-5",
    cost_usd: 0.004,
    baseline_usd: 0.0154,
    decision: {
      action: "route",
      target: { kind: "anthropic", model: "claude-sonnet-5-5" },
      reasons: ["subagent_at_spawn"],
      counterfactual_usd: null,
      applied: true,
    },
  }),
  entry("rec_4", 130, {
    served_model: "claude-sonnet-5-5",
    decision: {
      action: "stay",
      target: null,
      reasons: ["speculative_new_reason"],
      counterfactual_usd: null,
      applied: false,
    },
  }),
];

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
    created_at: at(0),
    updated_at: at(0),
    last_login_at: null,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockControlPlane({
  role = "editor",
  sessions = (_u: URL) => [session("aaaaaaaa-1111", 600), session("bbbbbbbb-2222", 300)],
  detail = (_u: URL) => ({
    session_id: "aaaaaaaa-1111",
    developer_id: "usr_dev",
    totals: totals({
      calls: 4,
      cost_usd: 0.42,
      with_routing_usd: 0.3,
      saving_usd: 0.12,
      routed_calls: 1,
    }),
    requests: TIMELINE,
    truncated: false,
  }),
}: {
  role?: string;
  sessions?: (u: URL) => unknown[];
  detail?: (u: URL) => unknown;
} = {}): URL[] {
  const asked: URL[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const parsed = new URL(url);
      asked.push(parsed);
      if (parsed.pathname === "/auth/status") {
        return json({ setup_required: false, user: user(role), providers: [] });
      }
      if (parsed.pathname === "/gateway/sessions") return json({ sessions: sessions(parsed) });
      if (parsed.pathname.startsWith("/gateway/sessions/")) return json(detail(parsed));
      return json({});
    }),
  );
  return asked;
}

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <SessionsTab />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AI Gateway sessions", () => {
  it("lists a developer's own sessions, most recently active first", async () => {
    const asked = mockControlPlane();
    renderTab();
    const list = await screen.findByRole("list", { name: "Sessions" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("aaaaaaaa");
    expect(rows[0]).toHaveTextContent("4 requests");
    expect(rows[0]).toHaveTextContent("routing would save $0.1200");
    expect(screen.getByText("Your Claude Code sessions")).toBeInTheDocument();
    expect(asked.find((u) => u.pathname === "/gateway/sessions")?.searchParams.get("scope")).toBe(
      "mine",
    );
  });

  it("says what routing saved once a session was routed", async () => {
    mockControlPlane({
      sessions: () => [
        session("aaaaaaaa-1111", 600, {
          totals: totals({
            calls: 4,
            cost_usd: 0.3,
            baseline_usd: 0.42,
            saving_usd: 0.12,
            applied_calls: 2,
          }),
        }),
      ],
    });
    renderTab();
    expect(await screen.findByText("routing saved $0.1200")).toBeInTheDocument();
  });

  it("gives admins everyone's sessions", async () => {
    const asked = mockControlPlane({ role: "admin" });
    renderTab();
    expect(await screen.findByLabelText("Whose sessions")).toHaveValue("org");
    await waitFor(() =>
      expect(
        asked
          .filter((u) => u.pathname === "/gateway/sessions")
          .at(-1)
          ?.searchParams.get("scope"),
      ).toBe("org"),
    );
  });

  it("pages back to older sessions", async () => {
    const full = Array.from({ length: 50 }, (_, i) => session(`s-${i}`, 5000 - i));
    const asked = mockControlPlane({
      sessions: (u) => (u.searchParams.get("before") ? [session("s-older", 1)] : full),
    });
    const ui = userEvent.setup();
    renderTab();
    await ui.click(await screen.findByRole("button", { name: "Older sessions" }));
    expect(await screen.findByText("s-older")).toBeInTheDocument();
    const paged = asked.filter((u) => u.searchParams.get("before"));
    expect(paged[0].searchParams.get("before")).toBe(full[49].last_at);
  });

  it("shows a session's requests in order, with each decision and its reasons", async () => {
    mockControlPlane();
    const ui = userEvent.setup();
    renderTab();
    await ui.click(await screen.findByRole("button", { name: "Open session aaaaaaaa-1111" }));
    const timeline = await screen.findByRole("list", { name: "Session timeline" });
    const rows = within(timeline).getAllByRole("listitem");
    expect(rows.map((r) => r.getAttribute("data-testid"))).toEqual([
      "request-rec_1",
      "request-rec_2",
      "request-rec_3",
      "request-rec_routed",
      "request-rec_4",
    ]);
    // Applied in router mode: routed, and what it saved against the requested model.
    expect(within(rows[3]).getByText("routed → claude-sonnet-5-5 (saved $0.0114)")).toBeTruthy();
    expect(rows[1]).toHaveTextContent("+0:42");
    expect(rows[1]).toHaveTextContent("Explore");
    expect(within(rows[1]).getByText("would route → claude-sonnet-5-5 ($0.0011)")).toBeTruthy();
    expect(
      within(rows[1]).getByLabelText(/^subagent spawn: A subagent starts with an empty cache/),
    ).toBeTruthy();
    expect(within(rows[0]).getByText("cold cache")).toBeInTheDocument();
    expect(within(rows[0]).getByText("no target set")).toBeInTheDocument();
    expect(within(rows[2]).getByText("rate_limit_error · 429")).toBeInTheDocument();
    expect(rows[4]).toHaveTextContent("served by claude-sonnet-5-5");
    // A reason this interface does not know yet shows as itself rather than disappearing.
    expect(within(rows[4]).getByText("speculative_new_reason")).toBeInTheDocument();
    expect(screen.getByText(/0\.1200 saving/)).toBeInTheDocument();
  });

  it("continues a long timeline past its last request", async () => {
    const asked = mockControlPlane({
      detail: (u) => ({
        session_id: "aaaaaaaa-1111",
        developer_id: "usr_dev",
        totals: totals({ calls: 5 }),
        requests: u.searchParams.get("after") ? [entry("rec_5", 200)] : TIMELINE,
        truncated: !u.searchParams.get("after"),
      }),
    });
    const ui = userEvent.setup();
    renderTab();
    await ui.click(await screen.findByRole("button", { name: "Open session aaaaaaaa-1111" }));
    await ui.click(await screen.findByRole("button", { name: "Later requests" }));
    expect(await screen.findByTestId("request-rec_5")).toBeInTheDocument();
    expect(asked.find((u) => u.searchParams.get("after"))?.searchParams.get("after")).toBe("rec_4");
    expect(screen.queryByRole("button", { name: "Later requests" })).not.toBeInTheDocument();
  });

  it("goes back to the list", async () => {
    mockControlPlane();
    const ui = userEvent.setup();
    renderTab();
    await ui.click(await screen.findByRole("button", { name: "Open session aaaaaaaa-1111" }));
    await ui.click(await screen.findByRole("button", { name: /All sessions/ }));
    expect(await screen.findByRole("list", { name: "Sessions" })).toBeInTheDocument();
  });

  it("says so when there are no sessions", async () => {
    mockControlPlane({ sessions: () => [] });
    renderTab();
    expect(await screen.findByText("No sessions in this range")).toBeInTheDocument();
  });

  it("explains every reason the gateway can record", () => {
    for (const [code, reason] of Object.entries(REASONS)) {
      expect(reason.label, code).not.toBe("");
      expect(reason.detail.length, code).toBeGreaterThan(20);
    }
    expect(Object.keys(REASONS)).toHaveLength(27);
  });
});
