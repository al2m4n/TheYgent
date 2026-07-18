// The Agents page's export/delete actions: per-agent Export downloads the latest version's IR as
// one JSON file (view re-embedded), Delete flows through the shared ConfirmDialog to
// api.deleteAgent, and Select mode drives the bulk bar (select-all, sequential bulk delete,
// agents-only bulk export).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRouter,
} from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const HOUR_AGO = new Date(Date.now() - 3_600_000).toISOString();

const IR = {
  schemaVersion: "1.0",
  id: "agent.pub",
  name: "Published one",
  version: "0.1.0",
  contentHash: "abc",
  models: {},
  tools: {},
  nodes: [],
  edges: [],
};

const mocks = vi.hoisted(() => ({
  listAgents: vi.fn(),
  listDrafts: vi.fn(),
  getAgent: vi.fn(),
  getAgentVersion: vi.fn(),
  deleteAgent: vi.fn(),
}));

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>();
  return { ...actual, api: { ...actual.api, ...mocks } };
});

import { Home } from "../src/routes/Home";

// jsdom implements neither URL.createObjectURL nor a navigating anchor click — stub both so a
// triggered download is observable as its filename.
const downloads: string[] = [];
beforeAll(() => {
  Object.assign(URL, {
    createObjectURL: vi.fn(() => "blob:agents-test"),
    revokeObjectURL: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloads.push(this.download);
  });
});

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
  downloads.length = 0;
  mocks.listAgents.mockResolvedValue([
    {
      id: "agent.pub",
      name: "Published one",
      created_at: HOUR_AGO,
      updated_at: HOUR_AGO,
      latest_version: "0.1.0",
      latest_content_hash: "abc",
      version_count: 1,
    },
  ]);
  mocks.listDrafts.mockResolvedValue([]);
  mocks.getAgent.mockResolvedValue({
    id: "agent.pub",
    name: "Published one",
    created_at: HOUR_AGO,
    updated_at: HOUR_AGO,
    versions: [{ version: "0.1.0", content_hash: "abc", seq: 1, created_at: HOUR_AGO }],
  });
  mocks.getAgentVersion.mockResolvedValue({
    agent_id: "agent.pub",
    version: "0.1.0",
    content_hash: "abc",
    seq: 1,
    created_at: HOUR_AGO,
    ir: IR,
    view: { nodes: { n1: { position: { x: 1, y: 2 } } } },
  });
  mocks.deleteAgent.mockResolvedValue(undefined);
});

describe("Agents page export/delete", () => {
  it("Export downloads the latest version as theygent-agent-<id>-v<version>.json", async () => {
    renderHome();
    await userEvent.click(await screen.findByRole("button", { name: "Export Published one" }));
    await waitFor(() => expect(downloads).toEqual(["theygent-agent-agent.pub-v0.1.0.json"]));
    expect(mocks.getAgent).toHaveBeenCalledWith("agent.pub");
    expect(mocks.getAgentVersion).toHaveBeenCalledWith("agent.pub", "0.1.0");
  });

  it("Delete asks through the shared ConfirmDialog and arms only on the typed name", async () => {
    renderHome();
    await userEvent.click(await screen.findByRole("button", { name: "Delete Published one" }));
    // The dialog carries the spec'd copy: versions+triggers go, runs and chats are kept.
    expect(screen.getByText("Delete Published one?")).toBeInTheDocument();
    expect(screen.getByText(/ALL its versions and triggers/)).toBeInTheDocument();
    expect(screen.getByText(/Past runs and chats are kept/)).toBeInTheDocument();
    expect(mocks.deleteAgent).not.toHaveBeenCalled();

    // Type-to-confirm: the confirm button stays disabled until the agent's exact name is typed.
    const confirm = screen.getByRole("button", { name: "Delete" });
    expect(confirm).toBeDisabled();
    const challenge = screen.getByLabelText(/Type .* to confirm/);
    await userEvent.type(challenge, "Published");
    expect(confirm).toBeDisabled();
    await userEvent.clear(challenge);
    await userEvent.type(challenge, "Published one");
    expect(confirm).toBeEnabled();

    await userEvent.click(confirm);
    await waitFor(() => expect(mocks.deleteAgent).toHaveBeenCalledWith("agent.pub"));
  });

  it("cancelling the confirm never deletes", async () => {
    renderHome();
    await userEvent.click(await screen.findByRole("button", { name: "Delete Published one" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(mocks.deleteAgent).not.toHaveBeenCalled();
  });

  it("Select mode: select-all + bulk delete confirms once, then deletes each id", async () => {
    renderHome();
    await userEvent.click(await screen.findByRole("button", { name: "Select" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select all" }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    expect(screen.getByText("Delete 1 agent?")).toBeInTheDocument();
    // Bulk delete arms on the typed selection count.
    await userEvent.type(screen.getByLabelText(/Type .* to confirm/), "1");
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(mocks.deleteAgent).toHaveBeenCalledWith("agent.pub"));
    expect(mocks.deleteAgent).toHaveBeenCalledTimes(1);
  });

  it("Select mode: bulk export downloads an agents-only bundle zip", async () => {
    renderHome();
    await userEvent.click(await screen.findByRole("button", { name: "Select" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Published one" }));
    await userEvent.click(screen.getByRole("button", { name: "Export selected" }));
    await waitFor(() => expect(downloads).toHaveLength(1));
    expect(downloads[0]).toMatch(/^theygent-export-\d{4}-\d{2}-\d{2}\.zip$/);
    // The bundle is assembled from the registry read surface — every version fetched.
    expect(mocks.getAgent).toHaveBeenCalledWith("agent.pub");
    expect(mocks.getAgentVersion).toHaveBeenCalledWith("agent.pub", "0.1.0");
  });

  it("a per-row delete while selected removes the id from the selection", async () => {
    renderHome();
    await userEvent.click(await screen.findByRole("button", { name: "Select" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select Published one" }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    // The per-row Delete stays available in select mode — a successful delete must also drop the
    // id from the selection, or the stale id would abort bulk export and fake bulk-delete failures.
    await userEvent.click(screen.getByRole("button", { name: "Delete Published one" }));
    await userEvent.type(screen.getByLabelText(/Type .* to confirm/), "Published one");
    await userEvent.click(screen.getByRole("button", { name: "Delete" })); // the dialog's confirm
    await waitFor(() => expect(mocks.deleteAgent).toHaveBeenCalledWith("agent.pub"));
    await waitFor(() => expect(screen.getByText("0 selected")).toBeInTheDocument());
  });

  it("bulk delete deselects each success; a failed id stays selected", async () => {
    mocks.listAgents.mockResolvedValue([
      {
        id: "agent.pub",
        name: "Published one",
        created_at: HOUR_AGO,
        updated_at: HOUR_AGO,
        latest_version: "0.1.0",
        latest_content_hash: "abc",
        version_count: 1,
      },
      {
        id: "agent.stuck",
        name: "Stuck one",
        created_at: HOUR_AGO,
        updated_at: HOUR_AGO,
        latest_version: "0.1.0",
        latest_content_hash: "def",
        version_count: 1,
      },
    ]);
    mocks.deleteAgent.mockImplementation(async (id: string) => {
      if (id === "agent.stuck") throw new Error("boom");
    });

    renderHome();
    await userEvent.click(await screen.findByRole("button", { name: "Select" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Select all" }));
    expect(screen.getByText("2 selected")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete selected" }));
    await userEvent.type(screen.getByLabelText(/Type .* to confirm/), "2");
    await userEvent.click(screen.getByRole("button", { name: "Delete" })); // the dialog's confirm
    await waitFor(() => expect(mocks.deleteAgent).toHaveBeenCalledTimes(2));
    // The deleted id left the set; the failed one is still selected for a retry.
    await waitFor(() => expect(screen.getByText("1 selected")).toBeInTheDocument());
    expect(screen.getByRole("checkbox", { name: "Select Stuck one" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Select Published one" })).not.toBeChecked();
  });
});
