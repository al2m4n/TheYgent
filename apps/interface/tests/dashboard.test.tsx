// Tests for the Dashboard home page (RTL over mocked planes). The dashboard reads the two planes'
// /readyz + the inference engines/models + the control-plane run/chat/agent lists, so the whole thing
// is mocked at `fetch`. Two shapes matter: both planes reachable (Online, with live content) and both
// unreachable (Offline, graceful empty state — never a page error).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRouter,
} from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthProvider } from "../src/lib/auth";
import { Dashboard } from "../src/routes/Dashboard";

// A moment in the recent past, so TimeAgo renders a stable "…ago" without a fixed clock.
const HOUR_AGO = new Date(Date.now() - 3_600_000).toISOString();

const READY = { status: "ready" };
const ENGINES = {
  maxResident: 2,
  resident: [
    {
      logicalId: "resident-model",
      engine: "mlx",
      model: "org/resident",
      baseUrl: "http://localhost:9000",
      inflight: 1,
      draining: false,
      priority: 0,
    },
  ],
};
const MODELS = {
  models: [{ logicalId: "cat-model", binding: { binding: "mlx", modality: "chat" } }],
};
const RUNS = {
  runs: [
    {
      id: "run-1",
      session_id: null,
      status: "completed",
      model: "run-model",
      graph_id: "agt-1",
      graph_version: "0.1.0",
      content_hash: null,
      created_at: HOUR_AGO,
      updated_at: HOUR_AGO,
      error: null,
      output: "hi",
    },
    {
      id: "run-2",
      session_id: null,
      status: "failed",
      model: "run-model",
      graph_id: "agt-1",
      graph_version: "0.1.0",
      content_hash: null,
      created_at: HOUR_AGO,
      updated_at: HOUR_AGO,
      error: "boom",
      output: null,
    },
  ],
};
const SESSIONS = {
  sessions: [
    {
      id: "ses-1",
      created_at: HOUR_AGO,
      last_activity: HOUR_AGO,
      message_count: 2,
      preview: "hello there",
      metadata: { title: "Session One" },
    },
  ],
};
const AGENTS = {
  agents: [
    {
      id: "agt-1",
      name: "Agent One",
      created_at: HOUR_AGO,
      updated_at: HOUR_AGO,
      latest_version: "0.1.0",
      latest_content_hash: "abc",
      version_count: 1,
    },
  ],
};

function jsonResponse(data: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => data } as unknown as Response;
}

function pathOf(url: string): string {
  return new URL(url, "http://localhost").pathname.replace(/^\/__[a-z]+/, "");
}

// Route each mocked endpoint by path. Both planes' /readyz answer the same here — the online test only
// needs them reachable, not distinguished by port.
function onlineMock() {
  const fetchMock = vi.fn(async (url: string) => {
    const path = pathOf(url);
    // The dashboard renders inside AuthProvider (its role gates read the signed-in user).
    if (path === "/auth/status")
      return jsonResponse({
        setup_required: false,
        user: {
          id: "usr_1",
          username: "sam",
          display_name: "Sam",
          email: null,
          role: "admin",
          disabled: false,
          has_password: true,
          avatar_url: null,
          created_at: HOUR_AGO,
          updated_at: HOUR_AGO,
          last_login_at: null,
        },
        providers: [],
      });
    if (path === "/readyz") return jsonResponse(READY);
    // Exact overview totals — large numbers so the K/M abbreviation + hover-exact is exercised.
    if (path === "/stats") return jsonResponse({ runs: 2_500_000, sessions: 56, agents: 1234 });
    if (path === "/admin/engines") return jsonResponse(ENGINES);
    if (path === "/admin/models") return jsonResponse(MODELS);
    if (path === "/runs") return jsonResponse(RUNS);
    if (path === "/sessions") return jsonResponse(SESSIONS);
    if (path === "/agents") return jsonResponse(AGENTS);
    if (path === "/admin/mcp/servers")
      return jsonResponse({ servers: [{ name: "s", transport: "stdio", connected: false }] });
    if (path === "/connections")
      return jsonResponse({
        connections: [{ id: "con_1", name: "hub", kind: "mcp_server", config: {} }],
      });
    if (path === "/rag/sources") return jsonResponse({ sources: [] });
    return jsonResponse({});
  });
  vi.stubGlobal("fetch", fetchMock);
}

// Every request rejects — the planes are unreachable. The dashboard must degrade, never throw.
function offlineMock() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new TypeError("network down");
    }),
  );
}

function renderDashboard(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rootRoute = createRootRoute({ component: () => <Dashboard /> });
  const router = createRouter({
    routeTree: rootRoute,
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Dashboard", () => {
  it("shows both planes Online with live content when the planes respond", async () => {
    onlineMock();
    renderDashboard();

    // Both plane cards, both reporting Online.
    expect(await screen.findByText("Inference plane")).toBeInTheDocument();
    expect(screen.getByText("Control plane")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("Online").length).toBe(2));

    // The resident (warm) engine surfaces in the inference card.
    expect(await screen.findByText("resident-model")).toBeInTheDocument();

    // The control-plane card reports total configured MCP servers (honest count, not a lazy
    // "connected" fraction) — merging the name-keyed server and the hub connection.
    expect(await screen.findByText("MCP servers")).toBeInTheDocument();

    // Latest runs / chats / agents / models each render their content.
    await waitFor(() => expect(screen.getAllByText("run-model").length).toBeGreaterThan(0));
    expect(await screen.findByText("Session One")).toBeInTheDocument();
    expect(await screen.findByText("Agent One")).toBeInTheDocument();
    expect(await screen.findByText("cat-model")).toBeInTheDocument();

    // Exact totals from /stats render abbreviated (K/M) with the full number available on hover.
    await waitFor(() => expect(screen.getAllByText("2.5M").length).toBeGreaterThan(0)); // 2,500,000 runs
    expect(screen.getAllByText("1.2K").length).toBeGreaterThan(0); // 1,234 agents
    expect(screen.getAllByTitle("2,500,000").length).toBeGreaterThan(0);
    expect(screen.getAllByTitle("1,234").length).toBeGreaterThan(0);
  });

  it("degrades to Offline for both planes when unreachable — no page error", async () => {
    offlineMock();
    renderDashboard();

    await waitFor(() => expect(screen.getAllByText("Offline").length).toBe(2));
    // The empty panels render their honest notes rather than throwing.
    expect(await screen.findByText(/No runs yet/i)).toBeInTheDocument();
    expect(await screen.findByText(/No conversations yet/i)).toBeInTheDocument();
  });
});
