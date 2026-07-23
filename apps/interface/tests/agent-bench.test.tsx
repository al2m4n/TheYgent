// AgentBench version pinning: the bench must default to the LATEST saved version, and follow it
// as fresh data arrives, while still honoring an explicitly picked older version.
// Plus the human-in-the-loop affordance: a durable run paused at a human node shows a resume panel
// that delivers the awaited input.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/lib/api", () => ({
  ApiError: class ApiError extends Error {
    code?: string;
  },
  // The interactive Run streams like every chat: reasoning frames, then answer deltas. The
  // frames carry an empty runId so the trace/canvas block is skipped (keeps the test light) and
  // the post-stream canonical-output lookup is not taken (the streamed text IS the result).
  streamRun: vi.fn(async () => ({
    events: (async function* () {
      yield { event: "run", data: JSON.stringify({ runId: "", status: "streaming" }) };
      yield { event: "reasoning", data: JSON.stringify({ runId: "", reasoning: "thinking hard" }) };
      yield { event: "delta", data: JSON.stringify({ runId: "", delta: "The answer " }) };
      yield { event: "delta", data: JSON.stringify({ runId: "", delta: "is **42**" }) };
      yield { event: "run", data: JSON.stringify({ runId: "", status: "completed" }) };
      yield { event: "message", data: "[DONE]" };
    })(),
    abort: vi.fn(),
  })),
  api: {
    runAgentDurable: vi.fn(async () => ({ run_id: "run_dur_1" })),
    getRun: vi.fn(async () => ({ id: "run_dur_1", status: "completed", output: "5", error: null })),
    getAgentVersion: vi.fn(async () => ({ ir: { id: "agent.docker", models: {} } })),
    resumeRun: vi.fn(async () => ({ runId: "run_dur_1", status: "resuming" })),
    listPresets: vi.fn(async () => []),
    uploadArtifact: vi.fn(async () => ({ ref: "art_bench1", contentType: "audio/webm", bytes: 3 })),
    downloadArtifact: vi.fn(async () => new Blob(["x"], { type: "image/png" })),
    createSession: vi.fn(async (b: { id: string }) => ({ id: b.id })),
  },
}));

import { AgentBench } from "../src/bench/AgentBench";
import { type AgentDetail, api, streamRun } from "../src/lib/api";

function withQuery(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

// The run buttons wait for the pinned IR (durable-only detection needs it) — settle first.
async function runButtonReady(name = "Run") {
  await waitFor(() => {
    const b = screen.getByRole("button", { name }) as HTMLButtonElement;
    expect(b.disabled).toBe(false);
  });
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

  it("defaults the pin to the latest version and runs it (streamed, chat-style result)", async () => {
    withQuery(<AgentBench agent={agent} />);
    const pin = screen.getByRole("combobox", { name: "Version" }) as HTMLSelectElement;
    expect(pin.value).toBe("0.1.2"); // latest, not the older llama-3.2 version

    fireEvent.change(screen.getByPlaceholderText("Run input…"), { target: { value: "hi" } });
    await runButtonReady();
    fireEvent.click(screen.getByRole("button", { name: "Run" })); // the split button runs directly
    await waitFor(() =>
      expect(streamRun).toHaveBeenCalledWith("/agents/agent.docker/runs", {
        input: "hi",
        stream: true,
        version: "0.1.2",
      }),
    );
    // The result presents like a chat answer: the thinking block + the markdown-rendered answer.
    expect(await screen.findByText("Thinking")).toBeTruthy();
    expect((await screen.findByText("42")).tagName).toBe("STRONG"); // **42** rendered as markdown
  });

  it("Enter in the input runs, like the chat composer sends", async () => {
    withQuery(<AgentBench agent={agent} />);
    const box = screen.getByPlaceholderText("Run input…");
    fireEvent.change(box, { target: { value: "hi" } });
    await runButtonReady();
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() =>
      expect(streamRun).toHaveBeenCalledWith("/agents/agent.docker/runs", {
        input: "hi",
        stream: true,
        version: "0.1.2",
      }),
    );
  });

  it("honors a deliberately picked older version", async () => {
    withQuery(<AgentBench agent={agent} />);
    const pin = screen.getByRole("combobox", { name: "Version" }) as HTMLSelectElement;
    fireEvent.change(pin, { target: { value: "0.1.1" } });
    expect(pin.value).toBe("0.1.1");

    fireEvent.change(screen.getByPlaceholderText("Run input…"), { target: { value: "hi" } });
    await runButtonReady();
    fireEvent.click(screen.getByRole("button", { name: "Run" })); // the split button runs directly
    await waitFor(() =>
      expect(streamRun).toHaveBeenLastCalledWith("/agents/agent.docker/runs", {
        input: "hi",
        stream: true,
        version: "0.1.1",
      }),
    );
  });

  it("the Run options menu offers Run + Run durably, and Run durably uses the durable endpoint", async () => {
    withQuery(<AgentBench agent={agent} />);
    fireEvent.change(screen.getByPlaceholderText("Run input…"), { target: { value: "5" } });
    await runButtonReady();
    // The split button's caret opens a menu with both paths. The menu primitive opens on a real
    // pointer sequence, so drive it with userEvent rather than a bare click event.
    await userEvent.click(screen.getByRole("button", { name: "Run options" }));
    expect(await screen.findByRole("menuitem", { name: "Run" })).toBeTruthy();
    await userEvent.click(screen.getByRole("menuitem", { name: "Run durably" }));
    await waitFor(() =>
      expect(api.runAgentDurable).toHaveBeenCalledWith("agent.docker", {
        input: "5",
        version: "0.1.2",
      }),
    );
    // the normal Run path was NOT taken.
    expect(streamRun).not.toHaveBeenCalled();
  });

  it("JSON input mode parses loudly and sends the parsed object", async () => {
    withQuery(<AgentBench agent={agent} />);
    fireEvent.change(screen.getByRole("combobox", { name: "Input mode" }), {
      target: { value: "json" },
    });
    const box = screen.getByPlaceholderText('{"field": "value"}');
    fireEvent.change(box, { target: { value: "{broken" } });
    // Malformed JSON never leaves the tab: a visible parse error, run disabled.
    expect(screen.getByText(/Invalid JSON input/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Run" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(box, { target: { value: '{"path": "/tmp/x", "question": "q"}' } });
    await runButtonReady();
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() =>
      expect(streamRun).toHaveBeenCalledWith("/agents/agent.docker/runs", {
        input: { path: "/tmp/x", question: "q" },
        stream: true,
        version: "0.1.2",
      }),
    );
  });

  it("a durable run paused at a human node shows the resume panel and delivers the input", async () => {
    (api.getRun as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: "run_dur_1",
      status: "waiting",
      awaiting_node: "n_gate",
      output: null,
      error: null,
    });
    withQuery(<AgentBench agent={agent} />);
    await runButtonReady();
    await userEvent.click(screen.getByRole("button", { name: "Run options" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Run durably" }));

    // The poll reports "waiting" → the resume panel names the node and offers a Resume box.
    await waitFor(() => expect(screen.getByText("n_gate")).toBeTruthy());
    fireEvent.change(screen.getByPlaceholderText("Reply to the waiting node…"), {
      target: { value: "approved!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(api.resumeRun).toHaveBeenCalledWith("run_dur_1", "approved!"));
  });
});

// The bench must respect the pinned version's I/O boundary in BOTH halves — the single-shot Run
// and the in-modal chat — using the same derivation New Chat and the canvas Test panel use.
describe("AgentBench input boundary", () => {
  beforeEach(() => vi.clearAllMocks());

  /** Point the version fetch at an IR whose boundaries declare the given modalities. */
  function pinBoundary(input?: string, output?: string) {
    (api.getAgentVersion as ReturnType<typeof vi.fn>).mockResolvedValue({
      ir: {
        id: "agent.docker",
        models: {},
        nodes: [
          { id: "n_in", type: "input", kind: "boundary", config: input ? { modality: input } : {} },
          {
            id: "n_out",
            type: "output",
            kind: "boundary",
            config: output ? { modality: output } : {},
          },
        ],
        edges: [],
      },
    });
  }

  it("offers the mic and refuses a run until a clip is staged, then sends the artifact ref", async () => {
    pinBoundary("audio", "audio");
    withQuery(<AgentBench agent={agent} />);

    // Two mics render for a voice agent: the single-shot Run control and the chat composer below
    // it. The first is the Run control (the chat fold is rendered after it).
    await waitFor(() =>
      expect(
        screen.getAllByRole("button", { name: "Record from microphone" }).length,
      ).toBeGreaterThan(0),
    );
    expect((screen.getByRole("button", { name: "Run" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/this graph's input boundary is audio/i)).toBeTruthy();

    const file = new File(["abc"], "note.webm", { type: "audio/webm" });
    // The bench renders BOTH halves for a voice agent — the single-shot Run control (first in DOM
    // order) and the chat composer below it. This exercises the Run half.
    await userEvent.upload(screen.getAllByLabelText<HTMLInputElement>("Audio file")[0], file);
    await runButtonReady();
    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(api.uploadArtifact).toHaveBeenCalledWith(file));
    expect(streamRun).toHaveBeenCalledWith("/agents/agent.docker/runs", {
      input: { ref: "art_bench1", contentType: "audio/webm" },
      stream: true,
      version: "0.1.2",
    });
  });

  it("opens in JSON for a json boundary without touching the mode toggle", async () => {
    pinBoundary("json");
    withQuery(<AgentBench agent={agent} />);
    await waitFor(() =>
      expect(
        (screen.getByRole("combobox", { name: "Input mode" }) as HTMLSelectElement).value,
      ).toBe("json"),
    );
    // Both halves show a JSON box for a json boundary; the first is the single-shot Run control.
    const box = screen.getAllByPlaceholderText('{"field": "value"}')[0];
    fireEvent.change(box, { target: { value: '{"code":"DE"}' } });
    await runButtonReady();
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() =>
      expect(streamRun).toHaveBeenCalledWith("/agents/agent.docker/runs", {
        input: { code: "DE" },
        stream: true,
        version: "0.1.2",
      }),
    );
  });

  it("shows a generated image instead of the artifact-reference JSON", async () => {
    pinBoundary("text", "image");
    // An image agent streams no text at all: its answer is the artifact the run row carries.
    (streamRun as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      events: (async function* () {
        yield { event: "run", data: JSON.stringify({ runId: "run_x", status: "streaming" }) };
        yield { event: "run", data: JSON.stringify({ runId: "run_x", status: "completed" }) };
        yield { event: "message", data: "[DONE]" };
      })(),
      abort: vi.fn(),
    });
    (api.getRun as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: "run_x",
      status: "completed",
      output: JSON.stringify({ ref: "art_pic", contentType: "image/png" }),
      error: null,
    });
    const { container } = withQuery(<AgentBench agent={agent} />);
    await runButtonReady();
    fireEvent.change(screen.getByPlaceholderText("Run input…"), { target: { value: "a cat" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(api.downloadArtifact).toHaveBeenCalledWith("art_pic"));
    await waitFor(() => expect(container.querySelector("img")).not.toBeNull());
    // The raw reference is never shown as prose.
    expect(screen.queryByText(/art_pic/)).toBeNull();
  });

  it("gives the in-modal chat the same boundary — a voice agent gets the mic there too", async () => {
    pinBoundary("audio", "audio");
    withQuery(<AgentBench agent={agent} />);
    // Two mics: the single-shot Run control and the chat composer beneath it.
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: "Record from microphone" })).toHaveLength(2),
    );
  });

  it("shows the plain text box for an agent whose input node declares no modality", async () => {
    pinBoundary();
    withQuery(<AgentBench agent={agent} />);
    await runButtonReady();
    expect(screen.getByPlaceholderText("Run input…")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Record from microphone" })).toBeNull();
  });
});
