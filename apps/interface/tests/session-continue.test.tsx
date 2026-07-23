// Continuing a stored conversation. A session's target is not enough to keep talking to an agent —
// the composer and the run payload are properties of that agent's I/O BOUNDARY, which lives in the
// version the conversation was recorded against. Reopening a voice session with a text box (and
// sending a bare string to an audio boundary) was the largest divergence between the two chat entry
// points before the boundary seam; these pin it shut.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRouter,
} from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const getAgentVersion = vi.fn();
const getAgent = vi.fn();

vi.mock("../src/lib/api", () => ({
  ApiError: class ApiError extends Error {
    code?: string;
  },
  streamRun: vi.fn(async () => ({
    events: (async function* () {
      yield { event: "run", data: JSON.stringify({ runId: "run_s1", status: "completed" }) };
      yield { event: "message", data: "[DONE]" };
    })(),
    abort: vi.fn(),
  })),
  api: {
    getSession: vi.fn(),
    getAgent: (...a: unknown[]) => getAgent(...a),
    getAgentVersion: (...a: unknown[]) => getAgentVersion(...a),
    getRun: vi.fn(async () => ({ id: "run_s1", status: "completed", output: "ok", error: null })),
    uploadArtifact: vi.fn(async () => ({ ref: "art_s1", contentType: "audio/webm", bytes: 3 })),
    downloadArtifact: vi.fn(async () => new Blob(["x"], { type: "audio/wav" })),
    appendSessionTurns: vi.fn(async () => ({})),
    createSession: vi.fn(async (b: { id: string }) => ({ id: b.id })),
    deleteSession: vi.fn(),
  },
}));

import { api, streamRun } from "../src/lib/api";
import { SessionDetail } from "../src/routes/SessionDetail";

/** A stored version whose input boundary declares `modality`. */
function version(v: string, modality?: string) {
  return {
    agent_id: "agent.v",
    version: v,
    content_hash: `sha256:${v}`,
    seq: 1,
    created_at: "2026-07-01T00:00:00Z",
    view: null,
    ir: {
      schemaVersion: "1.0",
      id: "agent.v",
      name: "v",
      version: v,
      models: {},
      tools: {},
      nodes: [
        {
          id: "n_in",
          type: "input",
          kind: "boundary",
          config: modality ? { modality } : {},
        },
        { id: "n_out", type: "output", kind: "boundary", config: {} },
      ],
      edges: [],
    },
  };
}

/** A stored agent session, with or without the recorded version. */
function session(metadata: Record<string, unknown>) {
  return {
    id: "ses_1",
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    metadata,
    messages: [{ id: "m1", role: "user", content: "earlier turn", run_id: null }],
  };
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createRouter({
    routeTree: createRootRoute({ component: () => <SessionDetail /> }),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

vi.mock("@tanstack/react-router", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@tanstack/react-router");
  return { ...actual, useParams: () => ({ sessionId: "ses_1" }), useNavigate: () => vi.fn() };
});

beforeEach(() => {
  vi.clearAllMocks();
  getAgent.mockResolvedValue({ id: "agent.v", name: "v", versions: [{ version: "2.0.0" }] });
});

describe("continuing an agent session", () => {
  it("re-derives a voice boundary from the RECORDED version, not from latest", async () => {
    (api.getSession as ReturnType<typeof vi.fn>).mockResolvedValue(
      session({ kind: "bench.agent", agent_id: "agent.v", agent_version: "1.0.0" }),
    );
    getAgentVersion.mockResolvedValue(version("1.0.0", "audio"));
    mount();

    // The microphone composer, not a text box — and no bare string can be sent to an audio boundary.
    expect(await screen.findByRole("button", { name: "Record from microphone" })).toBeTruthy();
    expect(screen.queryByPlaceholderText("Message the agent…")).toBeNull();
    expect(getAgentVersion).toHaveBeenCalledWith("agent.v", "1.0.0");
    // Latest was never consulted — the pinned version is authoritative.
    expect(getAgent).not.toHaveBeenCalled();
  });

  it("continues on the version it was recorded against", async () => {
    (api.getSession as ReturnType<typeof vi.fn>).mockResolvedValue(
      session({ kind: "bench.agent", agent_id: "agent.v", agent_version: "1.0.0" }),
    );
    getAgentVersion.mockResolvedValue(version("1.0.0"));
    mount();
    await userEvent.type(await screen.findByPlaceholderText("Message the agent…"), "next turn");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(streamRun).toHaveBeenCalled());
    const body = (streamRun as ReturnType<typeof vi.fn>).mock.calls[0][1] as Record<
      string,
      unknown
    >;
    expect(body.version).toBe("1.0.0");
    expect(body.session_id).toBe("ses_1");
  });

  it("falls back to latest for a session recorded before the version was stored", async () => {
    // Sessions predating `agent_version` carry no pin — latest is the only honest answer, and the
    // boundary must come from THAT version rather than being assumed textual.
    (api.getSession as ReturnType<typeof vi.fn>).mockResolvedValue(
      session({ kind: "bench.agent", agent_id: "agent.v" }),
    );
    getAgentVersion.mockResolvedValue(version("2.0.0", "audio"));
    mount();
    expect(await screen.findByRole("button", { name: "Record from microphone" })).toBeTruthy();
    expect(getAgent).toHaveBeenCalledWith("agent.v");
    expect(getAgentVersion).toHaveBeenCalledWith("agent.v", "2.0.0");
  });

  it("falls back to latest when the pinned version has since been deleted", async () => {
    (api.getSession as ReturnType<typeof vi.fn>).mockResolvedValue(
      session({ kind: "bench.agent", agent_id: "agent.v", agent_version: "0.0.9" }),
    );
    getAgentVersion.mockImplementation(async (_id: string, v: string) => {
      if (v === "0.0.9") throw new Error("agent_version_not_found");
      return version("2.0.0");
    });
    mount();
    await userEvent.type(await screen.findByPlaceholderText("Message the agent…"), "next turn");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(streamRun).toHaveBeenCalled());
    // The run must NOT keep sending the id that 404'd.
    const body = (streamRun as ReturnType<typeof vi.fn>).mock.calls[0][1] as Record<
      string,
      unknown
    >;
    expect(body.version).toBe("2.0.0");
  });

  it("refuses to guess a boundary it could not read", async () => {
    // "We could not read it" must never present as "it takes text" — that is how a voice agent
    // ends up receiving a bare string.
    (api.getSession as ReturnType<typeof vi.fn>).mockResolvedValue(
      session({ kind: "bench.agent", agent_id: "agent.v", agent_version: "1.0.0" }),
    );
    getAgentVersion.mockRejectedValue(new Error("boom"));
    getAgent.mockRejectedValue(new Error("boom"));
    mount();
    expect(await screen.findByText(/input boundary is unknown/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
  });
});
