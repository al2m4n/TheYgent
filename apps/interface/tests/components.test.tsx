// Light React Testing Library coverage (§4 toolchain). The canvas itself needs a real React Flow
// host, so we cover the two pure-DOM surfaces: the palette (derived from the registry) and the
// inspector (schema-driven config editing → valid IR mutations).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { NODE_TYPE_LIST } from "@theygent/ir-types";
import type { IRDocument } from "@theygent/ir-types";
import type { ReactNode } from "react";
import { Inspector } from "../src/components/Inspector";
import { Palette } from "../src/components/Palette";
import { sampleGraph } from "./fixtures";

// The inspector's reference pickers use TanStack Query, so its renders need a client (the network
// calls are never made — retry off, and the components fall back to free text when data is absent).
function renderWithClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("Palette (derived from the registry, never hardcoded)", () => {
  it("renders one draggable item per registry node type", () => {
    render(<Palette />);
    for (const spec of NODE_TYPE_LIST) {
      expect(screen.getByText(spec.type)).toBeInTheDocument();
    }
  });
});

describe("Inspector (schema-driven config editing + delete + edges)", () => {
  it("edits a string config field and emits a valid IR mutation", () => {
    const ir = sampleGraph();
    let next: IRDocument | null = null;
    renderWithClient(
      <Inspector
        ir={ir}
        selection={{ kind: "node", id: "n_llm" }}
        onChange={(x) => {
          next = x;
        }}
        onSelect={() => {}}
      />,
    );

    const modelInput = screen.getByDisplayValue("default");
    fireEvent.change(modelInput, { target: { value: "triage-fast" } });

    expect(next).not.toBeNull();
    const llm = (next as unknown as IRDocument).nodes?.find((n) => n.id === "n_llm");
    expect((llm?.config as Record<string, unknown>).model).toBe("triage-fast");
    // structure is untouched — still three nodes, two edges.
    expect((next as unknown as IRDocument).nodes).toHaveLength(3);
  });

  it("deletes the selected node and clears the selection", () => {
    const ir = sampleGraph();
    let next: IRDocument | null = null;
    let cleared = false;
    renderWithClient(
      <Inspector
        ir={ir}
        selection={{ kind: "node", id: "n_llm" }}
        onChange={(x) => {
          next = x;
        }}
        onSelect={(s) => {
          cleared = s === null;
        }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect((next as unknown as IRDocument).nodes?.some((n) => n.id === "n_llm")).toBe(false);
    expect(cleared).toBe(true);
  });

  it("edits a selected edge's channel (data ↔ control)", () => {
    const ir = sampleGraph();
    let next: IRDocument | null = null;
    renderWithClient(
      <Inspector
        ir={ir}
        selection={{ kind: "edge", id: "e1" }}
        onChange={(x) => {
          next = x;
        }}
        onSelect={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "control" }));
    expect((next as unknown as IRDocument).edges?.find((e) => e.id === "e1")?.channel).toBe(
      "control",
    );
  });

  it("shows graph-level model bindings read-only when nothing is selected", () => {
    renderWithClient(
      <Inspector ir={sampleGraph()} selection={null} onChange={() => {}} onSelect={() => {}} />,
    );
    expect(screen.getByText("Model bindings")).toBeInTheDocument();
    expect(screen.getByText("default")).toBeInTheDocument();
  });
});
