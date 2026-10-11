// Tests for the AI Gateway page's Report tab (RTL over a mocked control plane): the headline
// figures, setup warnings, spend by day, the breakdown and its grouping, subscription seats, who
// sees what (own records vs the organization), the default window, and the empty state.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReportTab } from "../src/components/gateway/ReportTab";
import { AuthProvider } from "../src/lib/auth";

const HOUR_AGO = new Date(Date.now() - 3_600_000).toISOString();

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
    shadow_calls: 0,
    shadow_answered_calls: 0,
    shadow_cost_usd: 0,
    shadow_output_tokens: 0,
    shadowed_output_tokens: 0,
    shadow_compared_calls: 0,
    shadow_agreed_calls: 0,
    ...over,
    // Without applied routes, what it would cost as requested is what it cost.
    baseline_usd: over.baseline_usd ?? over.cost_usd ?? 0,
  };
}

const TOTAL = totals({
  calls: 25,
  errors: 2,
  input_tokens: 3_462,
  output_tokens: 4_563,
  cache_read_tokens: 309_161,
  cache_write_tokens: 336_279,
  cost_usd: 2.6274,
  with_routing_usd: 2.5512,
  saving_usd: 0.0762,
  routed_calls: 11,
  routed_local_calls: 1,
});

function reportBody(over: Record<string, unknown> = {}) {
  return {
    since: HOUR_AGO,
    until: new Date().toISOString(),
    developer_id: null,
    first: HOUR_AGO,
    last: HOUR_AGO,
    total: TOTAL,
    groups: {
      day: [
        {
          key: "2026-10-05",
          ...totals({ calls: 5, cost_usd: 1, with_routing_usd: 0.5, saving_usd: 0.5 }),
        },
        { key: "2026-10-06", ...totals({ calls: 20, cost_usd: 1.6274, with_routing_usd: 2.0512 }) },
      ],
      class: [
        {
          key: "subagent",
          ...totals({ calls: 11, cost_usd: 0.2457, with_routing_usd: 0.1695, saving_usd: 0.0762 }),
        },
        { key: "main", ...totals({ calls: 10, cost_usd: 2.3269, with_routing_usd: 2.3269 }) },
      ],
      client: [{ key: "cli", ...totals({ calls: 25, cost_usd: 2.6274 }) }],
      model: [{ key: "claude-opus-5-5", ...totals({ calls: 25, cost_usd: 2.6274 }) }],
    },
    pricing_versions: ["2026-09-25"],
    served_differs: true,
    warnings: [{ code: "tool_search_off", calls: 2, values: [] }],
    seats: [],
    summarized_before: null,
    ...over,
  };
}

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

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockControlPlane(role: string, report: () => Response): URL[] {
  const asked: URL[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const parsed = new URL(url);
      if (parsed.pathname === "/auth/status") {
        return json({ setup_required: false, user: user(role), providers: [] });
      }
      if (parsed.pathname === "/gateway/developers") {
        return json({
          developers: [
            {
              user_id: "usr_ada",
              username: "ada",
              display_name: "Ada",
              disabled: false,
              credential_profile: "passthrough",
              live_gateway_keys: 1,
              updated_at: null,
              updated_by: null,
            },
          ],
        });
      }
      if (parsed.pathname === "/gateway/report") {
        asked.push(parsed);
        return report();
      }
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
        <ReportTab />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

function tile(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AI Gateway report", () => {
  it("shows spend, what routing would have cost, and the saving", async () => {
    mockControlPlane("admin", () => json(reportBody()));
    renderTab();
    expect(await screen.findByRole("region", { name: "Spend" })).toHaveTextContent("$2.63");
    expect(tile("With routing")).toHaveTextContent("$2.55");
    expect(tile("Saving")).toHaveTextContent("$0.0762");
    expect(tile("Saving")).toHaveTextContent("2.9% of spend");
    expect(tile("Calls")).toHaveTextContent("25");
    expect(tile("Calls")).toHaveTextContent("11 would route · 1 would not reach the API");
  });

  it("shows what routing saved once the gateway routes", async () => {
    // Router mode: 2.00 spent, 3.00 as requested; 4 of 6 routes applied, 2 only observed.
    const routedTotals = totals({
      calls: 10,
      cost_usd: 2,
      baseline_usd: 3,
      with_routing_usd: 1.8,
      saving_usd: 1.2,
      routed_calls: 6,
      applied_calls: 4,
    });
    mockControlPlane("admin", () =>
      json(
        reportBody({
          total: routedTotals,
          groups: {
            day: [{ key: "2026-10-07", ...routedTotals }],
            class: [{ key: "subagent", ...routedTotals }],
          },
          warnings: [],
        }),
      ),
    );
    renderTab();
    expect(await screen.findByRole("region", { name: "Spend" })).toHaveTextContent(
      "$3.00 without routing",
    );
    expect(tile("Saving")).toHaveTextContent("40.0% of the cost without routing");
    expect(tile("Calls")).toHaveTextContent("4 routed · 2 would route");
    const byClass = screen.getByRole("table", { name: "By request class" });
    expect(within(byClass).getByText("Without routing")).toBeInTheDocument();
    const days = screen.getByRole("list", { name: "Spend by day" });
    expect(
      within(days).getByLabelText(
        "2026-10-07: $2.00 spent, $3.00 without routing, $1.80 with routing",
      ),
    ).toBeTruthy();
  });

  it("keeps the observer view when nothing was routed", async () => {
    mockControlPlane("admin", () => json(reportBody()));
    renderTab();
    await screen.findByRole("region", { name: "Spend" });
    expect(tile("Spend")).not.toHaveTextContent("without routing");
    expect(
      within(screen.getByRole("table", { name: "By request class" })).queryByText(
        "Without routing",
      ),
    ).toBeNull();
  });

  it("flags setup problems with their fix", async () => {
    mockControlPlane("editor", () => json(reportBody()));
    renderTab();
    expect(await screen.findByText(/tool search is off/)).toBeInTheDocument();
    expect(screen.getByText("ENABLE_TOOL_SEARCH=true")).toBeInTheDocument();
  });

  it("draws each day's spend against what routing would have cost", async () => {
    mockControlPlane("admin", () => json(reportBody()));
    renderTab();
    const days = await screen.findByRole("list", { name: "Spend by day" });
    expect(
      within(days).getByLabelText("2026-10-05: $1.00 spent, $0.5000 with routing"),
    ).toBeTruthy();
    expect(within(days).getAllByRole("listitem")).toHaveLength(2);
  });

  it("shows what shadow runs measured, per model, only when there were any", async () => {
    const shadows = { shadow_calls: 4, shadow_answered_calls: 3, shadow_cost_usd: 0.0123 };
    const sonnet = {
      ...shadows,
      shadow_output_tokens: 240,
      shadowed_output_tokens: 300,
      shadow_compared_calls: 3,
      shadow_agreed_calls: 2,
    };
    mockControlPlane("admin", () =>
      json(
        reportBody({
          total: { ...TOTAL, ...sonnet, shadow_calls: 5 },
          groups: {
            ...reportBody().groups,
            // Grouped by the model a shadow ran on: a local trial's model is not its decision's
            // target, so this is where the local model's evidence is.
            "shadow-model": [
              { key: "(none)", ...totals({ calls: 14 }) },
              { key: "claude-sonnet-5-5", ...totals({ calls: 8, ...sonnet }) },
              { key: "aux-local", ...totals({ calls: 3, shadow_calls: 1 }) },
            ],
          },
        }),
      ),
    );
    renderTab();
    const table = await screen.findByRole("table", { name: "Shadow runs" });
    const sonnetRow = within(table).getByText("claude-sonnet-5-5").closest("tr");
    expect(sonnetRow?.textContent).toContain("75.0%"); // usable: 3 of 4
    expect(sonnetRow?.textContent).toContain("80.0%"); // wrote 240 where Opus wrote 300
    expect(sonnetRow?.textContent).toContain("66.7%"); // the same next action on 2 of 3 turns
    const localRow = within(table).getByText("aux-local").closest("tr");
    expect(localRow?.textContent).toContain("—"); // nothing usable to compare
    expect(within(table).queryByText("(none)")).toBeNull();
  });

  it("says from when the figures come from archived daily totals", async () => {
    mockControlPlane("admin", () => json(reportBody({ summarized_before: "2026-07-13" })));
    renderTab();
    expect(
      await screen.findByText(/Days before 2026-07-13 are archived: their figures come from daily/),
    ).toBeInTheDocument();
  });

  it("has no shadow runs section without shadow runs", async () => {
    mockControlPlane("admin", () => json(reportBody()));
    renderTab();
    await screen.findByRole("table", { name: "By request class" });
    expect(screen.queryByRole("table", { name: "Shadow runs" })).toBeNull();
  });

  it("groups the breakdown by the chosen dimension", async () => {
    const asked = mockControlPlane("admin", () => json(reportBody()));
    const ui = userEvent.setup();
    renderTab();
    const byClass = await screen.findByRole("table", { name: "By request class" });
    expect(within(byClass).getByText("subagent")).toBeInTheDocument();
    expect(asked[0].searchParams.get("by")).toBe("day,class,shadow-model");

    await ui.selectOptions(screen.getByLabelText("Group by"), "model");
    await screen.findByRole("table", { name: "By model requested" });
    expect(asked.at(-1)?.searchParams.get("by")).toBe("day,model,shadow-model");
    expect(screen.getByText(/Grouped by the model Claude Code requested/)).toBeInTheDocument();
  });

  it("starts admins on everyone and others on their own records", async () => {
    const adminAsked = mockControlPlane("admin", () => json(reportBody()));
    const first = renderTab();
    await screen.findByRole("region", { name: "Spend" });
    await waitFor(() => expect(adminAsked.at(-1)?.searchParams.get("scope")).toBe("org"));
    first.unmount();

    const devAsked = mockControlPlane("editor", () => json(reportBody()));
    renderTab();
    await screen.findByRole("region", { name: "Spend" });
    await waitFor(() => expect(devAsked.at(-1)?.searchParams.get("scope")).toBe("mine"));
  });

  it("keeps people out of the organization view below admin", async () => {
    const asked = mockControlPlane("editor", () => json(reportBody()));
    const ui = userEvent.setup();
    renderTab();
    await screen.findByRole("region", { name: "Spend" });
    const groupBy = screen.getByLabelText("Group by");
    expect(within(groupBy).getByRole("option", { name: "Developer" })).toBeInTheDocument();
    await ui.selectOptions(groupBy, "developer");

    await ui.selectOptions(screen.getByLabelText("Whose usage"), "org");
    await waitFor(() => expect(asked.at(-1)?.searchParams.get("scope")).toBe("org"));
    expect(asked.at(-1)?.searchParams.get("by")).toBe("day,class,shadow-model"); // never by developer
    const orgGroupBy = screen.getByLabelText("Group by");
    expect(within(orgGroupBy).queryByRole("option", { name: "Developer" })).toBeNull();
    expect(within(orgGroupBy).queryByRole("option", { name: "Session" })).toBeNull();
  });

  it("covers the last week unless asked otherwise", async () => {
    const asked = mockControlPlane("admin", () => json(reportBody()));
    renderTab();
    await screen.findByRole("region", { name: "Spend" });
    const since = Date.parse(asked[0].searchParams.get("since") ?? "");
    expect(Math.abs(Date.now() - 7 * 86_400_000 - since)).toBeLessThan(60_000);
  });

  it("lists subscription seats when there are some", async () => {
    mockControlPlane("admin", () =>
      json(
        reportBody({
          seats: [
            {
              developer_id: "usr_ada",
              latest_at: HOUR_AGO,
              five_hour_utilization: 0.32,
              seven_day_utilization: 0.05,
              five_hour_resets_at: null,
              seven_day_resets_at: null,
              five_hour_used: 0.17,
              seven_day_used: 0.03,
            },
          ],
        }),
      ),
    );
    renderTab();
    const seats = await screen.findByRole("table", { name: "Subscription seats" });
    // An admin sees the developer's username rather than their account id.
    expect(await within(seats).findByText("ada")).toBeInTheDocument();
    expect(within(seats).getByText("+17% / +3%")).toBeInTheDocument();
  });

  it("says so when the range has no traffic", async () => {
    mockControlPlane("admin", () =>
      json(reportBody({ total: totals(), groups: {}, warnings: [] })),
    );
    renderTab();
    expect(await screen.findByText("No traffic recorded in this range")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Spend" })).not.toBeInTheDocument();
  });

  it("shows the control plane's error", async () => {
    mockControlPlane("admin", () =>
      json({ error: { message: "report unavailable", code: "db_down" } }, 503),
    );
    renderTab();
    expect(await screen.findByText("report unavailable")).toBeInTheDocument();
  });
});
