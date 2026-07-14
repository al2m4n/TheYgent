// The bulk-add MCP tools dialog: pick a server, tick tools (or Select all), and the batch lands as
// configured mcp_tool nodes wired into the target llm — the one-gesture path that replaces the
// per-tool create → configure → drag cycle. Already-wired tools are shown but not re-addable.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { IRDocument } from "@theygent/ir-types";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/lib/api", () => ({
  api: {
    listMcpServers: vi.fn(),
    listConnections: vi.fn(),
    getMcpTools: vi.fn(),
    getConnectionMcpTools: vi.fn(),
  },
}));

import { AddMcpToolsDialog } from "../src/components/AddMcpToolsDialog";
import { api } from "../src/lib/api";

function renderWithClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

/** A minimal agent whose llm declares the tool-role `tools` in-port the batch wires into. */
function llmIr(extra?: { nodes?: unknown[]; edges?: unknown[] }): IRDocument {
  return {
    schemaVersion: "1.0",
    id: "a",
    name: "a",
    version: "0.1.0",
    models: {},
    tools: {},
    nodes: [
      {
        id: "n_llm",
        type: "llm",
        kind: "activity",
        label: null,
        config: { model: "default", messages: [] },
        ports: {
          in: [
            { id: "in", type: "any", required: true },
            { id: "tools", type: "any", required: false, role: "tool" },
          ],
          out: [{ id: "out", type: "any", required: true }],
        },
      },
      ...(extra?.nodes ?? []),
    ],
    edges: [...(extra?.edges ?? [])],
  } as IRDocument;
}

describe("AddMcpToolsDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.listMcpServers as ReturnType<typeof vi.fn>).mockResolvedValue([
      { name: "files", transport: "stdio", connected: true },
    ]);
    (api.listConnections as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    (api.getMcpTools as ReturnType<typeof vi.fn>).mockResolvedValue([
      { name: "read_file", description: "Read a file" },
      { name: "write_file", description: "Write a file" },
    ]);
  });

  it("Select all → Add lands every tool as a wired node in ONE onChange", async () => {
    const onChange = vi.fn();
    const onClose = vi.fn();
    renderWithClient(
      <AddMcpToolsDialog
        ir={llmIr()}
        attachTo="n_llm"
        initialServer="files"
        onChange={onChange}
        onClose={onClose}
      />,
    );
    await screen.findByText("Read a file"); // tool list fetched live from the server

    await userEvent.click(screen.getByRole("checkbox", { name: "Select all" }));
    await userEvent.click(screen.getByRole("button", { name: /Add 2 tools/ }));

    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as IRDocument;
    // one configured node per tool, id = the tool name (the function name the model calls) …
    for (const id of ["read_file", "write_file"]) {
      const node = next.nodes?.find((n) => n.id === id);
      expect(node?.type).toBe("mcp_tool");
      expect(node?.config).toMatchObject({ tool: id, server: "files" });
      // … wired into the llm's tools port on the tool channel.
      expect(
        next.edges?.some((e) => e.channel === "tool" && e.source === id && e.target === "n_llm"),
      ).toBe(true);
    }
    expect(onClose).toHaveBeenCalled();
  });

  it("an already-wired tool is disabled ('wired') and excluded from Select all", async () => {
    const ir = llmIr({
      nodes: [
        {
          id: "read_file",
          type: "mcp_tool",
          kind: "activity",
          label: "read_file",
          config: { tool: "read_file", server: "files", connection: null, args: {} },
          ports: {
            in: [{ id: "in", type: "any", required: true }],
            out: [
              { id: "out", type: "any", required: true },
              { id: "err", type: "error", required: true },
              { id: "use", type: "any", required: true, role: "tool" },
            ],
          },
        },
      ],
      edges: [
        {
          id: "e_t",
          source: "read_file",
          sourceHandle: "use",
          target: "n_llm",
          targetHandle: "tools",
          channel: "tool",
          condition: null,
        },
      ],
    });
    renderWithClient(
      <AddMcpToolsDialog
        ir={ir}
        attachTo="n_llm"
        initialServer="files"
        onChange={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    await screen.findByText("Read a file");

    expect(screen.getByRole("checkbox", { name: "read_file" })).toBeDisabled();
    expect(screen.getByText("wired")).toBeInTheDocument();
    // only write_file is selectable, so the count reflects that.
    expect(screen.getByText(/Select all \(1\)/)).toBeInTheDocument();
  });

  it("an unreachable server degrades to a plain message, never a crash", async () => {
    (api.getMcpTools as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("spawn failed"));
    renderWithClient(
      <AddMcpToolsDialog
        ir={llmIr()}
        attachTo="n_llm"
        initialServer="files"
        onChange={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(/Couldn't list this server's tools/)).toBeInTheDocument(),
    );
  });
});
