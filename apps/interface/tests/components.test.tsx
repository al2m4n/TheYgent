// Light React Testing Library coverage (Vitest + RTL toolchain). The canvas itself needs a real React Flow
// host, so we cover the two pure-DOM surfaces: the palette (derived from the registry) and the
// inspector (schema-driven config editing → valid IR mutations).

import type { CompletionContext } from "@codemirror/autocomplete";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { NODE_TYPE_LIST } from "@theygent/ir-types";
import type { IRDocument } from "@theygent/ir-types";
import { EditorView } from "@uiw/react-codemirror";
import { type ReactNode, useState } from "react";
import { IRCodeEditor, irCompletions } from "../src/components/IRCodeEditor";
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
  it("renders one draggable item per registry node type (minus the hidden ones)", () => {
    render(<Palette />);
    // `mcp_tool` is the MCP *kind* of the one "Tool" node, not its own palette entry — so the
    // palette shows every registry type EXCEPT the hidden ones.
    const hidden = new Set(["mcp_tool"]);
    for (const spec of NODE_TYPE_LIST) {
      if (hidden.has(spec.type)) {
        expect(screen.queryByText(spec.type)).not.toBeInTheDocument();
      } else {
        expect(screen.getByText(spec.type)).toBeInTheDocument();
      }
    }
    // the unified Tool entry is present (its MCP kind covers mcp_tool).
    expect(screen.getByText("tool")).toBeInTheDocument();
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

  it("click-to-add: clicking an item calls onAdd with its node type", () => {
    const added: string[] = [];
    render(<Palette onAdd={(t) => added.push(t)} />);
    fireEvent.click(screen.getByText("llm"));
    expect(added).toEqual(["llm"]);
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
  // CodeMirror is a contenteditable, not a <textarea> — drive it through the real EditorView so the
  // component's parse → commit pipeline runs exactly as it does for a user keystroke.
  const editorView = (container: HTMLElement): EditorView => {
    const content = container.querySelector(".cm-content");
    if (!content) throw new Error("CodeMirror content not mounted");
    const view = EditorView.findFromDOM(content as HTMLElement);
    if (!view) throw new Error("EditorView not found");
    return view;
  };
  const setDoc = (view: EditorView, text: string) =>
    act(() => {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
    });

  it("shows the IR as JSON and commits a valid edit straight to the IR", () => {
    const ir = sampleGraph();
    let next: IRDocument | null = null;
    const { container } = render(
      <IRCodeEditor
        ir={ir}
        onChange={(x) => {
          next = x;
        }}
      />,
    );
    const view = editorView(container);
    expect(view.state.doc.toString()).toContain(`"id": "${ir.id}"`);
    setDoc(view, JSON.stringify({ ...ir, name: "Renamed via code" }, null, 2));
    expect(next).not.toBeNull();
    expect((next as unknown as IRDocument).name).toBe("Renamed via code");
  });

  it("holds invalid JSON locally — never commits, reports invalid", () => {
    let next: IRDocument | null = null;
    let valid = true;
    const { container } = render(
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
    setDoc(editorView(container), "{ not valid json");
    expect(valid).toBe(false);
    expect(next).toBeNull();
    expect(screen.getByText(/invalid JSON/)).toBeInTheDocument();
  });
});

describe("irCompletions (schema-aware autocomplete source)", () => {
  // A minimal CompletionContext: a single-line doc + matchBefore anchored to the cursor.
  const ctx = (line: string, pos = line.length): CompletionContext =>
    ({
      pos,
      state: { doc: { lineAt: () => ({ text: line, from: 0 }) } },
      matchBefore: (re: RegExp) => {
        const before = line.slice(0, pos);
        const m = before.match(new RegExp(`(?:${re.source})$`));
        return m ? { from: pos - m[0].length, to: pos, text: m[0] } : null;
      },
    }) as unknown as CompletionContext;
  const labels = (line: string) => irCompletions(ctx(line))?.options.map((o) => o.label) ?? [];

  it("suggests registry node types in a `type` value context", () => {
    expect(labels('      "type": "')).toEqual(expect.arrayContaining(["llm", "input", "output"]));
  });
  it("suggests the kind enum in a `kind` value context", () => {
    expect(labels('      "kind": "')).toEqual(
      expect.arrayContaining(["boundary", "activity", "orchestration"]),
    );
  });
  it("suggests channels in a `channel` value context", () => {
    expect(labels('      "channel": "')).toEqual(
      expect.arrayContaining(["data", "control", "tool"]),
    );
  });
  it("suggests property keys when typing a key", () => {
    expect(labels('  { "')).toEqual(expect.arrayContaining(["nodes", "edges", "config"]));
  });
  it("returns null outside a known context", () => {
    expect(irCompletions(ctx('      "id": "agent.x"'))).toBeNull();
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
    // (Queried by name — the llm panel also hosts the binding-params selects.)
    expect(screen.getByDisplayValue("$in")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Message role" })).toHaveValue("user");

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
    const roleSelects = screen.getAllByRole("combobox", { name: "Message role" });
    fireEvent.change(roleSelects[1], { target: { value: "system" } });

    // The system turn floats to the top; the original user turn follows.
    expect(msgsOf(next).map((m) => m.role)).toEqual(["system", "user"]);
    expect(msgsOf(next)[1].content).toBe("$in");
  });

  it("node Code view: shows this node's JSON and round-trips with the wizard", () => {
    // A controlled harness (ir fed back via onChange) so a Code edit re-seeds the wizard, as it does
    // in the real editor. CodeMirror is a contenteditable — drive it through the real EditorView so
    // the component's parse → commit pipeline runs exactly as for a user keystroke.
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
    const { container } = renderWithClient(<Harness />);
    const nodeOf = () => irRef.current?.nodes?.find((n) => n.id === "n_llm");

    // The per-node view switch defaults to Wizard; there is no per-field messages "JSON" toggle.
    expect(screen.getByRole("button", { name: "Wizard" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("button", { name: "JSON" })).not.toBeInTheDocument();

    // Switch to Code → this exact node is shown as JSON in the CodeMirror editor.
    fireEvent.click(screen.getByRole("button", { name: "Code" }));
    const content = container.querySelector(".cm-content");
    if (!content) throw new Error("node code editor not mounted");
    const view = EditorView.findFromDOM(content as HTMLElement);
    if (!view) throw new Error("EditorView not found");
    expect(view.state.doc.toString()).toContain('"id": "n_llm"');

    // Editing the node JSON commits straight to the graph.
    act(() => {
      view.dispatch({
        changes: {
          from: 0,
          to: view.state.doc.length,
          insert: JSON.stringify({ ...nodeOf(), label: "Renamed via code" }, null, 2),
        },
      });
    });
    expect(nodeOf()?.label).toBe("Renamed via code");

    // Back to the Wizard → the label field reflects the Code edit.
    fireEvent.click(screen.getByRole("button", { name: "Wizard" }));
    expect(screen.getByDisplayValue("Renamed via code")).toBeInTheDocument();
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

  it("shows the graph's derived tool registry (read-only) when nothing is selected", () => {
    // ir.tools is the derived global registry (keyed by node id, like ir.models). The graph
    // panel lists it read-only; tools are added by wiring tool nodes on the canvas, not here.
    const ir = sampleGraph();
    ir.tools = { n_echo: { kind: "builtin", ref: "echo" } };
    renderWithClient(
      <Inspector ir={ir} selection={null} onChange={() => {}} onSelect={() => {}} />,
    );
    expect(screen.getByText("n_echo")).toBeInTheDocument();
    expect(screen.getByText("builtin")).toBeInTheDocument();
  });
});
