// The Agents page's drafts strip: work-in-progress drafts render above the published grid, a
// draft editing a published agent badges that agent, and discarding confirms before deleting.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRouter,
} from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const HOUR_AGO = new Date(Date.now() - 3_600_000).toISOString();

vi.mock("../src/lib/api", () => ({
  ApiError: class ApiError extends Error {
    code?: string;
  },
  api: {
    listAgents: vi.fn(async () => [
      {
        id: "agent.pub",
        name: "Published one",
        created_at: HOUR_AGO,
        updated_at: HOUR_AGO,
        latest_version: "0.1.0",
        latest_content_hash: "abc",
        version_count: 1,
      },
    ]),
    listDrafts: vi.fn(async () => [
      {
        id: "drf_new",
        agent_id: null,
        owner_id: null,
        name: "Fresh idea",
        node_count: 3,
        created_at: HOUR_AGO,
        updated_at: HOUR_AGO,
      },
      {
        id: "drf_edit",
        agent_id: "agent.pub",
        owner_id: null,
        name: "Published one",
        node_count: 5,
        created_at: HOUR_AGO,
        updated_at: HOUR_AGO,
      },
    ]),
    deleteDraft: vi.fn(async () => undefined),
    // The grid card lazily fetches the latest version's IR for its preview.
    getAgentVersion: vi.fn(async () => ({
      agent_id: "agent.pub",
      version: "0.1.0",
      content_hash: "abc",
      seq: 1,
      created_at: HOUR_AGO,
      ir: {
        schemaVersion: "1.0",
        id: "agent.pub",
        name: "Published one",
        version: "0.1.0",
        models: {},
        tools: {},
        nodes: [],
        edges: [],
      },
      view: null,
    })),
  },
}));

import { api } from "../src/lib/api";
import { Home } from "../src/routes/Home";

function renderHome(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rootRoute = createRootRoute({ component: () => <Home /> });
  const router = createRouter({
    routeTree: rootRoute,
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("Agents page drafts", () => {
  it("renders the drafts strip with open/discard actions and badges the drafted agent", async () => {
    renderHome();
    await waitFor(() => expect(screen.getByText("Drafts")).toBeInTheDocument());
    expect(screen.getByText("Fresh idea")).toBeInTheDocument();
    expect(screen.getByText("not published yet · 3 nodes · saved")).toBeInTheDocument();
    // Both drafts show a Draft badge in the strip; the published agent wears one too.
    expect(screen.getAllByText(/^draft$/i).length).toBeGreaterThanOrEqual(3);
    expect(screen.getAllByRole("link", { name: "Open" })).toHaveLength(2);
  });

  it("collapses and expands the strip; the count stays visible while folded", async () => {
    renderHome();
    await waitFor(() => expect(screen.getByText("Fresh idea")).toBeInTheDocument());
    const trigger = screen.getByRole("button", { name: /Drafts/ });
    await userEvent.click(trigger);
    expect(screen.queryByText("Fresh idea")).not.toBeInTheDocument();
    expect(trigger).toHaveTextContent("2"); // the folded header still says how many exist
    await userEvent.click(trigger);
    expect(screen.getByText("Fresh idea")).toBeInTheDocument();
  });

  it("discard asks for confirmation, then deletes", async () => {
    renderHome();
    await waitFor(() => expect(screen.getByText("Fresh idea")).toBeInTheDocument());
    const discardButtons = screen.getAllByRole("button", { name: "Discard" });
    await userEvent.click(discardButtons[0]);
    expect(screen.getByText("Discard this draft?")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Discard" })); // the dialog's confirm
    await waitFor(() => expect(api.deleteDraft).toHaveBeenCalledTimes(1));
  });
});
