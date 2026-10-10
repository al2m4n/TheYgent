// Tests for the AI Gateway page's Overview tab (RTL over a mocked control plane): the summary
// tiles, each instance's heartbeat (online, mode, whether it runs the latest config, upstream
// health), the not-yet-configured note, the empty state, and a failing control plane.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OverviewTab } from "../src/components/gateway/OverviewTab";
import { Gateway } from "../src/routes/Gateway";

const MINUTE_AGO = new Date(Date.now() - 60_000).toISOString();
const DAY_AGO = new Date(Date.now() - 86_400_000).toISOString();

const CONFIG = {
  version: 3,
  mode: "observer",
  policy: {
    subagent_target: "claude-sonnet-5-5",
    main_target: null,
    local_target: null,
    warm_switch_margin: 1.5,
  },
  fingerprints: [],
  pricing_overrides: {},
  updated_at: MINUTE_AGO,
  updated_by: "usr_admin",
};

function instance(overrides: Record<string, unknown>) {
  return {
    gateway_id: "gw-1",
    software_version: "0.1.0",
    mode: "observer",
    config_version: 3,
    config_current: true,
    key_set_version: 7,
    started_at: DAY_AGO,
    spool_backlog: 0,
    upstream_reachable: true,
    first_seen_at: DAY_AGO,
    last_seen_at: MINUTE_AGO,
    online: true,
    service_key_id: "key_1",
    ...overrides,
  };
}

const LIVE = instance({ gateway_id: "gw-live", spool_backlog: 12 });
const BEHIND = instance({
  gateway_id: "gw-behind",
  online: false,
  mode: "passthrough",
  config_version: 2,
  config_current: false,
  upstream_reachable: false,
  spool_backlog: 30,
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockControlPlane(config: () => Response, instances: () => Response): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = new URL(url).pathname;
      if (path === "/gateway/config") return config();
      if (path === "/gateway/instances") return instances();
      return json({});
    }),
  );
}

function tile(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

function renderTab(ui = <OverviewTab />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AI Gateway overview", () => {
  it("summarises the config and every instance's heartbeat", async () => {
    mockControlPlane(
      () => json(CONFIG),
      () => json({ instances: [LIVE, BEHIND] }),
    );
    renderTab();

    const mode = await screen.findByRole("region", { name: "Mode" });
    expect(within(mode).getByText("observer")).toBeInTheDocument();
    expect(within(mode).getByText("records every exchange")).toBeInTheDocument();
    expect(within(tile("Config")).getByText("v3")).toBeInTheDocument();
    expect(within(tile("Gateways online")).getByText("1 / 2")).toBeInTheDocument();
    expect(within(tile("Waiting to export")).getByText("42")).toBeInTheDocument(); // 12 + 30

    const live = screen.getByTestId("gateway-gw-live");
    expect(within(live).getByText("online")).toBeInTheDocument();
    expect(within(live).getByText("config v3")).toBeInTheDocument();
    expect(within(live).queryByText("upstream unreachable")).not.toBeInTheDocument();

    const behind = screen.getByTestId("gateway-gw-behind");
    expect(within(behind).getByText("offline")).toBeInTheDocument();
    expect(within(behind).getByText(/config v2 · latest v3/)).toBeInTheDocument();
    expect(within(behind).getByText("upstream unreachable")).toBeInTheDocument();
    expect(within(behind).getByText("30 waiting to export")).toBeInTheDocument();
    expect(screen.queryByText(/No gateway config has been saved yet/)).not.toBeInTheDocument();
  });

  it("says when no config has been saved: gateways then record nothing", async () => {
    mockControlPlane(
      () =>
        json(
          {
            error: {
              message: "no gateway config has been saved yet",
              code: "gateway_config_not_found",
            },
          },
          404,
        ),
      () =>
        json({
          instances: [
            instance({ config_version: null, config_current: true, mode: "passthrough" }),
          ],
        }),
    );
    renderTab();

    expect(await screen.findByText(/No gateway config has been saved yet/)).toBeInTheDocument();
    expect(within(tile("Mode")).getByText("passthrough")).toBeInTheDocument();
    expect(within(tile("Mode")).getByText("not configured")).toBeInTheDocument();
    expect(within(tile("Config")).getByText("none saved yet")).toBeInTheDocument();
    expect(screen.getByText("no config")).toBeInTheDocument();
  });

  it("explains how a gateway reports in when none has", async () => {
    mockControlPlane(
      () => json(CONFIG),
      () => json({ instances: [] }),
    );
    renderTab();

    expect(await screen.findByText("No gateway has reported in")).toBeInTheDocument();
    expect(within(tile("Gateways online")).getByText("0 / 0")).toBeInTheDocument();
    expect(screen.getByText("none reported yet")).toBeInTheDocument();
  });

  it("shows the control plane's error instead of an empty page", async () => {
    mockControlPlane(
      () => json(CONFIG),
      () => json({ error: { message: "database unavailable", code: "db_down" } }, 503),
    );
    renderTab();

    expect(await screen.findByText("database unavailable")).toBeInTheDocument();
    expect(screen.queryByText("No gateway has reported in")).not.toBeInTheDocument();
  });

  it("is the AI Gateway page's first tab", async () => {
    mockControlPlane(
      () => json(CONFIG),
      () => json({ instances: [LIVE] }),
    );
    renderTab(<Gateway />);

    expect(screen.getByRole("heading", { name: "AI Gateway" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(screen.getByTestId("gateway-gw-live")).toBeInTheDocument());
  });
});
