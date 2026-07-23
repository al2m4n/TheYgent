// The editor's test console: runs the CURRENT canvas IR through the inline graph-run stream,
// mirrors the trace stream's node spans onto the canvas as live execution state, and gates the
// Run button on validation errors / durable-only graphs (both of which the server would reject).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { IRDocument } from "@theygent/ir-types";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status?: number;
    code?: string;
  },
  // The run stream: first frame carries the runId, then answer deltas, then the terminal frame.
  streamRun: vi.fn(async () => ({
    events: (async function* () {
      yield { event: "run", data: JSON.stringify({ runId: "run_t1", status: "streaming" }) };
      yield { event: "delta", data: JSON.stringify({ runId: "run_t1", delta: "hello " }) };
      yield { event: "delta", data: JSON.stringify({ runId: "run_t1", delta: "world" }) };
      yield { event: "run", data: JSON.stringify({ runId: "run_t1", status: "completed" }) };
      yield { event: "message", data: "[DONE]" };
    })(),
    abort: vi.fn(),
  })),
  // The trace stream: a node span opens then closes ok; a phase span must be IGNORED by the
  // canvas join (it carries its parent's node_id but is not a node).
  streamGet: vi.fn(async () => ({
    events: (async function* () {
      yield {
        event: "span.open",
        data: JSON.stringify({ id: "s1", node_id: "n_llm", phase: null, status: "running" }),
      };
      yield {
        event: "span.open",
        data: JSON.stringify({
          id: "s2",
          node_id: "n_llm",
          phase: "model.generate",
          status: "running",
        }),
      };
      yield {
        event: "span.close",
        data: JSON.stringify({ id: "s1", node_id: "n_llm", phase: null, status: "ok" }),
      };
      yield { event: "done", data: JSON.stringify({ runId: "run_t1" }) };
    })(),
    abort: vi.fn(),
  })),
  api: {
    getRun: vi.fn(async () => ({
      id: "run_t1",
      status: "completed",
      output: "hello world",
      error: null,
    })),
    getTrace: vi.fn(async () => []),
    uploadArtifact: vi.fn(async () => ({
      ref: "art_canvas1",
      contentType: "audio/webm",
      bytes: 3,
    })),
    downloadArtifact: vi.fn(async () => new Blob(["x"], { type: "audio/wav" })),
  },
}));

import { TestPanel } from "../src/components/TestPanel";
import { api, streamGet, streamRun } from "../src/lib/api";

function withQuery(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

function graph(nodeType = "llm"): IRDocument {
  return {
    schemaVersion: "1.0",
    id: "agent.t",
    name: "t",
    version: "0.1.0",
    models: {},
    tools: {},
    nodes: [{ id: "n_llm", type: nodeType, kind: "activity", config: {}, ports: {} }],
    edges: [],
  } as unknown as IRDocument;
}

/** A canvas graph whose boundaries declare the given modalities (absent = the IR's own default). */
function boundaryGraph(input?: string, output?: string): IRDocument {
  return {
    schemaVersion: "1.0",
    id: "agent.t",
    name: "t",
    version: "0.1.0",
    models: {},
    tools: {},
    nodes: [
      {
        id: "n_in",
        type: "input",
        kind: "boundary",
        config: input ? { modality: input } : {},
        ports: {},
      },
      { id: "n_llm", type: "llm", kind: "activity", config: {}, ports: {} },
      {
        id: "n_out",
        type: "output",
        kind: "boundary",
        config: output ? { modality: output } : {},
        ports: {},
      },
    ],
    edges: [],
  } as unknown as IRDocument;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("TestPanel", () => {
  it("streams a run of the current IR and mirrors node spans onto the canvas", async () => {
    const onRunState = vi.fn();
    withQuery(
      <TestPanel
        ir={graph()}
        errorCount={0}
        onClose={() => {}}
        onRunState={onRunState}
        onHoverNode={() => {}}
      />,
    );
    await userEvent.type(screen.getByLabelText("Run input"), "hi");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(screen.getByText(/hello world/)).toBeInTheDocument());
    expect(streamRun).toHaveBeenCalledWith("/graphs/runs", {
      ir: expect.objectContaining({ id: "agent.t" }),
      input: "hi",
      stream: true,
    });
    // The trace stream attached off the first frame's runId…
    expect(streamGet).toHaveBeenCalledWith("/runs/run_t1/trace/stream");
    // …and the canvas saw running, then ok — from the NODE span only (the phase span is skipped).
    const states = onRunState.mock.calls.map((c) => c[0]);
    expect(states).toContainEqual({ n_llm: "running" });
    expect(states[states.length - 1]).toEqual({ n_llm: "ok" });
  });

  it("gates the Run button on validation errors", () => {
    withQuery(
      <TestPanel
        ir={graph()}
        errorCount={2}
        onClose={() => {}}
        onRunState={() => {}}
        onHoverNode={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
  });

  it("gates durable-only graphs with an explanation", () => {
    withQuery(
      <TestPanel
        ir={graph("human")}
        errorCount={0}
        onClose={() => {}}
        onRunState={() => {}}
        onHoverNode={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    expect(screen.getByText(/durable-only nodes/)).toBeInTheDocument();
  });

  // The canvas is the one surface whose boundary is LIVE — the author retypes the input node's
  // modality and the control must follow, with no publish and no network round trip.
  it("offers the audio control for an audio input boundary, derived from the canvas IR alone", () => {
    withQuery(
      <TestPanel
        ir={boundaryGraph("audio")}
        errorCount={0}
        onClose={() => {}}
        onRunState={() => {}}
        onHoverNode={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Record from microphone" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Attach audio file" })).toBeInTheDocument();
    // No clip staged yet — a run would send a bare string to an audio boundary.
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    expect(screen.getByText(/this graph's input boundary is audio/i)).toBeInTheDocument();
    // Derived locally: the control appeared without any network call at all — no version fetch,
    // no run. The canvas document alone decided it.
    expect(streamRun).not.toHaveBeenCalled();
    expect(streamGet).not.toHaveBeenCalled();
    expect(api.getRun).not.toHaveBeenCalled();
    expect(api.getTrace).not.toHaveBeenCalled();
  });

  it("uploads a staged clip and sends {ref, contentType} to /graphs/runs", async () => {
    withQuery(
      <TestPanel
        ir={boundaryGraph("audio")}
        errorCount={0}
        onClose={() => {}}
        onRunState={() => {}}
        onHoverNode={() => {}}
      />,
    );
    const file = new File(["abc"], "clip.webm", { type: "audio/webm" });
    await userEvent.upload(screen.getByLabelText<HTMLInputElement>("Audio file"), file);
    await waitFor(() => expect(screen.getByRole("button", { name: "Run" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(api.uploadArtifact).toHaveBeenCalledWith(file));
    expect(streamRun).toHaveBeenCalledWith("/graphs/runs", {
      ir: expect.objectContaining({ id: "agent.t" }),
      input: { ref: "art_canvas1", contentType: "audio/webm" },
      stream: true,
    });
  });

  it("defaults an input node with no modality to the text control", () => {
    withQuery(
      <TestPanel
        ir={boundaryGraph()}
        errorCount={0}
        onClose={() => {}}
        onRunState={() => {}}
        onHoverNode={() => {}}
      />,
    );
    expect(screen.getByLabelText("Run input")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record from microphone" })).toBeNull();
    // An empty text box is a legitimate input — the boundary asks for nothing more.
    expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
  });

  it("offers exactly one JSON route for a json boundary", () => {
    withQuery(
      <TestPanel
        ir={boundaryGraph("json")}
        errorCount={0}
        onClose={() => {}}
        onRunState={() => {}}
        onHoverNode={() => {}}
      />,
    );
    const mode = screen.getByRole("combobox", { name: "Input mode" }) as HTMLSelectElement;
    expect([...mode.options].map((o) => o.value)).toEqual(["json"]);
  });

  it("opens in JSON for a json input boundary and refuses a malformed payload", async () => {
    withQuery(
      <TestPanel
        ir={boundaryGraph("json")}
        errorCount={0}
        onClose={() => {}}
        onRunState={() => {}}
        onHoverNode={() => {}}
      />,
    );
    const box = screen.getByLabelText("Run input");
    await userEvent.type(box, "{{not json");
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    await userEvent.clear(box);
    await userEvent.type(box, '{{"code": "DE"}');
    await waitFor(() => expect(screen.getByRole("button", { name: "Run" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() =>
      expect(streamRun).toHaveBeenCalledWith("/graphs/runs", {
        ir: expect.objectContaining({ id: "agent.t" }),
        input: { code: "DE" },
        stream: true,
      }),
    );
  });

  it("plays an audio answer instead of printing the artifact reference", async () => {
    vi.mocked(api.getRun).mockResolvedValueOnce({
      id: "run_t1",
      status: "completed",
      output: JSON.stringify({ ref: "art_reply", contentType: "audio/wav" }),
      error: null,
    } as never);
    const { container } = withQuery(
      <TestPanel
        ir={boundaryGraph("text", "audio")}
        errorCount={0}
        onClose={() => {}}
        onRunState={() => {}}
        onHoverNode={() => {}}
      />,
    );
    await userEvent.type(screen.getByLabelText("Run input"), "hi");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(api.downloadArtifact).toHaveBeenCalledWith("art_reply"));
    await waitFor(() => expect(container.querySelector("audio")).not.toBeNull());
  });
});
