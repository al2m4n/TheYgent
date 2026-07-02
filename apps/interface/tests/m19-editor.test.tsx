// M19 §2.10 editor work: handle roles + data/control connect rules, the http-tool connection
// binding (id, never the secret), and the graph-level connections panel (secret write-only).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { IRDocument } from "@theygent/ir-types";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { connect, setHttpToolBinding } from "../src/adapter";

// ── pure adapter: handle roles + connect rules (§2.10) ───────────────────────────────────────────

function rolesIr(): IRDocument {
  return {
    schemaVersion: "1.0",
    id: "agt_roles",
    name: "roles",
    version: "0.1.0",
    models: {},
    tools: {},
    nodes: [
      {
        id: "n1",
        type: "guardrail",
        kind: "orchestration",
        config: { check: { type: "rule", rule: { kind: "length", spec: {} } } },
        ports: {
          in: [{ id: "in", type: "any", required: true }],
          out: [
            { id: "pass", type: "any", required: true, role: "data" },
            { id: "ctl", type: "any", required: true, role: "control" },
          ],
        },
      },
      {
        id: "n2",
        type: "output",
        kind: "boundary",
        config: {},
        ports: {
          in: [
            { id: "in", type: "any", required: true, role: "data" },
            { id: "after", type: "any", required: true, role: "control" },
          ],
          out: [],
        },
      },
    ],
    edges: [],
  } as IRDocument;
}

describe("connect: handle roles drive the channel (§2.10)", () => {
  it("data→data connects, channel derived as data", () => {
    const r = connect(rolesIr(), {
      source: "n1",
      sourceHandle: "pass",
      target: "n2",
      targetHandle: "in",
    });
    expect(r.error).toBeUndefined();
    expect(r.ir?.edges?.[0].channel).toBe("data");
  });

  it("control→control connects, channel derived as control", () => {
    const r = connect(rolesIr(), {
      source: "n1",
      sourceHandle: "ctl",
      target: "n2",
      targetHandle: "after",
    });
    expect(r.error).toBeUndefined();
    expect(r.ir?.edges?.[0].channel).toBe("control");
  });

  it("rejects a cross-role connection (data→control)", () => {
    const r = connect(rolesIr(), {
      source: "n1",
      sourceHandle: "pass",
      target: "n2",
      targetHandle: "after",
    });
    expect(r.ir).toBeUndefined();
    expect(r.error).toMatch(/data handle to a control handle|control to control/);
  });

  it("rejects a cross-role connection (control→data)", () => {
    const r = connect(rolesIr(), {
      source: "n1",
      sourceHandle: "ctl",
      target: "n2",
      targetHandle: "in",
    });
    expect(r.ir).toBeUndefined();
    expect(r.error).toMatch(/control handle to a data handle|data to data/);
  });
});

describe("setHttpToolBinding writes the connection id, never a secret (§1.1)", () => {
  it("declares ir.tools[key] = {kind:http, connection} + sets config.tool", () => {
    const ir = {
      schemaVersion: "1.0",
      id: "a",
      name: "n",
      version: "0.1.0",
      models: {},
      tools: {},
      nodes: [
        {
          id: "n_tool",
          type: "tool",
          kind: "activity",
          config: { tool: "" },
          ports: {
            in: [{ id: "in", type: "any", required: true }],
            out: [
              { id: "ok", type: "any", required: true },
              { id: "err", type: "error", required: true },
            ],
          },
        },
      ],
      edges: [],
    } as IRDocument;
    const next = setHttpToolBinding(ir, "n_tool", "weather", "con_abc");
    expect((next.tools as Record<string, unknown>).weather).toEqual({
      kind: "http",
      connection: "con_abc",
    });
    const node = next.nodes?.find((n) => n.id === "n_tool");
    expect((node?.config as { tool?: string }).tool).toBe("weather");
    // The whole serialized IR carries the connection id but no secret material.
    expect(JSON.stringify(next)).toContain("con_abc");
    expect(JSON.stringify(next)).not.toMatch(/secret|api[_-]?key|password/i);
  });
});

// ── RTL: the connections panel — secret is write-only (§1.1) ─────────────────────────────────────

vi.mock("../src/lib/api", () => ({
  api: {
    listConnections: vi.fn(),
    createConnection: vi.fn(),
    listTriggers: vi.fn().mockResolvedValue([]),
    listModels: vi.fn().mockResolvedValue([]),
    listMcpServers: vi.fn().mockResolvedValue([]),
    listAgents: vi.fn().mockResolvedValue([]),
  },
}));

import { Inspector } from "../src/components/Inspector";
import { api } from "../src/lib/api";

function renderWithClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const emptyIr = {
  schemaVersion: "1.0",
  id: "a",
  name: "n",
  version: "0.1.0",
  models: {},
  tools: {},
  nodes: [],
  edges: [],
} as IRDocument;

describe("ConnectionsPanel: secret is write-only", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.listConnections as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    (api.createConnection as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: "con_1",
      name: "CRM",
      kind: "http_auth",
      config: {},
      hasSecret: true,
      enabled: true,
      created_at: "",
      updated_at: "",
    });
  });

  it("sends the secret to createConnection but never renders it back", async () => {
    // No selection → the graph panel (which hosts the connections panel) renders.
    renderWithClient(
      <Inspector ir={emptyIr} selection={null} onChange={() => {}} onSelect={() => {}} />,
    );
    fireEvent.click(screen.getByText("New connection"));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "CRM" } });
    fireEvent.change(screen.getByLabelText(/Secret/), { target: { value: "super-secret-xyz" } });
    fireEvent.click(screen.getByText("Create connection"));

    await waitFor(() =>
      expect(api.createConnection).toHaveBeenCalledWith(
        expect.objectContaining({ name: "CRM", kind: "http_auth", secret: "super-secret-xyz" }),
      ),
    );
    // After a successful create the secret field is cleared — the value is never shown again.
    await waitFor(() => expect(screen.queryByDisplayValue("super-secret-xyz")).toBeNull());
  });
});
