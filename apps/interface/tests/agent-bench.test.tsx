// AgentBench version pinning (the "model change didn't apply" bug): the bench must default to the
// LATEST saved version, and follow it as fresh data arrives, while still honoring a deliberate pick.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/lib/api", () => ({
  api: {
    // empty runId → the trace/canvas block is skipped (keeps the test light).
    runAgent: vi.fn(async () => ({ runId: "", output: "done" })),
    getAgentVersion: vi.fn(async () => ({ ir: { id: "agent.docker", models: {} } })),
    listPresets: vi.fn(async () => []),
  },
}));

import { AgentBench } from "../src/bench/AgentBench";
import { type AgentDetail, api } from "../src/lib/api";

function withQuery(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

// versions seq-desc (newest first), the order the detail endpoint returns.
const agent: AgentDetail = {
  id: "agent.docker",
  name: "Test Agent",
  versions: [
    {
      version: "0.1.2",
      seq: 3,
      content_hash: "sha256:qwen35latest",
      created_at: "2026-06-29T11:41:19Z",
    },
    {
      version: "0.1.1",
      seq: 2,
      content_hash: "sha256:llama32older",
      created_at: "2026-06-29T10:55:12Z",
    },
  ],
} as unknown as AgentDetail;

describe("AgentBench version pin", () => {
  beforeEach(() => vi.clearAllMocks());

  it("defaults the pin to the latest version and runs it", async () => {
    withQuery(<AgentBench agent={agent} />);
    const pin = screen.getByRole("combobox") as HTMLSelectElement;
    expect(pin.value).toBe("0.1.2"); // latest, not the older llama-3.2 version

    fireEvent.change(screen.getByPlaceholderText("run input…"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() =>
      expect(api.runAgent).toHaveBeenCalledWith("agent.docker", { input: "hi", version: "0.1.2" }),
    );
  });

  it("honors a deliberately picked older version", async () => {
    withQuery(<AgentBench agent={agent} />);
    const pin = screen.getByRole("combobox") as HTMLSelectElement;
    fireEvent.change(pin, { target: { value: "0.1.1" } });
    expect(pin.value).toBe("0.1.1");

    fireEvent.change(screen.getByPlaceholderText("run input…"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() =>
      expect(api.runAgent).toHaveBeenLastCalledWith("agent.docker", {
        input: "hi",
        version: "0.1.1",
      }),
    );
  });
});
