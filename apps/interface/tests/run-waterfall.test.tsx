// RunWaterfall — the ONE waterfall every surface (bench, run detail, chat trace) renders. Asserts
// the load-bearing contracts: (1) the run → node → phase TREE renders with phase rows visible,
// (2) collapse/re-expand a node's phases (Lucide chevron buttons keep their accessible names),
// (3) the WHOLE row selects — clicking the timeline track (not the label) embeds the detail panel
// INSIDE the waterfall (no modal/dialog) with the gated /nodes/{id}/io payloads, (4) the reserved
// `reasoning` output surfaces as its own tab and lands as the default when a model.generate row is
// selected — and is pulled OUT of the Output ports, (5) hover → onHoverNode fires the canvas-flash
// join for NODE rows only, (6) zoom is the mouse wheel over the timeline (clamped at 1×; "Fit"
// resets), (7) an in-flight span renders a bounded pulsing bar, (8) no trace degrades to a note,
// (9) the chat fold mounts the waterfall lazily — an unopened turn fetches nothing.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/lib/api", () => ({
  api: {
    getTrace: vi.fn(),
    getNodeIo: vi.fn(),
  },
  streamGet: vi.fn(),
}));

import { RunTraceBlock } from "../src/chat/RunTraceBlock";
import { RunWaterfall } from "../src/components/waterfall";
import { api } from "../src/lib/api";

function withQuery(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
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
    attributes: { "gen_ai.usage.input_tokens": 42, "gen_ai.usage.output_tokens": 7 },
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

// The llm node's captured I/O, including the reserved `reasoning` output entry.
const FULL_IO = {
  run_id: "r",
  node_id: "n_llm",
  capture_level: "full",
  inputs: { in: "fetch example.com" },
  outputs: { ok: "the example domain answer", reasoning: "let me think about that fetch" },
  bytes_in: 16,
  bytes_out: 25,
  truncated: false,
  reason: null,
};

const getTrace = api.getTrace as ReturnType<typeof vi.fn>;
const getNodeIo = api.getNodeIo as ReturnType<typeof vi.fn>;

// The timeline halves of the rendered rows, in row order (index 0 is the time ruler; the rows
// follow the DFS: root, n_llm, model.generate, tool.http_fetch, n_out).
function tracks(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>("[data-wf-track]")];
}

describe("RunWaterfall", () => {
  beforeEach(() => {
    getTrace.mockResolvedValue(SPANS);
    getNodeIo.mockResolvedValue(FULL_IO);
  });
  afterEach(() => vi.clearAllMocks());

  it("renders the run → node → phase tree, phases included", async () => {
    withQuery(<RunWaterfall runId="r" />);
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());
    expect(screen.getByText("model.generate")).toBeInTheDocument();
    expect(screen.getByText("tool.http_fetch")).toBeInTheDocument();
    expect(screen.getByText("n_out")).toBeInTheDocument();
  });

  it("collapses then re-expands a node's phase children", async () => {
    withQuery(<RunWaterfall runId="r" />);
    await waitFor(() => expect(screen.getByText("model.generate")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Collapse n_llm" }));
    expect(screen.queryByText("model.generate")).not.toBeInTheDocument();
    expect(screen.queryByText("tool.http_fetch")).not.toBeInTheDocument();
    expect(screen.getByText("n_llm")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Expand n_llm" }));
    expect(screen.getByText("model.generate")).toBeInTheDocument();
    expect(screen.getByText("tool.http_fetch")).toBeInTheDocument();
  });

  it("selects from ANYWHERE on the row and embeds the I/O detail in the card (no modal)", async () => {
    const { container } = withQuery(<RunWaterfall runId="r" />);
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());
    // Click the TIMELINE half of the n_llm row (ruler is index 0; n_llm is the 2nd row) — the
    // whole row is the target, not just the label column.
    fireEvent.click(tracks(container)[2]);
    await waitFor(() => expect(getNodeIo).toHaveBeenCalledWith("r", "n_llm"));
    // Embedded inside the waterfall container — never a dialog.
    const detail = within(screen.getByTestId("run-waterfall")).getByTestId("span-detail");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // Input and Output are stacked sections, BOTH visible at once — no tab clicks.
    expect(await within(detail).findByText("the example domain answer")).toBeInTheDocument();
    expect(within(detail).getByText("fetch example.com")).toBeInTheDocument();
  });

  it("selecting the run-root row shows the whole-run summary", async () => {
    const { container } = withQuery(<RunWaterfall runId="r" />);
    await waitFor(() => expect(screen.getByText("run-1")).toBeInTheDocument());
    fireEvent.click(tracks(container)[1]);
    expect(await screen.findByText(/Whole-run totals/)).toBeInTheDocument();
    expect(getNodeIo).not.toHaveBeenCalled();
  });

  it("shows reasoning as its own section on a model.generate row, alongside input and output", async () => {
    withQuery(<RunWaterfall runId="r" />);
    await waitFor(() => expect(screen.getByText("model.generate")).toBeInTheDocument());
    fireEvent.click(screen.getByText("model.generate"));
    // Input, Reasoning and Output all render together — no tabs, no clicks.
    expect(await screen.findByText("let me think about that fetch")).toBeInTheDocument();
    const detail = screen.getByTestId("span-detail");
    expect(within(detail).getByText("Reasoning")).toBeInTheDocument();
    expect(within(detail).getByText("fetch example.com")).toBeInTheDocument();
    expect(within(detail).getByText("the example domain answer")).toBeInTheDocument();
    // The Output ports show only real ports — the reserved `reasoning` entry is pulled out
    // into the section (the lowercase port label must not appear).
    expect(within(detail).queryByText("reasoning")).not.toBeInTheDocument();
  });

  it("shows the gated capture note when payloads are withheld (metadata)", async () => {
    getNodeIo.mockResolvedValue({
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
    withQuery(<RunWaterfall runId="r" />);
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());
    fireEvent.click(screen.getByText("n_llm"));
    const note = await screen.findByText(/full I\/O capture is off/);
    expect(note.textContent).toMatch(/capture: metadata/);
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(1);
  });

  it("warns when a captured payload was truncated to the cap", async () => {
    getNodeIo.mockResolvedValue({ ...FULL_IO, truncated: true });
    withQuery(<RunWaterfall runId="r" />);
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());
    fireEvent.click(screen.getByText("n_llm"));
    expect(await screen.findByText(/Payload truncated to the capture cap/)).toBeInTheDocument();
  });

  it("fires onHoverNode with the node_id for NODE rows and not for phase rows", async () => {
    const onHoverNode = vi.fn();
    withQuery(<RunWaterfall runId="r" onHoverNode={onHoverNode} />);
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());

    fireEvent.mouseEnter(screen.getByText("n_llm").closest("[role='button']") as HTMLElement);
    expect(onHoverNode).toHaveBeenCalledWith("n_llm");
    fireEvent.mouseLeave(screen.getByText("n_llm").closest("[role='button']") as HTMLElement);
    expect(onHoverNode).toHaveBeenLastCalledWith(null);

    onHoverNode.mockClear();
    fireEvent.mouseEnter(
      screen.getByText("model.generate").closest("[role='button']") as HTMLElement,
    );
    expect(onHoverNode).not.toHaveBeenCalled();
  });

  it("zooms with the mouse wheel over the timeline, clamps at 1×, and Fit resets", async () => {
    const { container } = withQuery(<RunWaterfall runId="r" />);
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());
    const readout = () => container.querySelector(".tabular-nums")?.textContent;
    const reset = screen.getByRole("button", { name: "Reset zoom" });
    const track = tracks(container)[0]; // the time ruler is a zoomable timeline surface too

    expect(readout()).toBe("1.0×");
    expect(reset).toBeDisabled();
    fireEvent.wheel(track, { deltaY: -100 }); // wheel up → zoom in (e^0.2 ≈ 1.22)
    expect(readout()).toBe("1.2×");
    expect(reset).toBeEnabled();
    // Wheeling out far past 1× clamps at the minimum, never below.
    fireEvent.wheel(track, { deltaY: 800 });
    expect(readout()).toBe("1.0×");
    fireEvent.wheel(track, { deltaY: -100 });
    fireEvent.click(reset);
    expect(readout()).toBe("1.0×");
    expect(reset).toBeDisabled();
  });

  it("renders an in-flight (open) span as a bounded pulsing bar", async () => {
    getTrace.mockResolvedValue([
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
    ]);
    const { container } = withQuery(<RunWaterfall runId="r" />);
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());
    const pulsing = container.querySelector(".animate-pulse") as HTMLElement | null;
    expect(pulsing).not.toBeNull();
    // The clamp keeps the open bar a finite, in-track width (never NaN / >100%).
    expect(pulsing?.style.width ?? "").toMatch(/^\d+(\.\d+)?%$/);
  });

  it("degrades to a note when no trace is recorded", async () => {
    getTrace.mockResolvedValue([]);
    withQuery(<RunWaterfall runId="r" />);
    await waitFor(() => expect(screen.getByText(/Trace unavailable/)).toBeInTheDocument());
  });
});

describe("RunTraceBlock (chat per-turn trace)", () => {
  beforeEach(() => {
    getTrace.mockResolvedValue(SPANS);
    getNodeIo.mockResolvedValue(FULL_IO);
  });
  afterEach(() => vi.clearAllMocks());

  it("starts folded and mounts the waterfall (and its fetch) only when opened", async () => {
    withQuery(<RunTraceBlock runId="r" />);
    expect(screen.getByText("Trace")).toBeInTheDocument();
    expect(screen.queryByTestId("run-waterfall")).not.toBeInTheDocument();
    expect(getTrace).not.toHaveBeenCalled();

    await userEvent.click(screen.getByText("Trace"));
    await waitFor(() => expect(getTrace).toHaveBeenCalledWith("r"));
    expect(screen.getByTestId("run-waterfall")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("n_llm")).toBeInTheDocument());
  });
});
