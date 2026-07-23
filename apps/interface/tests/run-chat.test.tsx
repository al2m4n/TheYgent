// The control-plane chat transport, driven through the real composer. What a graph's declared
// input boundary means for a CHAT turn: which controls appear, what shape the run body's `input`
// takes, whether the turn is server-recorded, and how a media answer comes back. These encodings
// had no coverage at all before the boundary seam existed.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRouter,
} from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { IRDocument } from "@theygent/ir-types";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/lib/api", () => ({
  ApiError: class ApiError extends Error {
    code?: string;
  },
  streamRun: vi.fn(async () => ({
    events: (async function* () {
      yield { event: "run", data: JSON.stringify({ runId: "run_c1", status: "streaming" }) };
      yield { event: "delta", data: JSON.stringify({ runId: "run_c1", delta: "ok" }) };
      yield { event: "run", data: JSON.stringify({ runId: "run_c1", status: "completed" }) };
      yield { event: "message", data: "[DONE]" };
    })(),
    abort: vi.fn(),
  })),
  api: {
    createSession: vi.fn(async (b: { id: string }) => ({ id: b.id })),
    appendSessionTurns: vi.fn(async () => ({})),
    getRun: vi.fn(async () => ({ id: "run_c1", status: "completed", output: "ok", error: null })),
    uploadArtifact: vi.fn(async () => ({ ref: "art_c1", contentType: "audio/webm", bytes: 3 })),
    downloadArtifact: vi.fn(async () => new Blob(["x"], { type: "audio/wav" })),
  },
}));

import { ChatView } from "../src/chat/ChatView";
import { useRunChat } from "../src/chat/useRunChat";
import { api, streamRun } from "../src/lib/api";
import { boundaryOf } from "../src/lib/modality";

function ir(input?: string, output?: string): IRDocument {
  return {
    schemaVersion: "1.0",
    id: "agent.c",
    name: "c",
    version: "0.1.0",
    models: {},
    tools: {},
    nodes: [
      { id: "n_in", type: "input", kind: "boundary", config: input ? { modality: input } : {} },
      { id: "n_out", type: "output", kind: "boundary", config: output ? { modality: output } : {} },
    ],
    edges: [],
  } as unknown as IRDocument;
}

function Harness({ doc }: { doc: IRDocument }) {
  const chat = useRunChat(
    { kind: "agent", agentId: "agent.c", agentName: "c", version: "0.1.0" },
    { boundary: boundaryOf(doc) },
  );
  return <ChatView controller={chat} emptyHint={null} />;
}

// A run-backed turn links to its run detail, so the transcript needs a router in scope.
function mount(doc: IRDocument) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createRouter({
    routeTree: createRootRoute({ component: () => <Harness doc={doc} /> }),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

/** The body of the most recent run POST. */
function lastBody(): Record<string, unknown> {
  const calls = (streamRun as ReturnType<typeof vi.fn>).mock.calls;
  return calls[calls.length - 1][1] as Record<string, unknown>;
}

beforeEach(() => vi.clearAllMocks());

describe("useRunChat input boundary", () => {
  it("sends a bare string for a text boundary and lets the server record the turn", async () => {
    mount(ir());
    await userEvent.type(await screen.findByPlaceholderText("Message the agent…"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(streamRun).toHaveBeenCalled());
    const body = lastBody();
    expect(body.input).toBe("hello");
    // A text turn is legible, so the SERVER appends it — the session id rides the run.
    expect(body.session_id).toBeTruthy();
    expect(api.appendSessionTurns).not.toHaveBeenCalled();
  });

  it("uploads a clip and sends only the reference for an audio boundary", async () => {
    mount(ir("audio", "text"));
    // The clip IS the message: no text box at all.
    expect(await screen.findByRole("button", { name: "Attach audio file" })).toBeTruthy();
    expect(screen.queryByPlaceholderText("Message the agent…")).toBeNull();
    const file = new File(["abc"], "note.webm", { type: "audio/webm" });
    await userEvent.upload(screen.getByLabelText<HTMLInputElement>("Audio file"), file);
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(api.uploadArtifact).toHaveBeenCalledWith(file));
    const body = lastBody();
    expect(body.input).toEqual({ ref: "art_c1", contentType: "audio/webm" });
    // A blob is not a session turn — the run is session-less and a readable label is recorded here.
    expect(body.session_id).toBeNull();
    await waitFor(() =>
      expect(api.appendSessionTurns).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({ user_content: "🎤 voice message" }),
      ),
    );
  });

  it("refuses to send a vision turn with no image", async () => {
    mount(ir("image"));
    await userEvent.type(
      await screen.findByPlaceholderText("Attach an image and ask about it…"),
      "what is this?",
    );
    // `imagesRequired`: the graph drills `$in.in.image`, so text alone would break the run.
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(streamRun).not.toHaveBeenCalled();
  });

  it("sends an image INLINE with the question, never as an artifact reference", async () => {
    mount(ir("image"));
    const png = new File(["\x89PNG"], "shot.png", { type: "image/png" });
    await userEvent.upload(await screen.findByLabelText<HTMLInputElement>("Image file"), png);
    await userEvent.type(
      screen.getByPlaceholderText("Attach an image and ask about it…"),
      "what is this?",
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(streamRun).toHaveBeenCalled());
    const body = lastBody() as { input: { image: string; text: string } };
    expect(body.input.text).toBe("what is this?");
    expect(body.input.image.startsWith("data:")).toBe(true);
    // The llm node string-templates the url into an image_url part — an `art_` ref would reach the
    // model as a literal string.
    expect(api.uploadArtifact).not.toHaveBeenCalled();
  });

  it("gives a json boundary a JSON editor and keeps server-side session memory", async () => {
    mount(ir("json"));
    const box = await screen.findByPlaceholderText('{"field": "value"}');
    await userEvent.type(box, '{{"code":"DE"}');
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(streamRun).toHaveBeenCalled());
    const body = lastBody();
    expect(body.input).toEqual({ code: "DE" });
    // A json turn is legible text — it keeps the conversation memory a chat needs.
    expect(body.session_id).toBeTruthy();
  });

  it("blocks send on malformed JSON and says why", async () => {
    mount(ir("json"));
    await userEvent.type(await screen.findByPlaceholderText('{"field": "value"}'), "{{oops");
    expect(screen.getByText(/Invalid JSON input/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(streamRun).not.toHaveBeenCalled();
  });

  it("downloads a media answer and plays it", async () => {
    (api.getRun as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: "run_c1",
      status: "completed",
      output: JSON.stringify({ ref: "art_reply", contentType: "audio/wav" }),
      error: null,
    });
    const { container } = mount(ir("text", "audio"));
    await userEvent.type(
      await screen.findByPlaceholderText("Type something to have it read back…"),
      "say hi",
    );
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(api.downloadArtifact).toHaveBeenCalledWith("art_reply"));
    await waitFor(() => expect(container.querySelector("audio")).not.toBeNull());
  });

  it("reads the answer from the run row when the graph streamed no tokens", async () => {
    // A graph that ends at a tool or a router emits no deltas at all — the persisted output IS the
    // answer. This is the other half of why the run row is read back, and the half a media-only
    // test never exercises.
    (streamRun as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      events: (async function* () {
        yield { event: "run", data: JSON.stringify({ runId: "run_c1", status: "completed" }) };
        yield { event: "message", data: "[DONE]" };
      })(),
      abort: vi.fn(),
    });
    (api.getRun as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: "run_c1",
      status: "completed",
      output: "the tool's answer",
      error: null,
    });
    mount(ir());
    await userEvent.type(await screen.findByPlaceholderText("Message the agent…"), "go");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getByText(/the tool's answer/)).toBeInTheDocument());
  });

  it("keeps a failed run's persisted answer alongside the error", async () => {
    // Recording the failure must not discard what the graph produced before it failed.
    (streamRun as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      events: (async function* () {
        yield { event: "run", data: JSON.stringify({ runId: "run_c1", status: "streaming" }) };
        yield { event: "message", data: "[DONE]" };
      })(),
      abort: vi.fn(),
    });
    (api.getRun as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: "run_c1",
      status: "failed",
      output: "partial answer before the failure",
      error: "downstream node exploded",
    });
    mount(ir());
    await userEvent.type(await screen.findByPlaceholderText("Message the agent…"), "go");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() =>
      expect(screen.getByText(/partial answer before the failure/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/downstream node exploded/)).toBeInTheDocument();
  });

  it("prompts for a description on an image-generation agent", async () => {
    // The caller's page-level placeholder must not win over what the boundary actually needs.
    mount(ir("text", "image"));
    expect(await screen.findByPlaceholderText("Describe an image to generate…")).toBeTruthy();
  });

  it("renders the error branch's prose on a media boundary instead of a blank bubble", async () => {
    // voice-desk's shape: the audio boundary is declared, but this run ended on the text error
    // branch. The answer must be read from the BYTES, not from what the boundary declared.
    (api.getRun as ReturnType<typeof vi.fn>).mockResolvedValue({
      id: "run_c1",
      status: "completed",
      output: "transcribe failed: no launcher registered",
      error: null,
    });
    (streamRun as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      events: (async function* () {
        yield { event: "run", data: JSON.stringify({ runId: "run_c1", status: "completed" }) };
        yield { event: "message", data: "[DONE]" };
      })(),
      abort: vi.fn(),
    });
    mount(ir("text", "audio"));
    await userEvent.type(
      await screen.findByPlaceholderText("Type something to have it read back…"),
      "say hi",
    );
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getByText(/transcribe failed/)).toBeInTheDocument());
    expect(api.downloadArtifact).not.toHaveBeenCalled();
  });
});
