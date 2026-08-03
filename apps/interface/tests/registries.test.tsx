// Tests for the Registries page (installed + add) and the Browse hub screen (RTL over a mocked plane).
//
// The Registries page opens on installed models with an Add flow; Browse is the separate hub
// surface (search → quant variants with a fit badge → install with live progress). The inference
// plane is mocked at `fetch`, so this is pure FE wiring — the backend's own tests cover
// normalization / fit / the plane boundary.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRouter,
} from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../src/lib/api";
import { NotificationCenter, notify } from "../src/lib/notify";
import { BrowseModal, Registries } from "../src/routes/Registries";

const GB = 1_000_000_000;

const LIST = {
  engines: ["mlx", "llamacpp"],
  entries: [
    {
      provider: "huggingface",
      ref: "bartowski/Qwen2.5-7B-Instruct-GGUF",
      title: "Qwen2.5-7B-Instruct-GGUF",
      description: "bartowski",
      category: "models",
      kind: "model",
      sovereignty: "in-domain",
      engines: ["llamacpp"],
      badges: { downloads: 120000, likes: 300 },
      params: "7B",
      license: "apache-2.0",
      gated: false,
      updatedAt: "2026-06-01T00:00:00+00:00",
      installed: false,
      installedAs: null,
      variants: [],
    },
  ],
};

const DETAIL = {
  ...LIST.entries[0],
  description: "Qwen2.5 is the latest series of Qwen large language models.", // model-card blurb
  variants: [
    {
      id: "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
      label: "Q4_K_M",
      engine: "llamacpp",
      filename: "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
      sizeBytes: 4 * GB,
      fit: "fits",
      recommended: true,
      quality: "balanced",
      fitReason: "~5 GB needed · 16 GB RAM",
    },
    {
      id: "Qwen2.5-7B-Instruct-Q8_0.gguf",
      label: "Q8_0",
      engine: "llamacpp",
      filename: "Qwen2.5-7B-Instruct-Q8_0.gguf",
      sizeBytes: 8 * GB,
      fit: "too-large",
      recommended: false,
      quality: "max quality · large",
      fitReason: "~10 GB needed · 16 GB RAM",
    },
  ],
};

function jsonResponse(data: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => data } as unknown as Response;
}

// Route a stubbed request by its API path, HERMETICALLY. The api client prefixes the configured
// base URL, which a dev's `.env.local` may point at a relative proxy path (e.g. /__inf, /__cp) — so
// the raw url can be `/__inf/admin/catalog/models` rather than `http://localhost:8081/admin/...`.
// `new URL(relative)` throws and the `/__inf` prefix would break exact-path routing, so we tolerate a
// relative url (dummy base) and strip the proxy prefix. Keeps the suite green regardless of env.
function pathOf(url: string): string {
  return new URL(url, "http://localhost").pathname.replace(/^\/__[a-z]+/, "");
}

function installMock() {
  // Records calls so we can assert the install payload + that the repo slash is NOT encoded.
  const calls: { url: string; method: string; body?: unknown }[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ url, method, body });
    const path = pathOf(url);
    if (path === "/admin/catalog/models") return jsonResponse(LIST);
    if (path.startsWith("/admin/catalog/models/")) return jsonResponse(DETAIL);
    if (path === "/admin/catalog/install" && method === "POST")
      return jsonResponse({
        id: "dl-1",
        logicalId: body.logicalId,
        repo: body.repo,
        engine: body.engine,
        status: "downloading",
        doneBytes: 0,
        totalBytes: 4 * GB,
        error: null,
      });
    if (path.startsWith("/admin/catalog/downloads/"))
      return jsonResponse({
        id: "dl-1",
        logicalId: "qwen2-5-7b-instruct-gguf-q4-k-m",
        repo: DETAIL.ref,
        engine: "llamacpp",
        status: "done",
        doneBytes: 4 * GB,
        totalBytes: 4 * GB,
        error: null,
      });
    return jsonResponse({ models: [], downloads: [] });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

// The page uses <Link>, so it must render inside a router. A minimal memory router whose root
// renders the component under test gives Link a context without pulling in the whole app shell.
function renderWithRouter(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rootRoute = createRootRoute({ component: () => <>{ui}</> });
  const router = createRouter({
    routeTree: rootRoute,
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
      {/* Download progress reports to the global center (as in the real app shell), so mount it
          here too — the install tests assert on the toast it renders. */}
      <NotificationCenter />
    </QueryClientProvider>,
  );
}

const renderRegistries = () => renderWithRouter(<Registries />);
const renderBrowse = () => renderWithRouter(<BrowseModal onClose={() => {}} />);

afterEach(() => {
  notify.dismiss(); // clear any progress toasts before the next test (sonner state is module-global)
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Browse — the hub browser", () => {
  it("lists models filtered to ready engines, shows variants + fit, then installs with progress", async () => {
    installMock();
    renderBrowse();

    // A card appears for the searched model, and the engine filter is shown.
    await screen.findByText("Qwen2.5-7B-Instruct-GGUF");
    // MLX is unique to the engine-filter row; llama.cpp also tags the card, so it appears more than once.
    expect(screen.getByText("MLX")).toBeInTheDocument();
    expect(screen.getAllByText("llama.cpp").length).toBeGreaterThan(0);
    // Card metadata: param size, license, and downloads/stars.
    expect(screen.getByText("7B")).toBeInTheDocument();
    expect(screen.getByText("apache-2.0")).toBeInTheDocument();
    expect(screen.getByText(/120k/)).toBeInTheDocument(); // downloads, compacted

    // Expand the card → blurb + HF link + variants with quality hints + a fit badge.
    fireEvent.click(screen.getByRole("button", { name: /Qwen2\.5-7B-Instruct-GGUF/ }));
    await screen.findByText("Q4_K_M");
    expect(screen.getByText(/latest series of Qwen/)).toBeInTheDocument(); // model-card blurb
    expect(screen.getByRole("link", { name: /View on Hugging Face/ })).toHaveAttribute(
      "href",
      "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF",
    );
    expect(screen.getByText("balanced")).toBeInTheDocument(); // variant quality hint
    expect(screen.getByText("fits")).toBeInTheDocument();
    expect(screen.getByText("too large")).toBeInTheDocument();
    expect(screen.getByText("recommended")).toBeInTheDocument();

    // Install the recommended variant → confirm the logical id in the dialog.
    fireEvent.click(screen.getAllByRole("button", { name: "Install" })[0]);
    await screen.findByText("Install model");
    fireEvent.click(screen.getByRole("button", { name: /Download & install/ }));

    // The download tray shows live progress that resolves to Installed.
    await waitFor(() => expect(screen.getByText("Installed ✓")).toBeInTheDocument());
  });

  it("shows browse-time capability badges and filters to reasoning models client-side", async () => {
    const base = LIST.entries[0];
    const listWithCaps = {
      engines: ["mlx", "llamacpp"],
      entries: [
        {
          ...base,
          ref: "org/reasoner",
          title: "Reasoner",
          engines: ["mlx"],
          reasoning: true,
          toolCalling: true,
          vision: false,
          maxContext: 32768,
        },
        {
          ...base,
          ref: "org/plain-vlm",
          title: "PlainVLM",
          reasoning: false,
          toolCalling: false,
          vision: true,
          maxContext: null,
        },
      ],
    };
    const fetchMock = vi.fn(async (url: string) => {
      const path = pathOf(url);
      if (path === "/admin/catalog/models") return jsonResponse(listWithCaps);
      if (path.startsWith("/admin/catalog/models/")) return jsonResponse(DETAIL);
      return jsonResponse({ downloads: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderBrowse();

    // Both cards list; capability hints render as badges WITHOUT any probe/install (lowercase badge
    // text vs the Capitalized filter chips keeps them distinct).
    await screen.findByText("Reasoner");
    await screen.findByText("PlainVLM");
    expect(screen.getByText("reasoning")).toBeInTheDocument(); // on the reasoner card
    expect(screen.getByText("vision")).toBeInTheDocument(); // on the VLM card
    expect(screen.getByText("32k ctx")).toBeInTheDocument();

    // The "Reasoning" capability chip filters the loaded list to just the reasoning model.
    fireEvent.click(screen.getByRole("button", { name: "Reasoning" }));
    await waitFor(() => expect(screen.queryByText("PlainVLM")).not.toBeInTheDocument());
    expect(screen.getByText("Reasoner")).toBeInTheDocument();
  });

  it("blocks a variant the plane flagged as an incomplete model", async () => {
    // A text-to-image repo that publishes the denoiser alone: the files look installable (right
    // extension, right size), so the only thing standing between the user and a wasted
    // multi-gigabyte download is the plane's reason — the row must act on it, not just show it.
    const detail = {
      ...DETAIL,
      variants: [
        {
          ...DETAIL.variants[0],
          quality: "balanced",
          recommended: false,
          unusableReason: "krea.gguf holds a diffusion model's denoiser only",
        },
      ],
    };
    const fetchMock = vi.fn(async (url: string) => {
      const path = pathOf(url);
      if (path === "/admin/catalog/models") return jsonResponse(LIST);
      if (path.startsWith("/admin/catalog/models/")) return jsonResponse(detail);
      return jsonResponse({ downloads: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderBrowse();

    await screen.findByText("Qwen2.5-7B-Instruct-GGUF");
    fireEvent.click(screen.getByRole("button", { name: /Qwen2\.5-7B-Instruct-GGUF/ }));
    await screen.findByText("Q4_K_M");

    // The fit badge and the quality hint both give way — a file that cannot run is not "fits".
    expect(screen.getByText("incomplete")).toBeInTheDocument();
    expect(screen.queryByText("fits")).not.toBeInTheDocument();
    expect(screen.queryByText("balanced")).not.toBeInTheDocument();
    expect(screen.getByText("not a complete model")).toBeInTheDocument();

    // Install is refused here rather than at the download, and the reason is one hover away.
    const install = screen.getByRole("button", { name: "Install" });
    expect(install).toBeDisabled();
    expect(install).toHaveAttribute("title", "krea.gguf holds a diffusion model's denoiser only");
    fireEvent.click(install);
    expect(screen.queryByText("Install model")).not.toBeInTheDocument();
  });

  it("engine chips: clicking one (from all-on) turns it off and narrows to the other", async () => {
    const urls: string[] = [];
    const fetchMock = vi.fn(async (url: string) => {
      const path = pathOf(url);
      if (path === "/admin/catalog/models") {
        urls.push(url);
        return jsonResponse(LIST);
      }
      if (path.startsWith("/admin/catalog/models/")) return jsonResponse(DETAIL);
      return jsonResponse({ downloads: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderBrowse();

    // Both engines start active (the empty = all sentinel); the initial query sends no engine filter.
    await screen.findByRole("button", { name: "MLX" });
    expect(urls.some((u) => /[?&]engines=/.test(u))).toBe(false);

    // Click the llama.cpp chip → it turns OFF; the query narrows to exactly MLX (the OTHER one
    // stays on — it is NOT flipped). Regression test for the "clicking one toggles the other" bug.
    fireEvent.click(screen.getByRole("button", { name: "llama.cpp" }));
    await waitFor(() => expect(urls.some((u) => /[?&]engines=mlx(&|$)/.test(u))).toBe(true));
    expect(urls.some((u) => /engines=llamacpp/.test(u))).toBe(false);
  });

  it("offers cancel while a download is in flight and calls the cancel endpoint", async () => {
    let cancelled = false;
    const job = (status: string) => ({
      id: "dl-9",
      logicalId: "qwen2-5-7b-instruct-gguf-q4-k-m",
      repo: DETAIL.ref,
      engine: "llamacpp",
      status,
      doneBytes: 1 * GB,
      totalBytes: 4 * GB,
      error: null,
    });
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const path = pathOf(url);
      const method = init?.method ?? "GET";
      if (path === "/admin/catalog/models") return jsonResponse(LIST);
      if (path.startsWith("/admin/catalog/models/")) return jsonResponse(DETAIL);
      if (path === "/admin/catalog/install") return jsonResponse(job("downloading"));
      if (path.endsWith(":cancel") && method === "POST") {
        cancelled = true;
        return jsonResponse(job("cancelled"));
      }
      if (path.startsWith("/admin/catalog/downloads/"))
        return jsonResponse(job(cancelled ? "cancelled" : "downloading"));
      return jsonResponse({ downloads: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderBrowse();

    await screen.findByText("Qwen2.5-7B-Instruct-GGUF");
    fireEvent.click(screen.getByRole("button", { name: /Qwen2\.5-7B-Instruct-GGUF/ }));
    await screen.findByText("Q4_K_M");
    fireEvent.click(screen.getAllByRole("button", { name: "Install" })[0]);
    await screen.findByText("Install model");
    fireEvent.click(screen.getByRole("button", { name: /Download & install/ }));

    // The tray shows a live download with a cancel control; clicking it cancels. (The install
    // dialog has its own "Cancel" — target the download control by its accessible name.)
    fireEvent.click(await screen.findByRole("button", { name: "Cancel download" }));
    await waitFor(() => expect(screen.getByText("Cancelled.")).toBeInTheDocument());
    expect(cancelled).toBe(true);
  });

  it("shows the air-gap empty state when no engine is ready", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (pathOf(url) === "/admin/catalog/models")
        return jsonResponse({ entries: [], engines: [] });
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetchMock);
    renderBrowse();
    await screen.findByText(/No local engine is ready/);
  });
});

describe("Registries page — installed + add", () => {
  it("opens on installed models and probes capabilities on demand", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      const path = pathOf(url);
      if (path === "/admin/models")
        return jsonResponse({
          models: [
            { logicalId: "qwen-reasoner", binding: { binding: "mlx", model: "x" }, state: {} },
          ],
        });
      if (path === "/admin/engines") return jsonResponse({ maxResident: 2, resident: [] });
      if (path.endsWith("/capabilities"))
        return jsonResponse({
          toolCalling: true,
          structuredOutput: false,
          vision: false,
          reasoning: true,
          maxContext: 32768,
          approximate: true,
        });
      return jsonResponse({ downloads: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderRegistries();
    // The installed model shows without any tab switch (Installed is the page's landing view).
    await screen.findByText("qwen-reasoner");
    // Capabilities are not auto-fetched (probing warms the engine) — there's a probe button.
    fireEvent.click(screen.getByRole("button", { name: "Probe capabilities" }));
    await screen.findByText("reasoning");
    expect(screen.getByText("tools")).toBeInTheDocument();
    expect(screen.getByText("32k ctx")).toBeInTheDocument();
    expect(screen.getByText("approx")).toBeInTheDocument();
  });

  it("adds a model by pasting a Hugging Face id and offers a Browse link", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      const path = pathOf(url);
      if (path === "/admin/models") return jsonResponse({ models: [] });
      if (path === "/admin/engines") return jsonResponse({ maxResident: 2, resident: [] });
      if (path.startsWith("/admin/catalog/models/")) return jsonResponse(DETAIL);
      return jsonResponse({ downloads: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderRegistries();

    await screen.findByText(/No models registered yet/);
    fireEvent.click(screen.getByRole("button", { name: /Add model/ }));
    // The Add panel's paste box resolves a ref straight to its installable variants.
    fireEvent.change(screen.getByPlaceholderText(/mlx-community/), {
      target: { value: "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await screen.findByText("Q4_K_M"); // resolved → variants shown inline
    expect(screen.getByText("balanced")).toBeInTheDocument();
  });

  it("hosted/local form offers stored credentials as secret:// options (no hf/url source)", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      const path = pathOf(url);
      if (path === "/admin/models") return jsonResponse({ models: [] });
      if (path === "/admin/engines") return jsonResponse({ maxResident: 2, resident: [] });
      if (path === "/admin/credentials")
        return jsonResponse({ credentials: [{ name: "OPENAI_API_KEY", hasValue: true }] });
      return jsonResponse({ downloads: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderRegistries();

    await screen.findByText(/No models registered yet/);
    fireEvent.click(screen.getByRole("button", { name: /Add model/ }));
    fireEvent.click(screen.getByRole("button", { name: "Hosted / local" }));

    // The credential picker lists the stored secret (openai-compatible is the default binding).
    await screen.findByRole("option", { name: "OPENAI_API_KEY" });
    // No hf/url Source options — manual managed registration is local-path only.
    expect(screen.queryByRole("option", { name: "url" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "hf" })).not.toBeInTheDocument();
  });

  it("opens the hub browser in a pop-up modal (not a separate page)", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      const path = pathOf(url);
      if (path === "/admin/models") return jsonResponse({ models: [] });
      if (path === "/admin/engines") return jsonResponse({ maxResident: 2, resident: [] });
      if (path === "/admin/catalog/models") return jsonResponse(LIST);
      return jsonResponse({ downloads: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderRegistries();

    await screen.findByText(/No models registered yet/);
    fireEvent.click(screen.getByRole("button", { name: /Add model/ }));
    // "Browse Hugging Face" is a button (not a link) — clicking it opens the hub modal in place.
    fireEvent.click(screen.getByRole("button", { name: /Browse Hugging Face/ }));
    expect(
      await screen.findByText("Browse and install directly into your inference plane"),
    ).toBeInTheDocument();
    // …and the hub search + results render inside the modal.
    await screen.findByText("Qwen2.5-7B-Instruct-GGUF");
    expect(screen.getByPlaceholderText(/qwen, llama, phi/)).toBeInTheDocument();
  });
});

describe("catalog api client", () => {
  it("does not percent-encode the repo slash and posts the install payload verbatim", async () => {
    const calls = installMock();
    await api.getCatalogModel("bartowski/Qwen2.5-7B-Instruct-GGUF");
    await api.installCatalogModel({
      repo: "bartowski/Qwen2.5-7B-Instruct-GGUF",
      engine: "llamacpp",
      variantId: "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
      logicalId: "qwen-fast",
    });
    const getCall = calls.find((c) => c.method === "GET" && c.url.includes("/models/bartowski"));
    expect(getCall?.url).toContain("/admin/catalog/models/bartowski/Qwen2.5-7B-Instruct-GGUF");
    expect(getCall?.url).not.toContain("%2F"); // the slash stays a path separator
    const post = calls.find((c) => c.method === "POST");
    expect(post?.body).toMatchObject({ repo: expect.any(String), logicalId: "qwen-fast" });
  });
});
