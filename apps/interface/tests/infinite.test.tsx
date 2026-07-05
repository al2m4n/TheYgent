// Scroll pagination wiring: the flatten/dedup helper, the keyset infinite query (cursor via `before`),
// and the IntersectionObserver sentinel. The backend keyset contract (last row's `id` → next `before`,
// a short page → the end) is covered by the control-plane's own tests; this pins the FRONTEND side —
// page 2 carries `before=<last id>`, a short page stops paging, a shifted live window can't
// double-render a row, and the sentinel fires exactly when it should.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useInView } from "../src/lib/useInView";
import { flattenPages, useRunsInfinite } from "../src/queries";

function jsonResponse(data: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => data } as unknown as Response;
}

function wrap() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

describe("flattenPages", () => {
  it("concatenates pages in order and de-dupes by id (a shifted live window can't double-render)", () => {
    const data = {
      pages: [
        [{ id: "a" }, { id: "b" }],
        [{ id: "b" }, { id: "c" }],
      ],
    };
    expect(flattenPages(data).map((x) => x.id)).toEqual(["a", "b", "c"]);
  });
  it("returns [] for undefined data", () => {
    expect(flattenPages(undefined)).toEqual([]);
  });
});

describe("useRunsInfinite — keyset scroll pagination", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("page 1 has no cursor; page 2 sends before=<last id>; a short page ends paging", async () => {
    const page1 = Array.from({ length: 50 }, (_, i) => ({ id: `r${100 - i}` }));
    const page2 = Array.from({ length: 10 }, (_, i) => ({ id: `r${50 - i}` }));
    const urls: string[] = [];
    const fetchMock = vi.fn(async (url: string) => {
      urls.push(url);
      const before = new URL(url, "http://localhost").searchParams.get("before");
      return jsonResponse({ runs: before ? page2 : page1 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useRunsInfinite(), { wrapper: wrap() });
    await waitFor(() => expect(result.current.data?.pages.length).toBe(1));

    // Page 1: limit only, no cursor.
    expect(urls[0]).toMatch(/\/runs\?limit=50$/);
    expect(result.current.hasNextPage).toBe(true);

    // Fetching the next page (what the sentinel triggers) sends before = the last id of page 1 (r51).
    await act(async () => {
      await result.current.fetchNextPage();
    });
    await waitFor(() => expect(result.current.data?.pages.length).toBe(2));
    expect(urls[1]).toMatch(/before=r51(&|$)/);

    // The short second page (< 50) means the end — no further pages.
    expect(result.current.hasNextPage).toBe(false);
    expect(flattenPages(result.current.data).length).toBe(60);
  });
});

describe("useInView — the scroll sentinel", () => {
  afterEach(() => vi.unstubAllGlobals());

  function Probe({ onInView, enabled }: { onInView: () => void; enabled: boolean }) {
    const ref = useInView(onInView, { enabled });
    return <div ref={ref} data-testid="sentinel" />;
  }

  it("calls onInView when the observed element intersects", () => {
    class IO {
      cb: (entries: unknown[]) => void;
      constructor(cb: (entries: unknown[]) => void) {
        this.cb = cb;
      }
      observe() {
        this.cb([{ isIntersecting: true }]);
      }
      disconnect() {}
    }
    vi.stubGlobal("IntersectionObserver", IO);
    const onInView = vi.fn();
    render(<Probe onInView={onInView} enabled />);
    expect(onInView).toHaveBeenCalledTimes(1);
  });

  it("does not observe (or fire) while disabled", () => {
    const observe = vi.fn();
    class IO {
      observe() {
        observe();
      }
      disconnect() {}
    }
    vi.stubGlobal("IntersectionObserver", IO);
    const onInView = vi.fn();
    render(<Probe onInView={onInView} enabled={false} />);
    expect(observe).not.toHaveBeenCalled();
    expect(onInView).not.toHaveBeenCalled();
  });
});
