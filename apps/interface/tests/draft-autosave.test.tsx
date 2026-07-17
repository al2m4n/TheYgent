// The draft autosave loop: no draft while the document matches its baseline; the first divergence
// debounce-creates one; later divergences update it; flush saves immediately; publishing deletes
// the draft and resets the baseline; a draft discarded elsewhere (404) is re-minted.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { IRDocument } from "@theygent/ir-types";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    code: string;
    constructor(message: string, status = 500, code = "err") {
      super(message);
      this.status = status;
      this.code = code;
    }
  },
  flushDraftOnUnload: vi.fn(),
  api: {
    createDraft: vi.fn(async (body: { ir: IRDocument; agent_id?: string | null }) => ({
      id: "drf_1",
      agent_id: body.agent_id ?? null,
      owner_id: null,
      name: body.ir.name,
      node_count: (body.ir.nodes ?? []).length,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      ir: body.ir,
      view: null,
    })),
    updateDraft: vi.fn(async (id: string, body: { ir: IRDocument }) => ({
      id,
      agent_id: null,
      owner_id: null,
      name: body.ir.name,
      node_count: (body.ir.nodes ?? []).length,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      ir: body.ir,
      view: null,
    })),
    deleteDraft: vi.fn(async () => undefined),
  },
}));

import { type DraftSeed, useDraftAutosave } from "../src/hooks/useDraftAutosave";
import { ApiError, api } from "../src/lib/api";

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

function doc(name: string): IRDocument {
  return {
    schemaVersion: "1.0",
    id: "agent.t",
    name,
    version: "0.1.0",
    models: {},
    tools: {},
    nodes: [],
    edges: [],
  } as unknown as IRDocument;
}

const baseline = doc("base");
const seed: DraftSeed = {
  key: "new",
  baseline,
  draftId: null,
  agentId: null,
  savedAt: null,
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe("useDraftAutosave", () => {
  it("never creates a draft while the document matches its baseline", async () => {
    vi.useFakeTimers();
    try {
      const { result } = renderHook(({ ir }) => useDraftAutosave(seed, ir), {
        wrapper: wrap(),
        initialProps: { ir: baseline },
      });
      await act(() => vi.advanceTimersByTimeAsync(10_000));
      expect(api.createDraft).not.toHaveBeenCalled();
      expect(result.current.status).toBe("clean");
      expect(result.current.draftId).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("debounce-creates a draft on the first divergence, then updates it", async () => {
    vi.useFakeTimers();
    try {
      const { result, rerender } = renderHook(({ ir }) => useDraftAutosave(seed, ir), {
        wrapper: wrap(),
        initialProps: { ir: baseline },
      });
      rerender({ ir: doc("edited") });
      expect(result.current.status).toBe("pending");
      expect(result.current.hasUnsaved).toBe(true);
      await act(() => vi.advanceTimersByTimeAsync(2_000));
      expect(api.createDraft).toHaveBeenCalledTimes(1);
      expect(result.current.draftId).toBe("drf_1");
      expect(result.current.status).toBe("clean");

      rerender({ ir: doc("edited again") });
      await act(() => vi.advanceTimersByTimeAsync(2_000));
      expect(api.updateDraft).toHaveBeenCalledWith("drf_1", { ir: doc("edited again") });
      expect(api.createDraft).toHaveBeenCalledTimes(1); // still the one create
    } finally {
      vi.useRealTimers();
    }
  });

  it("flush saves immediately without waiting out the debounce", async () => {
    const { result, rerender } = renderHook(({ ir }) => useDraftAutosave(seed, ir), {
      wrapper: wrap(),
      initialProps: { ir: baseline },
    });
    rerender({ ir: doc("quick edit") });
    let ok = false;
    await act(async () => {
      ok = await result.current.flush();
    });
    expect(ok).toBe(true);
    expect(api.createDraft).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe("clean");
  });

  it("markPublished deletes the draft and re-baselines (no re-create for the same content)", async () => {
    vi.useFakeTimers();
    try {
      const edited = doc("to publish");
      const { result, rerender } = renderHook(({ ir }) => useDraftAutosave(seed, ir), {
        wrapper: wrap(),
        initialProps: { ir: baseline },
      });
      rerender({ ir: edited });
      await act(() => vi.advanceTimersByTimeAsync(2_000));
      expect(result.current.draftId).toBe("drf_1");

      await act(() => result.current.markPublished(edited));
      expect(api.deleteDraft).toHaveBeenCalledWith("drf_1");
      expect(result.current.draftId).toBeNull();
      expect(result.current.status).toBe("clean");

      // The published content is the new baseline — nothing further to save.
      await act(() => vi.advanceTimersByTimeAsync(10_000));
      expect(api.createDraft).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("re-mints a draft when the saved one was discarded elsewhere (404 on update)", async () => {
    vi.useFakeTimers();
    try {
      const { result, rerender } = renderHook(({ ir }) => useDraftAutosave(seed, ir), {
        wrapper: wrap(),
        initialProps: { ir: baseline },
      });
      rerender({ ir: doc("first") });
      await act(() => vi.advanceTimersByTimeAsync(2_000));
      expect(result.current.draftId).toBe("drf_1");

      vi.mocked(api.updateDraft).mockRejectedValueOnce(
        new ApiError("unknown draft", 404, "draft_not_found"),
      );
      rerender({ ir: doc("second") });
      await act(() => vi.advanceTimersByTimeAsync(2_000));
      expect(api.createDraft).toHaveBeenCalledTimes(2);
      expect(result.current.status).toBe("clean");
    } finally {
      vi.useRealTimers();
    }
  });

  it("surfaces a failed save as an error state that flush can retry", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(api.createDraft).mockRejectedValueOnce(new ApiError("boom", 500, "http_error"));
      const { result, rerender } = renderHook(({ ir }) => useDraftAutosave(seed, ir), {
        wrapper: wrap(),
        initialProps: { ir: baseline },
      });
      rerender({ ir: doc("edit") });
      await act(() => vi.advanceTimersByTimeAsync(2_000));
      expect(result.current.status).toBe("error");
      expect(result.current.hasUnsaved).toBe(true);

      vi.useRealTimers();
      let ok = false;
      await act(async () => {
        ok = await result.current.flush();
      });
      expect(ok).toBe(true);
      await waitFor(() => expect(result.current.status).toBe("clean"));
    } finally {
      vi.useRealTimers();
    }
  });
});
