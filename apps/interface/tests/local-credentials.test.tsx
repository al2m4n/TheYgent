// Settings → Local credentials: names in, names out (values write-only), add + remove over the
// user-side inference-plane store. The plane is mocked at `fetch`.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LocalCredentials } from "../src/components/LocalCredentials";

function json(data: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => data } as unknown as Response;
}

function pathOf(url: string): string {
  return new URL(url, "http://localhost").pathname.replace(/^\/__[a-z]+/, "");
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LocalCredentials />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Local credentials", () => {
  it("lists names (never values), adds a new one, and removes", async () => {
    const store: Record<string, string> = { OPENAI_API_KEY: "sk-secret" };
    const calls: { method: string; path: string; body?: { value?: string } }[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const path = pathOf(url);
      const body = init?.body ? JSON.parse(init.body as string) : undefined;
      calls.push({ method, path, body });
      if (path === "/admin/credentials" && method === "GET")
        return json({ credentials: Object.keys(store).map((name) => ({ name, hasValue: true })) });
      const m = path.match(/^\/admin\/credentials\/(.+)$/);
      if (m && method === "PUT") {
        store[decodeURIComponent(m[1])] = body?.value ?? "";
        return json({ name: decodeURIComponent(m[1]), hasValue: true });
      }
      if (m && method === "DELETE") {
        delete store[decodeURIComponent(m[1])];
        return json({}, 204);
      }
      return json({});
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPanel();

    // the existing name renders; the secret VALUE is never in the DOM
    await screen.findByText("OPENAI_API_KEY");
    expect(screen.queryByText("sk-secret")).not.toBeInTheDocument();

    // add a new credential — the value is posted, the field is redacted (type=password)
    fireEvent.change(screen.getByPlaceholderText("OPENAI_API_KEY"), {
      target: { value: "ANTHROPIC_API_KEY" },
    });
    const valueInput = screen.getByPlaceholderText("sk-…");
    expect(valueInput).toHaveAttribute("type", "password");
    fireEvent.change(valueInput, { target: { value: "sk-ant" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await screen.findByText("ANTHROPIC_API_KEY");
    const put = calls.find((c) => c.method === "PUT");
    expect(put?.body).toEqual({ value: "sk-ant" });
    expect(put?.path).toContain("ANTHROPIC_API_KEY");

    // remove one — the first click arms the confirmation, the second fires the DELETE
    fireEvent.click(screen.getAllByRole("button", { name: "Remove" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "Confirm remove?" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));
  });
});
