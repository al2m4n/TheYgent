// Settings → Samples: the sample catalog renders with capability badges and per-slot model
// pickers; Install is gated until every slot the sample declares is filled, sends the chosen
// {logical_id, binding} pairs, and an installed sample shows its state instead of the button.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRouter,
} from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listSamples: vi.fn(),
  listModels: vi.fn(),
  getSettings: vi.fn(),
  installSamples: vi.fn(),
}));

const toasts = vi.hoisted(() => ({
  success: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
}));

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>();
  return { ...actual, api: { ...actual.api, ...mocks } };
});

vi.mock("../src/lib/notify", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/notify")>();
  return { ...actual, notify: { ...actual.notify, ...toasts } };
});

import { SamplesTab } from "../src/components/settings/SamplesTab";

const SAMPLES = [
  {
    id: "hybrid-reasoner",
    title: "Hybrid reasoner",
    description: "Two models in one run.",
    capabilities: ["two models in one run", "router"],
    durable: false,
    input_example: "hello",
    agent_id: "agt_sample_hybrid_reasoner",
    agent_name: "sample-hybrid-reasoner",
    agent_names: ["sample-hybrid-reasoner"],
    installed: false,
    model_slots: {
      local: { label: "Local model", description: "Triage + easy answers.", modality: null },
      remote: { label: "API model", description: "Hard questions.", modality: null },
    },
    connections: [],
    rag_sources: [],
    trigger: null,
  },
  {
    id: "expense-approver",
    title: "Expense approver",
    description: "Human in the loop.",
    capabilities: ["human in the loop"],
    durable: true,
    input_example: {},
    agent_id: "agt_sample_expense_approver",
    agent_name: "sample-expense-approver",
    agent_names: ["sample-expense-approver"],
    installed: true,
    model_slots: {
      local: { label: "Local model", description: "Risk assessment.", modality: null },
    },
    connections: [],
    rag_sources: [],
    trigger: null,
  },
  {
    id: "batch-briefing",
    title: "Batch briefing",
    description: "Durable fan-out.",
    capabilities: ["parallel fan-out (map)"],
    durable: true,
    input_example: [],
    agent_id: "agt_sample_batch_briefing",
    agent_name: "sample-batch-briefing",
    agent_names: ["sample-briefing-worker", "sample-batch-briefing"],
    installed: false,
    model_slots: {
      local: { label: "Local model", description: "Writes each brief.", modality: null },
    },
    connections: [],
    rag_sources: [],
    trigger: { kind: "schedule", enabled: false },
  },
  {
    id: "visual-inspector",
    title: "Visual inspector",
    description: "Vision in a pipeline.",
    capabilities: ["vision in a pipeline"],
    durable: false,
    input_example: {},
    agent_id: "agt_sample_visual_inspector",
    agent_name: "sample-visual-inspector",
    agent_names: ["sample-visual-inspector"],
    installed: false,
    model_slots: {
      vision: { label: "Vision model", description: "Inspects images.", modality: "vision" },
    },
    connections: [],
    rag_sources: [],
    trigger: null,
  },
];

const MODELS = [
  { logicalId: "qwen-local", binding: { binding: "mlx", modality: "chat" } },
  { logicalId: "claude-api", binding: { binding: "openai-compatible", modality: "chat" } },
  { logicalId: "embed-small", binding: { binding: "llamacpp", modality: "embeddings" } },
  { logicalId: "glm-vision", binding: { binding: "mlx", modality: "vision" } },
];

async function hybridCard(): Promise<HTMLElement> {
  const title = await screen.findByText("Hybrid reasoner");
  const card = title.closest('[data-slot="item"]');
  if (!(card instanceof HTMLElement)) throw new Error("hybrid card not found");
  return card;
}

function renderTab(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rootRoute = createRootRoute({ component: () => <SamplesTab /> });
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
  mocks.listSamples.mockResolvedValue({ samples: SAMPLES });
  mocks.listModels.mockResolvedValue(MODELS);
  mocks.getSettings.mockResolvedValue({
    settings: [],
    boot: [{ key: "durable", value: false, description: "", warning: null }],
    orphaned: [],
  });
  mocks.installSamples.mockResolvedValue({
    report: [
      {
        id: "hybrid-reasoner",
        agent_id: "agt_sample_hybrid_reasoner",
        status: "installed",
        connections: [],
        rag_sources: [],
        warnings: [],
      },
    ],
  });
});

describe("SamplesTab", () => {
  it("renders the catalog with capability badges and the installed state", async () => {
    renderTab();
    expect(await screen.findByText("Hybrid reasoner")).toBeInTheDocument();
    expect(screen.getByText("two models in one run")).toBeInTheDocument();
    // The installed sample links to the editor instead of offering Install.
    expect(screen.getByText("Installed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in editor" })).toBeInTheDocument();
    // Durable samples + durable off in boot → the loud variant of the badge (one per sample).
    expect(screen.getAllByText(/THEYGENT_DURABLE=1/)).toHaveLength(2);
  });

  it("model pickers filter by slot: local excludes openai-compatible and non-chat", async () => {
    renderTab();
    const local = await screen.findByRole("combobox", { name: "Local model" });
    const localOptions = [...local.querySelectorAll("option")].map((o) => o.value);
    expect(localOptions).toContain("qwen-local");
    expect(localOptions).not.toContain("claude-api");
    expect(localOptions).not.toContain("embed-small");
    expect(localOptions).not.toContain("glm-vision");

    const remote = screen.getByRole("combobox", { name: "API model" });
    const remoteOptions = [...remote.querySelectorAll("option")].map((o) => o.value);
    expect(remoteOptions).toContain("claude-api");
    expect(remoteOptions).not.toContain("qwen-local");
  });

  it("a modality slot filters the registry by that modality, whatever the binding", async () => {
    renderTab();
    const vision = await screen.findByRole("combobox", { name: "Vision model" });
    const options = [...vision.querySelectorAll("option")].map((o) => o.value);
    expect(options).toContain("glm-vision");
    expect(options).not.toContain("qwen-local");
    expect(options).not.toContain("claude-api");
  });

  it("shows composition and trigger badges", async () => {
    renderTab();
    expect(await screen.findByText("installs 2 agents")).toBeInTheDocument();
    expect(screen.getByText("ships a disabled schedule trigger")).toBeInTheDocument();
  });

  it("gates Install until every declared slot is filled, then sends the chosen bindings", async () => {
    const user = userEvent.setup();
    renderTab();
    const button = within(await hybridCard()).getByRole("button", { name: "Install" });
    expect(button).toBeDisabled();

    await user.selectOptions(screen.getByRole("combobox", { name: "Local model" }), "qwen-local");
    expect(button).toBeDisabled(); // remote still unfilled for the two-slot sample

    await user.selectOptions(screen.getByRole("combobox", { name: "API model" }), "claude-api");
    expect(button).toBeEnabled();

    await user.click(button);
    await waitFor(() => expect(mocks.installSamples).toHaveBeenCalledTimes(1));
    expect(mocks.installSamples).toHaveBeenCalledWith({
      ids: ["hybrid-reasoner"],
      models: {
        local: { logical_id: "qwen-local", binding: "mlx" },
        remote: { logical_id: "claude-api", binding: "openai-compatible" },
      },
    });
    await waitFor(() =>
      expect(toasts.success).toHaveBeenCalledWith(
        "Installed sample-hybrid-reasoner",
        expect.anything(),
      ),
    );
  });

  it("surfaces a per-sample error report entry as an error toast", async () => {
    mocks.installSamples.mockResolvedValue({
      report: [
        {
          id: "hybrid-reasoner",
          agent_id: "agt_sample_hybrid_reasoner",
          status: "error",
          code: "sample_invalid",
          message: "sample 'hybrid-reasoner' did not validate: boom",
          connections: [],
        },
      ],
    });
    const user = userEvent.setup();
    renderTab();
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Local model" }),
      "qwen-local",
    );
    await user.selectOptions(screen.getByRole("combobox", { name: "API model" }), "claude-api");
    await user.click(within(await hybridCard()).getByRole("button", { name: "Install" }));
    await waitFor(() =>
      expect(toasts.error).toHaveBeenCalledWith(
        expect.stringContaining("did not validate"),
        expect.anything(),
      ),
    );
    expect(toasts.success).not.toHaveBeenCalled();
  });

  it("reports already_installed as a warning, not a success", async () => {
    mocks.installSamples.mockResolvedValue({
      report: [
        {
          id: "hybrid-reasoner",
          agent_id: "agt_sample_hybrid_reasoner",
          status: "already_installed",
          connections: [],
        },
      ],
    });
    const user = userEvent.setup();
    renderTab();
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Local model" }),
      "qwen-local",
    );
    await user.selectOptions(screen.getByRole("combobox", { name: "API model" }), "claude-api");
    await user.click(within(await hybridCard()).getByRole("button", { name: "Install" }));
    await waitFor(() =>
      expect(toasts.warning).toHaveBeenCalledWith(
        expect.stringContaining("already installed"),
        expect.anything(),
      ),
    );
    expect(toasts.success).not.toHaveBeenCalled();
  });
});
