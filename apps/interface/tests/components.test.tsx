// Light React Testing Library coverage (§4 toolchain). The canvas itself needs a real React Flow
// host, so we cover the two pure-DOM surfaces: the palette (derived from the registry) and the
// inspector (schema-driven config editing → valid IR mutations).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { NODE_TYPE_LIST } from "@theygent/ir-types";
import type { IRDocument } from "@theygent/ir-types";
import { type ReactNode, useState } from "react";
import { IRCodeEditor } from "../src/components/IRCodeEditor";
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

  it("filters to a single category when its chip is clicked", () => {
    const boundary = NODE_TYPE_LIST.find((s) => s.kind === "boundary");
    const activity = NODE_TYPE_LIST.find((s) => s.kind === "activity");
    if (!boundary || !activity) throw new Error("fixture needs a boundary + activity type");
    render(<Palette />);
    // "I/O & Human" is the boundary category chip — clicking it hides activity-kind nodes. Use an
    // EXACT name so we hit the chip, not the same-labelled (collapsible) category group header
    // (whose accessible name also carries its item count, e.g. "I/O & Human 4").
    fireEvent.click(screen.getByRole("button", { name: "I/O & Human" }));
    expect(screen.getByText(boundary.type)).toBeInTheDocument();
    expect(screen.queryByText(activity.type)).not.toBeInTheDocument();
  });

  it("collapses a category group when its header is clicked (expanded by default)", () => {
    const boundary = NODE_TYPE_LIST.find((s) => s.kind === "boundary");
    if (!boundary) throw new Error("fixture needs a boundary type");
    render(<Palette />);
    // Expanded by default: the group's items are visible without any interaction.
    expect(screen.getByText(boundary.type)).toBeInTheDocument();
    // The category header is collapsible — clicking the first group's header (boundary, per the kind
    // order) hides that group's items. (Headers expose "Collapse category" via title while open.)
    fireEvent.click(screen.getAllByTitle("Collapse category")[0]);
    expect(screen.queryByText(boundary.type)).not.toBeInTheDocument();
  });

  it("filters by the search box and shows an empty state when nothing matches", () => {
    const some = NODE_TYPE_LIST[0];
    render(<Palette />);
    const search = screen.getByPlaceholderText("Search nodes…");
    fireEvent.change(search, { target: { value: some.type } });
    expect(screen.getByText(some.type)).toBeInTheDocument();
    fireEvent.change(search, { target: { value: "zzz-no-such-node" } });
    expect(screen.getByText(/No nodes match/)).toBeInTheDocument();
  });
});

describe("IRCodeEditor (the Code view — one IR, two editors)", () => {
  it("shows the IR as JSON and commits a valid edit straight to the IR", () => {
    const ir = sampleGraph();
    let next: IRDocument | null = null;
    render(
      <IRCodeEditor
        ir={ir}
        onChange={(x) => {
          next = x;
        }}
      />,
    );
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.value).toContain(`"id": "${ir.id}"`);
    const edited = JSON.stringify({ ...ir, name: "Renamed via code" }, null, 2);
    fireEvent.change(textarea, { target: { value: edited } });
    expect(next).not.toBeNull();
    expect((next as unknown as IRDocument).name).toBe("Renamed via code");
  });

  it("holds invalid JSON locally — never commits, reports invalid", () => {
    let next: IRDocument | null = null;
    let valid = true;
    render(
      <IRCodeEditor
        ir={sampleGraph()}
        onChange={(x) => {
          next = x;
        }}
        onValidityChange={(v) => {
          valid = v;
        }}
      />,
    );
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "{ not valid json" } });
    expect(valid).toBe(false);
    expect(next).toBeNull();
    expect(screen.getByText(/invalid JSON/)).toBeInTheDocument();
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

    // The model field is a searchable dropdown: open it (the trigger shows the current value),
    // type a value, then pick the "Use …" custom-value row (the registry is empty in this test).
    fireEvent.click(screen.getByRole("button", { name: "default" }));
    fireEvent.change(screen.getByPlaceholderText("Search…"), { target: { value: "triage-fast" } });
    fireEvent.click(screen.getByRole("button", { name: /Use/ }));

    expect(next).not.toBeNull();
    const llm = (next as unknown as IRDocument).nodes?.find((n) => n.id === "n_llm");
    expect((llm?.config as Record<string, unknown>).model).toBe("triage-fast");
    // structure is untouched — still three nodes, two edges.
    expect((next as unknown as IRDocument).nodes).toHaveLength(3);
  });

  it("edits llm messages with the structured chat editor (insert $in + add a turn)", () => {
    const ir = sampleGraph(); // n_llm starts with one user turn whose content is "$in"
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

    const msgsOf = (d: IRDocument | null) =>
      (
        (d as unknown as IRDocument)?.nodes?.find((n) => n.id === "n_llm")?.config as Record<
          string,
          unknown
        >
      )?.messages as { role: string; content: string }[];

    // The turn renders as a structured row, not raw JSON: a role dropdown + a text box (value "$in").
    expect(screen.getByDisplayValue("$in")).toBeInTheDocument();
    expect(screen.getByRole("combobox")).toHaveValue("user");

    // The "insert input ($in)" helper splices the token into the text box → content grows by 3.
    fireEvent.click(screen.getByRole("button", { name: /Insert input/ }));
    expect(msgsOf(next)[0].content).toHaveLength(6);

    // "+ Add message" appends a second user turn (default role).
    fireEvent.click(screen.getByRole("button", { name: /Add message/ }));
    expect(msgsOf(next)).toHaveLength(2);
    expect(msgsOf(next)[1]).toEqual({ role: "user", content: "" });
  });

  it("messages editor: a system turn auto-sorts to the top", () => {
    const ir = sampleGraph(); // n_llm starts with one user turn ("$in")
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
    const msgsOf = (d: IRDocument | null) =>
      (
        (d as unknown as IRDocument)?.nodes?.find((n) => n.id === "n_llm")?.config as Record<
          string,
          unknown
        >
      )?.messages as { role: string; content: string }[];

    // Add a turn (appends a user turn at index 1), then flip its role to "system".
    fireEvent.click(screen.getByRole("button", { name: /Add message/ }));
    const roleSelects = screen.getAllByRole("combobox");
    fireEvent.change(roleSelects[1], { target: { value: "system" } });

    // The system turn floats to the top; the original user turn follows.
    expect(msgsOf(next).map((m) => m.role)).toEqual(["system", "user"]);
    expect(msgsOf(next)[1].content).toBe("$in");
  });

  it("messages editor: a JSON view round-trips with the wizard", () => {
    // A controlled harness (ir state fed back via onChange) so a JSON edit re-seeds the wizard, as
    // it does in the real editor.
    const irRef: { current: IRDocument | null } = { current: null };
    function Harness() {
      const [ir, setIr] = useState<IRDocument>(sampleGraph());
      irRef.current = ir;
      return (
        <Inspector
          ir={ir}
          selection={{ kind: "node", id: "n_llm" }}
          onChange={setIr}
          onSelect={() => {}}
        />
      );
    }
    renderWithClient(<Harness />);
    const msgs = () =>
      (irRef.current?.nodes?.find((n) => n.id === "n_llm")?.config as Record<string, unknown>)
        .messages as { role: string; content: string }[];

    // Switch to the JSON view → the messages array is shown as editable JSON.
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    const json = screen.getByDisplayValue(/"role": "user"/);

    // Editing the JSON commits straight to the IR.
    fireEvent.change(json, {
      target: { value: JSON.stringify([{ role: "system", content: "hi" }], null, 2) },
    });
    expect(msgs()).toEqual([{ role: "system", content: "hi" }]);

    // Back to the Wizard → it reflects the JSON edit (a single system turn).
    fireEvent.click(screen.getByRole("button", { name: "Wizard" }));
    expect(screen.getByDisplayValue("hi")).toBeInTheDocument();
    expect(screen.getByRole("combobox")).toHaveValue("system");
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
