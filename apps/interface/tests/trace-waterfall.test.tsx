// TraceWaterfall — the rich bench waterfall. Asserts the behaviours the old flat list couldn't do
// AND the load-bearing contracts behind them: (1) it renders the run → node → phase TREE (phase rows
// like tool.http_fetch are visible, not filtered out), (2) collapse/re-expand a node's phases,
// (3) clicking a node opens the I/O drawer with the per-step context from /nodes/{id}/io — including
// the gated/capture states, (4) the hover → onHover canvas-flash JOIN fires for NODE rows only (the
// node_id, never the span_id, never a phase row), (5) zoom + the MIN_ZOOM clamp, (6) an in-flight
// (end_ns null) span renders a bounded, pulsing bar.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TraceWaterfall } from "../src/bench/TraceWaterfall";
import { api } from "../src/lib/api";

vi.mock("../src/lib/api", () => ({
  api: {
    getRunTrace: vi.fn(),
    getRunIo: vi.fn(),
  },
}));

function withQuery(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

// A run-root → n_llm → {model.generate, tool.http_fetch} + n_out span tree, snake_case like /trace.
const SPANS = [
  {
    id: "r:_root",
    span_id: "root",
    parent_span_id: null,
    name: "run-1",
    status: "ok",
    start_ns: 0,
    end_ns: 600,
  },
  {
    id: "r:n_llm",
    span_id: "llm",
    parent_span_id: "root",
    node_id: "n_llm",
    node_type: "llm",
    name: "n_llm",
    status: "ok",
    start_ns: 10,
    end_ns: 540,
    attributes: { "gen_ai.request.model": "qwen-gguf" },
  },
  {
    id: "r:n_llm:model.generate",
    span_id: "gen",
    parent_span_id: "llm",
    node_id: "n_llm",
    name: "model.generate",
    phase: "model.generate",
    status: "ok",
    start_ns: 12,
    end_ns: 538,
  },
  {
    id: "r:n_llm:tool.http_fetch",
    span_id: "tool",
    parent_span_id: "llm",
    node_id: "n_llm",
    name: "tool.http_fetch",
    phase: "tool.http_fetch",
    status: "ok",
    start_ns: 270,
    end_ns: 314,
    attributes: { "tool.name": "http_fetch", "tool.ok": true },
  },
  {
    id: "r:n_out",
    span_id: "out",
    parent_span_id: "root",
    node_id: "n_out",
    node_type: "output",
    name: "n_out",
    status: "ok",
    start_ns: 540,
    end_ns: 541,
  },
];

const FULL_IO = {
  run_id: "r",
  node_id: "n_llm",
  capture_level: "full",
  inputs: { in: "fetch example.com" },
  outputs: { ok: "the example domain answer" },
  bytes_in: 16,
  bytes_out: 25,
  truncated: false,
  reason: null,
};

const getRunTrace = api.getRunTrace as ReturnType<typeof vi.fn>;
const getRunIo = api.getRunIo as ReturnType<typeof vi.fn>;

describe("TraceWaterfall", () => {
  beforeEach(() => {
    getRunTrace.mockResolvedValue({ runId: "r", status: "completed", spans: SPANS });
    getRunIo.mockResolvedValue(FULL_IO);
  });
  afterEach(() => vi.clearAllMocks());

  it("renders the run → node → phase tree, phases included", async () => {
    render(withQuery(<TraceWaterfall runId="r" />));
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());
    // The phase children the OLD flat list filtered out are now visible.
    expect(screen.getByText("model.generate")).toBeInTheDocument();
    expect(screen.getByText("tool.http_fetch")).toBeInTheDocument();
    expect(screen.getByText("n_out")).toBeInTheDocument();
  });

  it("collapses then re-expands a node's phase children", async () => {
    render(withQuery(<TraceWaterfall runId="r" />));
    await waitFor(() => expect(screen.getByText("model.generate")).toBeInTheDocument());
    // n_llm has children → it shows a collapse control (the root also has one, so target by name).
    fireEvent.click(screen.getByRole("button", { name: "Collapse n_llm" }));
    expect(screen.queryByText("model.generate")).not.toBeInTheDocument();
    expect(screen.queryByText("tool.http_fetch")).not.toBeInTheDocument();
    // The node row itself stays.
    expect(screen.getByText("n_llm")).toBeInTheDocument();
    // Re-expand (exercises the toggle's delete branch + the caret/aria flip).
    fireEvent.click(screen.getByRole("button", { name: "Expand n_llm" }));
    expect(screen.getByText("model.generate")).toBeInTheDocument();
    expect(screen.getByText("tool.http_fetch")).toBeInTheDocument();
  });

  it("opens the I/O drawer with the per-step context when a node is clicked", async () => {
    render(withQuery(<TraceWaterfall runId="r" />));
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());
    fireEvent.click(screen.getByText("n_llm"));
    await waitFor(() => expect(getRunIo).toHaveBeenCalledWith("r", "n_llm"));
    expect(await screen.findByText("the example domain answer")).toBeInTheDocument();
    expect(screen.getByText("fetch example.com")).toBeInTheDocument();
  });

  it("shows the gated capture note when payloads are withheld (metadata)", async () => {
    getRunIo.mockResolvedValue({
      run_id: "r",
      node_id: "n_llm",
      capture_level: "metadata",
      inputs: null,
      outputs: null,
      bytes_in: 16,
      bytes_out: 25,
      truncated: false,
      reason: "Sizes only — full I/O capture is off for this agent",
    });
    render(withQuery(<TraceWaterfall runId="r" />));
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());
    fireEvent.click(screen.getByText("n_llm"));
    // The reason + the capture level both surface; the null panes render the em-dash placeholder.
    const note = await screen.findByText(/full I\/O capture is off/);
    expect(note.textContent).toMatch(/capture: metadata/);
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it("warns when a captured payload was truncated to the cap", async () => {
    getRunIo.mockResolvedValue({ ...FULL_IO, truncated: true });
    render(withQuery(<TraceWaterfall runId="r" />));
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());
    fireEvent.click(screen.getByText("n_llm"));
    expect(await screen.findByText(/Payload truncated to the capture cap/)).toBeInTheDocument();
  });

  it("fires onHover with the node_id for NODE rows and not for phase rows", async () => {
    const onHover = vi.fn();
    render(withQuery(<TraceWaterfall runId="r" onHover={onHover} />));
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());

    // A node row → fires the canvas-flash join with the node_id (NOT the span_id "llm").
    fireEvent.mouseEnter(screen.getByText("n_llm"));
    expect(onHover).toHaveBeenCalledWith({ kind: "node", id: "n_llm" });
    fireEvent.mouseLeave(screen.getByText("n_llm"));
    expect(onHover).toHaveBeenLastCalledWith(null);

    // A phase row must NOT fire (guarded by `isNode = node_id && !phase`).
    onHover.mockClear();
    fireEvent.mouseEnter(screen.getByText("model.generate"));
    expect(onHover).not.toHaveBeenCalled();
  });

  it("zooms the timeline and clamps at the minimum", async () => {
    const { container } = render(withQuery(<TraceWaterfall runId="r" />));
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());
    const readout = () => container.querySelector(".tabular-nums")?.textContent;
    const reset = screen.getByRole("button", { name: "Reset zoom" });

    expect(readout()).toBe("1.0×");
    expect(reset).toBeDisabled(); // fit is a no-op at 1×
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(readout()).toBe("1.5×");
    expect(reset).toBeEnabled();
    // Zooming out past 1× is clamped to MIN_ZOOM, never below.
    fireEvent.click(screen.getByRole("button", { name: "Zoom out" }));
    fireEvent.click(screen.getByRole("button", { name: "Zoom out" }));
    expect(readout()).toBe("1.0×");
    expect(reset).toBeDisabled();
  });

  it("renders an in-flight (open) span as a bounded pulsing bar", async () => {
    getRunTrace.mockResolvedValue({
      runId: "r",
      status: "streaming",
      spans: [
        {
          id: "r:_root",
          span_id: "root",
          parent_span_id: null,
          name: "run-1",
          status: "running",
          start_ns: 0,
          end_ns: null,
        },
        {
          id: "r:n_llm",
          span_id: "llm",
          parent_span_id: "root",
          node_id: "n_llm",
          node_type: "llm",
          name: "n_llm",
          status: "running",
          start_ns: 10,
          end_ns: null,
        },
      ],
    });
    const { container } = render(withQuery(<TraceWaterfall runId="r" />));
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());
    const pulsing = container.querySelector(".animate-pulse") as HTMLElement | null;
    expect(pulsing).not.toBeNull();
    // The clamp keeps the open bar a finite, in-track width (never NaN / >100%).
    expect(pulsing?.style.width ?? "").toMatch(/^\d+(\.\d+)?%$/);
  });

  it("degrades to a note when no trace is recorded", async () => {
    getRunTrace.mockResolvedValue({ runId: "r", status: "completed", spans: [] });
    render(withQuery(<TraceWaterfall runId="r" />));
    await waitFor(() => expect(screen.getByText(/Trace unavailable/)).toBeInTheDocument());
  });
});
