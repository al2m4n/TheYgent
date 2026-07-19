// The Agents page's API-access dialog: per-agent endpoints assembled from the agent id, an example
// body derived from the published IR's `$in.<port>.<field>` references, durable-only gating, and a
// triggers section that only renders for callers allowed to list triggers (a viewer's 403 hides it).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRouter,
} from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { IRDocument } from "@theygent/ir-types";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const HOUR_AGO = new Date(Date.now() - 3_600_000).toISOString();

// A two-field agent: the input boundary feeds two activity nodes that drill `$in.in.path` and
// `$in.in.question`, so the derived example input is an object with exactly those fields.
const MULTI_INPUT_IR = {
  schemaVersion: "1.0",
  id: "agent.pub",
  name: "Published one",
  version: "0.1.0",
  contentHash: "abc",
  models: {},
  tools: {},
  nodes: [
    {
      id: "n_in",
      type: "input",
      kind: "boundary",
      config: {},
      ports: { in: [], out: [{ id: "out" }] },
    },
    {
      id: "n_read",
      type: "tool",
      kind: "activity",
      config: { tool: "echo", args: { value: "$in.in.path" } },
      ports: { in: [{ id: "in" }], out: [{ id: "ok" }] },
    },
    {
      id: "n_q",
      type: "tool",
      kind: "activity",
      config: { tool: "echo", args: { value: "$in.in.question" } },
      ports: { in: [{ id: "in" }], out: [{ id: "ok" }] },
    },
    {
      id: "n_out",
      type: "output",
      kind: "boundary",
      config: {},
      ports: { in: [{ id: "in" }], out: [] },
    },
  ],
  edges: [
    {
      id: "e1",
      source: "n_in",
      sourceHandle: "out",
      target: "n_read",
      targetHandle: "in",
      channel: "data",
    },
    {
      id: "e2",
      source: "n_in",
      sourceHandle: "out",
      target: "n_q",
      targetHandle: "in",
      channel: "data",
    },
    {
      id: "e3",
      source: "n_read",
      sourceHandle: "ok",
      target: "n_out",
      targetHandle: "in",
      channel: "data",
    },
  ],
} as unknown as IRDocument;

const STRING_INPUT_IR = {
  ...MULTI_INPUT_IR,
  nodes: [
    {
      id: "n_in",
      type: "input",
      kind: "boundary",
      config: {},
      ports: { in: [], out: [{ id: "out" }] },
    },
    {
      id: "n_out",
      type: "output",
      kind: "boundary",
      config: {},
      ports: { in: [{ id: "in" }], out: [] },
    },
  ],
  edges: [
    {
      id: "e1",
      source: "n_in",
      sourceHandle: "out",
      target: "n_out",
      targetHandle: "in",
      channel: "data",
    },
  ],
} as unknown as IRDocument;

const DURABLE_IR = {
  ...STRING_INPUT_IR,
  nodes: [
    ...(STRING_INPUT_IR.nodes ?? []),
    {
      id: "n_gate",
      type: "human",
      kind: "boundary",
      config: {},
      ports: { in: [{ id: "in" }], out: [{ id: "ok" }] },
    },
  ],
} as unknown as IRDocument;

const mocks = vi.hoisted(() => ({
  listAgents: vi.fn(),
  listDrafts: vi.fn(),
  getAgentVersion: vi.fn(),
  listTriggers: vi.fn(),
}));

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>();
  return { ...actual, api: { ...actual.api, ...mocks } };
});

import { deriveExampleInput } from "../src/components/ApiAccessModal";
import { Home } from "../src/routes/Home";

const clipboardWrites: string[] = [];
beforeAll(() => {
  // jsdom has no navigator.clipboard — observe copies through a stub.
  Object.assign(navigator, {
    clipboard: {
      writeText: vi.fn((text: string) => {
        clipboardWrites.push(text);
        return Promise.resolve();
      }),
    },
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

function storedVersion(ir: IRDocument) {
  return {
    agent_id: "agent.pub",
    version: "0.1.0",
    content_hash: "abc",
    seq: 1,
    created_at: HOUR_AGO,
    ir,
    view: {},
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  clipboardWrites.length = 0;
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
  mocks.getAgentVersion.mockResolvedValue(storedVersion(MULTI_INPUT_IR));
  mocks.listTriggers.mockResolvedValue([]);
});

async function openDialog(): Promise<void> {
  renderHome();
  await userEvent.click(
    await screen.findByRole("button", { name: "API access for Published one" }),
  );
  await screen.findByText("API access · Published one");
}

describe("deriveExampleInput", () => {
  it("derives object fields from $in.<port>.<field> on nodes fed by the input boundary", () => {
    expect(deriveExampleInput(MULTI_INPUT_IR)).toEqual({ path: "<path>", question: "<question>" });
  });

  it("falls back to a plain string when nothing drills into the input", () => {
    expect(deriveExampleInput(STRING_INPUT_IR)).toBe("Hello!");
  });

  it("keeps a hyphenated field intact in a whole-string ref instead of truncating it", () => {
    const ir = {
      ...MULTI_INPUT_IR,
      nodes: (MULTI_INPUT_IR.nodes ?? []).map((n) =>
        n.id === "n_read"
          ? { ...n, config: { tool: "echo", args: { value: "$in.in.user-id" } } }
          : n,
      ),
    } as unknown as IRDocument;
    // A whole-string ref may use any dot-free field name — "user-id" must not become "user",
    // which would advertise a field the agent never reads.
    expect(deriveExampleInput(ir)).toEqual({ "user-id": "<user-id>", question: "<question>" });
  });

  it("captures an inline template token embedded in prose", () => {
    const ir = {
      ...MULTI_INPUT_IR,
      nodes: (MULTI_INPUT_IR.nodes ?? []).map((n) =>
        n.id === "n_read"
          ? { ...n, config: { tool: "echo", args: { value: "The file is: $in.in.path thanks" } } }
          : n,
      ),
    } as unknown as IRDocument;
    expect(deriveExampleInput(ir)).toEqual({ path: "<path>", question: "<question>" });
  });

  it("ignores $in drilling on nodes NOT fed by the input boundary", () => {
    const ir = {
      ...STRING_INPUT_IR,
      nodes: [
        ...(STRING_INPUT_IR.nodes ?? []),
        {
          id: "n_far",
          type: "tool",
          kind: "activity",
          config: { tool: "echo", args: { value: "$in.in.notAnInputField" } },
          ports: { in: [{ id: "in" }], out: [{ id: "ok" }] },
        },
      ],
    } as unknown as IRDocument;
    expect(deriveExampleInput(ir)).toBe("Hello!");
  });
});

describe("Agents page API access dialog", () => {
  it("shows the run + invoke endpoints for this agent with the derived example body", async () => {
    await openDialog();
    // The run sections wait for the IR fetch to settle (the IR decides durable-only gating).
    expect(await screen.findByText(/agents\/agent\.pub\/runs/)).toBeInTheDocument();
    expect(screen.getByText(/agents\/agent\.pub\/invoke/)).toBeInTheDocument();
    // The example body carries the input fields drilled out of the IR.
    expect(screen.getAllByText(/"path":\s*"<path>"/).length).toBeGreaterThan(0);
    expect(screen.getByText(/THEYGENT_INVOKE_TOKEN/)).toBeInTheDocument();
    // The pin hint names the latest published version.
    expect(screen.getByText(/omitted means latest/)).toBeInTheDocument();
  });

  it("copies a snippet to the clipboard", async () => {
    await openDialog();
    await userEvent.click(await screen.findByRole("button", { name: "Copy invoke request" }));
    expect(clipboardWrites).toHaveLength(1);
    expect(clipboardWrites[0]).toContain("/agents/agent.pub/invoke");
    expect(clipboardWrites[0]).toContain('"question":"<question>"');
  });

  it("a durable-only agent gets the durable-runs endpoint instead of run/invoke", async () => {
    mocks.getAgentVersion.mockResolvedValue(storedVersion(DURABLE_IR));
    // An existing token-invoke trigger must point at the durable path too, not the hidden
    // invoke section.
    mocks.listTriggers.mockResolvedValue([
      {
        id: "trg_http",
        agent_id: "agent.pub",
        version: "0.1.0",
        content_hash: null,
        kind: "http",
        config: {},
        enabled: true,
        last_fired_at: null,
        created_at: HOUR_AGO,
        updated_at: HOUR_AGO,
      },
    ]);
    await openDialog();
    await screen.findByText(/durable-runs/);
    expect(screen.queryByText(/agents\/agent\.pub\/invoke/)).not.toBeInTheDocument();
    expect(screen.getByText(/durable_required/)).toBeInTheDocument();
    // The human node also surfaces the resume path.
    expect(screen.getByText(/\/resume/)).toBeInTheDocument();
    expect(await screen.findByText(/runs only durably/)).toBeInTheDocument();
  });

  it("falls back to the interactive sections with a visible caveat when the IR fetch fails", async () => {
    mocks.getAgentVersion.mockRejectedValue(new Error("boom"));
    await openDialog();
    expect(await screen.findByText(/could not be loaded/)).toBeInTheDocument();
    expect(screen.getByText(/agents\/agent\.pub\/runs/)).toBeInTheDocument();
  });

  it("lists this agent's triggers with the webhook fire recipe", async () => {
    mocks.listTriggers.mockResolvedValue([
      {
        id: "trg_1",
        agent_id: "agent.pub",
        version: "0.1.0",
        content_hash: null,
        kind: "webhook",
        config: { secret: "***" },
        enabled: true,
        last_fired_at: null,
        created_at: HOUR_AGO,
        updated_at: HOUR_AGO,
      },
      {
        id: "trg_other",
        agent_id: "agent.other",
        version: "0.1.0",
        content_hash: null,
        kind: "webhook",
        config: { secret: "***" },
        enabled: true,
        last_fired_at: null,
        created_at: HOUR_AGO,
        updated_at: HOUR_AGO,
      },
    ]);
    await openDialog();
    await screen.findByText(/hooks\/trg_1/);
    expect(screen.getAllByText(/X-Theygent-Signature/).length).toBeGreaterThan(0);
    // Another agent's trigger never leaks into this dialog.
    expect(screen.queryByText(/trg_other/)).not.toBeInTheDocument();
  });

  it("hides the triggers section entirely when listing them is forbidden (viewer)", async () => {
    mocks.listTriggers.mockRejectedValue(new Error("forbidden"));
    await openDialog();
    expect(screen.queryByText("Triggers")).not.toBeInTheDocument();
  });

  it("the API button is disabled for an agent with no published version", async () => {
    mocks.listAgents.mockResolvedValue([
      {
        id: "agent.empty",
        name: "Empty one",
        created_at: HOUR_AGO,
        updated_at: HOUR_AGO,
        latest_version: null,
        latest_content_hash: null,
        version_count: 0,
      },
    ]);
    renderHome();
    expect(await screen.findByRole("button", { name: "API access for Empty one" })).toBeDisabled();
  });
});
