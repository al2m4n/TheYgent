// The Settings page: tabs render; catalog entries render honestly (env-pinned = locked with the
// env var named, env-capped = editable with the cap shown); dirty-state saves PATCH only the
// changed keys (null = reset); the sensitive headers editor sends a full replacement and never
// shows a value; the collector test button covers success and failure; boot warnings render loud;
// endpoint overrides persist to localStorage; the inference plane's pinned ceiling is disabled;
// the HF token PUTs to the reserved credential name.
//
// The api OBJECT is mocked; the endpoint-override helpers stay REAL (they are localStorage-only),
// so the Overview override editor is exercised end to end.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SettingEntry, SettingsView } from "../src/lib/api";

const mocks = vi.hoisted(() => ({
  getSettings: vi.fn(),
  patchSettings: vi.fn(),
  testOtlp: vi.fn(),
  getInferenceSettings: vi.fn(),
  patchInferenceSettings: vi.fn(),
  getInferenceDiagnostics: vi.fn(),
  getEngines: vi.fn(),
  listModels: vi.fn(),
  listCredentials: vi.fn(),
  putCredential: vi.fn(),
  deleteCredential: vi.fn(),
  controlHealth: vi.fn(),
  inferenceHealth: vi.fn(),
}));

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>();
  return { ...actual, api: { ...actual.api, ...mocks } };
});

import { Settings } from "../src/routes/Settings";

function entry(over: Partial<SettingEntry> & { key: string }): SettingEntry {
  return {
    value: null,
    stored_value: null,
    default: null,
    type: "str",
    source: "default",
    env_var: null,
    env_pinned: false,
    env_capped: false,
    env_cap_value: null,
    apply: "live",
    restart_pending: false,
    stored_invalid: false,
    constraints: null,
    description: "",
    group: "telemetry",
    ...over,
  };
}

function fixture(): SettingsView {
  return {
    settings: [
      // Capped: stored "full" but the env cap tightens the effective value to "metadata".
      entry({
        key: "telemetry.io_capture",
        type: "enum",
        value: "metadata",
        stored_value: "full",
        default: "full",
        source: "stored",
        env_capped: true,
        env_cap_value: "metadata",
        env_var: "THEYGENT_IO_CAPTURE",
        constraints: { choices: ["off", "metadata", "full"] },
        description: "What run I/O the span store records.",
      }),
      entry({
        key: "telemetry.io_capture_max_bytes",
        type: "int",
        value: 262144,
        default: 262144,
        constraints: { min: 1024, max: 10485760 },
      }),
      entry({ key: "telemetry.otlp_enabled", type: "bool", value: false, default: false }),
      // Stored, so the "cleared text = staged reset" path is exercised.
      entry({
        key: "telemetry.otlp_endpoint",
        type: "str",
        value: "http://collector.local:4318",
        stored_value: "http://collector.local:4318",
        default: null,
        source: "stored",
      }),
      entry({
        key: "telemetry.otlp_headers",
        type: "secret",
        value: { set: true, names: ["Authorization"] },
        stored_value: { set: true, names: ["Authorization"] },
        source: "stored",
      }),
      entry({ key: "telemetry.otlp_redact_attrs", type: "list[str]", value: [], default: [] }),
      // Pinned: the env var owns it; the UI must lock the control.
      entry({
        key: "mcp.call_timeout_s",
        group: "mcp",
        type: "float",
        value: 120,
        default: 120,
        source: "env",
        env_pinned: true,
        env_var: "THEYGENT_MCP_CALL_TIMEOUT_S",
        apply: "live (next connect)",
        constraints: { min: 1, max: 3600 },
      }),
      entry({
        key: "mcp.connect_timeout_s",
        group: "mcp",
        type: "float",
        value: 30,
        default: 30,
        apply: "live (next connect)",
        constraints: { min: 1, max: 600 },
      }),
      entry({
        key: "mcp.oauth_redirect_url",
        group: "mcp",
        type: "str",
        value: "http://localhost:8080/mcp/oauth/callback",
        default: "http://localhost:8080/mcp/oauth/callback",
      }),
      entry({
        key: "mcp.extra_registries",
        group: "mcp",
        type: "list[registry]",
        value: [],
        default: [],
      }),
      entry({
        key: "rag.chunk_max_tokens",
        group: "rag",
        type: "int",
        value: 450,
        default: 450,
        constraints: { min: 50, max: 4000 },
      }),
      entry({
        key: "rag.chunk_overlap_tokens",
        group: "rag",
        type: "int",
        value: 60,
        default: 60,
        constraints: { min: 0, max: 1000 },
      }),
      entry({
        key: "rag.crawl_desired_concurrency",
        group: "rag",
        type: "int",
        value: 2,
        default: 2,
        constraints: { min: 1, max: 16 },
      }),
      entry({
        key: "rag.crawl_max_concurrency",
        group: "rag",
        type: "int",
        value: 4,
        default: 4,
        constraints: { min: 1, max: 32 },
      }),
      entry({
        key: "rag.max_upload_bytes",
        group: "rag",
        type: "int",
        value: 52428800,
        default: 52428800,
        constraints: { min: 1048576, max: 1073741824 },
      }),
      entry({
        key: "rag.default_top_k",
        group: "rag",
        type: "int",
        value: 5,
        default: 5,
        constraints: { min: 1, max: 50 },
      }),
      entry({ key: "rag.default_embedding_model", group: "rag", type: "str", value: null }),
    ],
    boot: [
      {
        key: "THEYGENT_SECRET_KEY",
        value: "(ephemeral)",
        description: "Encryption key for stored secrets.",
        warning: "Secret key is ephemeral — stored secrets will not survive a restart.",
      },
      {
        key: "THEYGENT_DURABLE",
        value: "0",
        description: "Durable runtime mode.",
        warning: null,
      },
      // Structured boot facts: the wire really carries objects and lists here, not just scalars.
      {
        key: "database",
        value: { host: "localhost:5432", database: "theygent" },
        description: "Where run history lives.",
        warning: null,
      },
      {
        key: "cors_origins",
        value: ["http://localhost:5174", "http://localhost:1420"],
        description: "Allowed browser origins.",
        warning: null,
      },
    ],
    orphaned: [],
  };
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Settings />
    </QueryClientProvider>,
  );
}

// This test environment ships no localStorage (the app code treats it as optional) — stub an
// in-memory one so the endpoint-override editor's persistence is actually exercised.
function stubLocalStorage(): void {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => void store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  } as Storage);
}

beforeEach(() => {
  stubLocalStorage();
  mocks.getSettings.mockResolvedValue(fixture());
  mocks.patchSettings.mockImplementation(async () => ({ ...fixture(), apply_errors: {} }));
  mocks.testOtlp.mockResolvedValue({ ok: true, error: null, latency_ms: 42 });
  mocks.getInferenceSettings.mockResolvedValue({
    maxResident: {
      value: 2,
      default: 2,
      source: "default",
      envVar: "THEYGENT_MAX_RESIDENT",
      apply: "live",
    },
  });
  mocks.patchInferenceSettings.mockImplementation(async (body: { maxResident: number | null }) => ({
    maxResident: {
      value: body.maxResident ?? 2,
      default: 2,
      source: body.maxResident == null ? "default" : "stored",
      envVar: "THEYGENT_MAX_RESIDENT",
      apply: "live",
    },
  }));
  mocks.getInferenceDiagnostics.mockResolvedValue({
    stateDir: "/var/lib/theygent",
    modelDir: "/models",
    hfHome: "/home/user/.cache/huggingface",
    persistent: true,
    binaries: [
      {
        engine: "llamacpp",
        modality: "chat",
        path: "/usr/local/bin/llama-server",
        status: "resolved",
      },
      { engine: "mlx", modality: "vision", path: null, status: "missing" },
    ],
  });
  mocks.getEngines.mockResolvedValue({
    maxResident: 2,
    resident: [
      {
        logicalId: "local-fast",
        engine: "llamacpp",
        model: "m",
        baseUrl: "http://localhost:9001",
        inflight: 1,
        draining: false,
        priority: 0,
      },
    ],
  });
  mocks.listModels.mockResolvedValue([]);
  mocks.listCredentials.mockResolvedValue([]);
  mocks.putCredential.mockResolvedValue({ name: "HF_TOKEN", hasValue: true });
  mocks.deleteCredential.mockResolvedValue(undefined);
  mocks.controlHealth.mockResolvedValue({
    reachable: true,
    status: "ready",
    reason: null,
    // The real /readyz wire shape: an ARRAY of server entries, not a name-keyed map.
    mcp: { servers: [{ name: "files", transport: "stdio", connected: false }] },
    engines: null,
  });
  mocks.inferenceHealth.mockResolvedValue({
    reachable: true,
    status: "ready",
    reason: null,
    mcp: null,
    engines: {},
  });
});

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("Settings page", () => {
  it("renders the six tabs with Overview active", async () => {
    renderPage();
    for (const name of ["Overview", "Inference", "Telemetry", "RAG", "MCP", "Credentials"]) {
      expect(screen.getByRole("tab", { name })).toBeInTheDocument();
    }
    // Overview content: the two plane cards + the boot block.
    await screen.findByText("Control plane");
    await screen.findByText("Inference plane");
    await screen.findByText("Boot configuration");
    // The MCP summary names the servers from /readyz's array-of-entries shape.
    await screen.findByText(/— files/);
  });

  it("boot warnings render loud; plain boot entries render as rows", async () => {
    renderPage();
    await screen.findByText(/stored secrets will not survive a restart/);
    expect(screen.getByText("THEYGENT_DURABLE")).toBeInTheDocument();
    expect(screen.getByText("Durable runtime mode.")).toBeInTheDocument();
  });

  it("endpoint override saves to localStorage and Reset clears it", async () => {
    renderPage();
    const input = await screen.findByLabelText("Control plane URL");
    fireEvent.change(input, { target: { value: "http://other:9999/" } });
    await userEvent.click(screen.getAllByRole("button", { name: "Apply" })[0]);
    expect(localStorage.getItem("theygent.url.control")).toBe("http://other:9999");

    await userEvent.click(screen.getAllByRole("button", { name: "Reset" })[0]);
    expect(localStorage.getItem("theygent.url.control")).toBeNull();
  });

  it("an env-pinned entry renders locked with a badge naming the env var", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "MCP" }));
    const input = await screen.findByLabelText("mcp.call_timeout_s");
    expect(input).toBeDisabled();
    expect(screen.getByText(/pinned by THEYGENT_MCP_CALL_TIMEOUT_S/)).toBeInTheDocument();
    // No reset offered for a pinned key even though the env owns a value.
    expect(
      screen.queryByRole("button", { name: "Reset mcp.call_timeout_s to default" }),
    ).not.toBeInTheDocument();
  });

  it("an env-capped entry stays editable, shows the cap, and makes effective vs stored visible", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Telemetry" }));
    const select = await screen.findByLabelText("telemetry.io_capture");
    expect(select).not.toBeDisabled();
    // The control edits the STORED value…
    expect(select).toHaveValue("full");
    // …while the badge + helper line surface the env cap and the effective value.
    expect(screen.getByText(/capped at metadata by THEYGENT_IO_CAPTURE/)).toBeInTheDocument();
    expect(screen.getByText(/Effective: metadata/)).toBeInTheDocument();
  });

  it("saving a tab PATCHes only the changed keys, with null for a staged reset", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Telemetry" }));
    const bytes = await screen.findByLabelText("telemetry.io_capture_max_bytes");
    fireEvent.change(bytes, { target: { value: "524288" } });

    // Reset the capped enum back to its default (stored_value exists, so reset is offered).
    await userEvent.click(
      screen.getByRole("button", { name: "Reset telemetry.io_capture to default" }),
    );

    await screen.findByText("2 unsaved changes");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() =>
      expect(mocks.patchSettings).toHaveBeenCalledWith({
        "telemetry.io_capture_max_bytes": 524288,
        "telemetry.io_capture": null,
      }),
    );
  });

  it("the sensitive headers editor sends a FULL replacement and never displays values", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Telemetry" }));

    // Read state: the existing name shows masked — no value anywhere.
    await screen.findByText("Authorization");
    expect(screen.getByText("••••••")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Replace headers" }));
    // Replacement is explicit: kept names re-seed with EMPTY password inputs.
    const value1 = screen.getByLabelText("Header 1 value");
    expect(value1).toHaveAttribute("type", "password");
    expect(value1).toHaveValue("");
    fireEvent.change(value1, { target: { value: "Bearer xyz" } });

    await userEvent.click(screen.getByRole("button", { name: "Add header" }));
    fireEvent.change(screen.getByLabelText("Header 2 name"), { target: { value: "X-Org" } });
    fireEvent.change(screen.getByLabelText("Header 2 value"), { target: { value: "42" } });

    // One staged key → the save bar reads "Save change" (singular).
    await userEvent.click(screen.getByRole("button", { name: "Save change" }));
    await waitFor(() =>
      expect(mocks.patchSettings).toHaveBeenCalledWith({
        "telemetry.otlp_headers": { Authorization: "Bearer xyz", "X-Org": "42" },
      }),
    );
    // The typed secret never appears as text content.
    expect(screen.queryByText("Bearer xyz")).not.toBeInTheDocument();
  });

  it("the collector test button uses the CURRENT unsaved endpoint and reports both outcomes", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Telemetry" }));

    const endpoint = await screen.findByLabelText("telemetry.otlp_endpoint");
    fireEvent.change(endpoint, { target: { value: "http://collector:4318" } });

    await userEvent.click(screen.getByRole("button", { name: "Test connection" }));
    await screen.findByText(/Collector reachable · 42 ms/);
    expect(mocks.testOtlp).toHaveBeenCalledWith({ endpoint: "http://collector:4318" });

    mocks.testOtlp.mockResolvedValueOnce({ ok: false, error: "connection refused", latency_ms: 0 });
    await userEvent.click(screen.getByRole("button", { name: "Test connection" }));
    await screen.findByText(/Collector test failed: connection refused/);
  });

  it("inference maxResident renders; env-pinned disables the control and names the env var", async () => {
    mocks.getInferenceSettings.mockResolvedValue({
      maxResident: {
        value: 4,
        default: 2,
        source: "env",
        envVar: "THEYGENT_MAX_RESIDENT",
        apply: "live",
      },
    });
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Inference" }));

    const input = await screen.findByLabelText("maxResident");
    expect(input).toBeDisabled();
    expect(input).toHaveValue(4);
    expect(screen.getByText(/pinned by THEYGENT_MAX_RESIDENT/)).toBeInTheDocument();

    // The rest of the tab still renders: warm engines + diagnostics with the missing binary loud.
    await screen.findByText("local-fast");
    await screen.findByText("missing");
  });

  it("saves and resets the inference ceiling when not pinned", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Inference" }));

    const input = await screen.findByLabelText("maxResident");
    fireEvent.change(input, { target: { value: "3" } });
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mocks.patchInferenceSettings).toHaveBeenCalledWith({ maxResident: 3 }),
    );

    // Once stored, the reset affordance appears and PATCHes null.
    await userEvent.click(await screen.findByRole("button", { name: "Reset to default" }));
    await waitFor(() =>
      expect(mocks.patchInferenceSettings).toHaveBeenCalledWith({ maxResident: null }),
    );
  });

  it("the HF token PUTs to the reserved credential name, write-only", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Inference" }));

    const token = await screen.findByPlaceholderText("hf_…");
    expect(token).toHaveAttribute("type", "password");
    fireEvent.change(token, { target: { value: "hf_secret" } });
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));

    await waitFor(() => expect(mocks.putCredential).toHaveBeenCalledWith("HF_TOKEN", "hf_secret"));
    expect(screen.queryByText("hf_secret")).not.toBeInTheDocument();
  });

  it("opening the headers editor stages nothing — saving unrelated edits leaves headers alone", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Telemetry" }));
    await userEvent.click(await screen.findByRole("button", { name: "Replace headers" }));

    // Just looking must not create a pending change (a staged blank map would wipe the values).
    expect(screen.queryByText(/unsaved change/)).not.toBeInTheDocument();

    const bytes = screen.getByLabelText("telemetry.io_capture_max_bytes");
    fireEvent.change(bytes, { target: { value: "524288" } });
    await screen.findByText("1 unsaved change");
    await userEvent.click(screen.getByRole("button", { name: "Save change" }));

    await waitFor(() =>
      expect(mocks.patchSettings).toHaveBeenCalledWith({
        "telemetry.io_capture_max_bytes": 524288,
      }),
    );
  });

  it("removing every header row stages a clear (null), never an empty map", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Telemetry" }));
    await userEvent.click(await screen.findByRole("button", { name: "Replace headers" }));
    await userEvent.click(screen.getByRole("button", { name: "Remove header 1" }));

    await screen.findByText("All headers will be removed on save.");
    await userEvent.click(screen.getByRole("button", { name: "Save change" }));
    await waitFor(() =>
      expect(mocks.patchSettings).toHaveBeenCalledWith({ "telemetry.otlp_headers": null }),
    );
  });

  it("a header row missing a name or value holds the save until fixed", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Telemetry" }));
    await userEvent.click(await screen.findByRole("button", { name: "Replace headers" }));

    fireEvent.change(screen.getByLabelText("Header 1 value"), { target: { value: "Bearer xyz" } });
    await userEvent.click(screen.getByRole("button", { name: "Add header" }));
    fireEvent.change(screen.getByLabelText("Header 2 name"), { target: { value: "X-Org" } });

    await screen.findByText("Every kept header needs a name and a value.");
    expect(screen.getByText(/1 invalid field/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save change" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Header 2 value"), { target: { value: "42" } });
    await waitFor(() =>
      expect(
        screen.queryByText("Every kept header needs a name and a value."),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Save change" })).toBeEnabled();
  });

  it("a successful save closes the headers editor — plaintext values never linger", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Telemetry" }));
    await userEvent.click(await screen.findByRole("button", { name: "Replace headers" }));
    fireEvent.change(screen.getByLabelText("Header 1 value"), { target: { value: "Bearer xyz" } });
    await userEvent.click(screen.getByRole("button", { name: "Save change" }));

    // Back to the masked read view: no password inputs, no replacement warning.
    await screen.findByRole("button", { name: "Replace headers" });
    expect(screen.queryByLabelText("Header 1 value")).not.toBeInTheDocument();
    expect(screen.queryByText(/re-enter the value/)).not.toBeInTheDocument();
  });

  it("undoing a staged clear while the editor was open returns to the read view", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Telemetry" }));
    await userEvent.click(await screen.findByRole("button", { name: "Replace headers" }));
    fireEvent.change(screen.getByLabelText("Header 1 value"), { target: { value: "Bearer xyz" } });

    // The row-level reset stages a clear over the open editor…
    await userEvent.click(
      screen.getByRole("button", { name: "Reset telemetry.otlp_headers to default" }),
    );
    await screen.findByText("All headers will be removed on save.");

    // …and undoing it lands on the read view, not the stale just-typed rows.
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    await screen.findByRole("button", { name: "Replace headers" });
    expect(screen.queryByLabelText("Header 1 value")).not.toBeInTheDocument();
  });

  it("an invalid number's blocker does not outlive a tab switch", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Telemetry" }));
    const bytes = await screen.findByLabelText("telemetry.io_capture_max_bytes");
    fireEvent.change(bytes, { target: { value: "" } });
    await screen.findByText(/1 invalid field/);

    await userEvent.click(screen.getByRole("tab", { name: "MCP" }));
    await userEvent.click(screen.getByRole("tab", { name: "Telemetry" }));

    // The remounted control re-seeds valid text — a stale blocker must not hold the save.
    expect(await screen.findByLabelText("telemetry.io_capture_max_bytes")).toHaveValue(262144);
    expect(screen.queryByText(/invalid field/)).not.toBeInTheDocument();
    expect(screen.queryByText(/A value is required/)).not.toBeInTheDocument();
  });

  it("a registry row needs id, label, AND url before the save unlocks", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "MCP" }));
    await userEvent.click(await screen.findByRole("button", { name: "Add registry" }));

    fireEvent.change(screen.getByLabelText("Registry 1 id"), { target: { value: "r1" } });
    fireEvent.change(screen.getByLabelText("Registry 1 url"), {
      target: { value: "https://registry.example.com" },
    });
    await screen.findByText("Every registry needs an id, a label, and an absolute https:// URL.");
    expect(screen.getByRole("button", { name: "Save change" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Registry 1 label"), { target: { value: "Example" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Save change" })).toBeEnabled());
  });

  it("clearing a stored text field stages a reset; clearing an unstored one un-dirties", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Telemetry" }));

    // Stored endpoint: emptying the input means reset — "" would be rejected by the server.
    const endpoint = await screen.findByLabelText("telemetry.otlp_endpoint");
    fireEvent.change(endpoint, { target: { value: "" } });
    await screen.findByText("resets to default on save");
    await userEvent.click(screen.getByRole("button", { name: "Save change" }));
    await waitFor(() =>
      expect(mocks.patchSettings).toHaveBeenCalledWith({ "telemetry.otlp_endpoint": null }),
    );

    // Nothing stored: emptying just un-stages (there is nothing to reset).
    await userEvent.click(screen.getByRole("tab", { name: "MCP" }));
    const redirect = await screen.findByLabelText("mcp.oauth_redirect_url");
    fireEvent.change(redirect, { target: { value: "http://elsewhere" } });
    await screen.findByText("1 unsaved change");
    fireEvent.change(redirect, { target: { value: "" } });
    await waitFor(() => expect(screen.queryByText(/unsaved change/)).not.toBeInTheDocument());
    // The field falls back to showing the effective (default) value.
    expect(redirect).toHaveValue("http://localhost:8080/mcp/oauth/callback");
  });

  it("boot values render objects as k=v pairs and lists comma-joined", async () => {
    renderPage();
    await screen.findByText("database");
    expect(screen.getByText("host=localhost:5432 · database=theygent")).toBeInTheDocument();
    expect(screen.getByText("http://localhost:5174, http://localhost:1420")).toBeInTheDocument();
  });

  it("the MiB input re-syncs when the underlying bytes value changes", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "RAG" }));
    const input = await screen.findByLabelText("rag.max_upload_bytes");
    expect(input).toHaveValue(50);

    fireEvent.change(input, { target: { value: "60" } });
    await screen.findByText("1 unsaved change");
    await userEvent.click(screen.getByRole("button", { name: "Save change" }));

    // The save echo is the server's canonical state — the text must follow it rather than keep
    // showing a stale MiB number next to a byte label that already moved.
    await waitFor(() => expect(input).toHaveValue(50));
  });

  it("a re-edit typed while a save is in flight survives the save", async () => {
    let resolvePatch!: (v: unknown) => void;
    mocks.patchSettings.mockImplementation(
      () =>
        new Promise((res) => {
          resolvePatch = res;
        }),
    );
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Telemetry" }));
    const bytes = await screen.findByLabelText("telemetry.io_capture_max_bytes");
    fireEvent.change(bytes, { target: { value: "524288" } });
    await userEvent.click(await screen.findByRole("button", { name: "Save change" }));

    // The request is in flight — the user keeps typing.
    fireEvent.change(bytes, { target: { value: "1048576" } });
    resolvePatch({ ...fixture(), apply_errors: {} });

    // The newer edit is NOT what was saved — it must stay staged, not snap back silently.
    await screen.findByText("1 unsaved change");
    expect(bytes).toHaveValue(1048576);
    await userEvent.click(screen.getByRole("button", { name: "Save change" }));
    await waitFor(() =>
      expect(mocks.patchSettings).toHaveBeenLastCalledWith({
        "telemetry.io_capture_max_bytes": 1048576,
      }),
    );
  });
});
