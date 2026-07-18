// Settings → Import / Export: the Export card's checkboxes drive the include list (All toggles
// everything; Traces auto-checks Runs), and the Import card previews a picked bundle — counts +
// the prominent warnings (HF weight downloads, secrets never exported) — BEFORE anything is
// written, then applies only through the shared ConfirmDialog and renders the merged report.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { strToU8, zipSync } from "fflate";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  exportControlBundle: vi.fn(),
  exportInferenceBundle: vi.fn(),
  downloadArtifact: vi.fn(),
  importControlBundle: vi.fn(),
  putArtifact: vi.fn(),
  importInferenceBundle: vi.fn(),
}));

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>();
  return { ...actual, api: { ...actual.api, ...mocks } };
});

import { ImportExportTab } from "../src/components/settings/ImportExportTab";

const downloads: string[] = [];
beforeAll(() => {
  Object.assign(URL, {
    createObjectURL: vi.fn(() => "blob:tab-test"),
    revokeObjectURL: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloads.push(this.download);
  });
});

function renderTab(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ImportExportTab />
    </QueryClientProvider>,
  );
}

// A minimal but warning-rich bundle zip, built the way the real exporter lays it out.
function bundleZipFile(): File {
  const control = {
    format_version: 1,
    exported_at: "2026-07-18T00:00:00Z",
    agents: [
      {
        id: "agent.echo",
        name: "Echo",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
        versions: [],
        io_policy: null,
        triggers: [],
      },
    ],
    connections: [
      {
        id: "conn_1",
        name: "github",
        kind: "http_auth",
        config: { auth: { type: "bearer" } },
        enabled: true,
        has_secret: true,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ],
  };
  const inference = {
    formatVersion: 1,
    models: [
      {
        logicalId: "local-fast",
        binding: { binding: "llamacpp", model: "/m/x.gguf", source: "local-path" },
        install: { repo: "org/model", variantId: "x.gguf" },
      },
      {
        logicalId: "hub-model",
        binding: { binding: "mlx", model: "org/mlx", source: "hf" },
        install: null,
      },
    ],
    credentialNames: ["HF_TOKEN"],
  };
  const zipped = zipSync({
    "manifest.json": strToU8(
      JSON.stringify({
        formatVersion: 1,
        exportedAt: "2026-07-18T00:00:00Z",
        parts: ["control", "inference"],
        artifacts: [],
        counts: {},
      }),
    ),
    "control-plane.json": strToU8(JSON.stringify(control)),
    "inference/models.json": strToU8(JSON.stringify(inference)),
  });
  return new File([zipped.slice()], "theygent-export-2026-07-18.zip", {
    type: "application/zip",
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  downloads.length = 0;
  mocks.exportControlBundle.mockResolvedValue({
    format_version: 1,
    exported_at: "2026-07-18T00:00:00Z",
    agents: [],
  });
  mocks.exportInferenceBundle.mockResolvedValue({
    formatVersion: 1,
    models: [],
    credentialNames: [],
  });
  mocks.importControlBundle.mockResolvedValue({
    agents: { created: 1 },
    connections: { created: 1, needs_secret: 1 },
    warnings: [{ code: "needs_secret", message: "connection conn_1 needs its secret re-entered" }],
  });
  mocks.importInferenceBundle.mockResolvedValue({
    registered: ["local-fast", "hub-model"],
    skipped: [],
    downloads: [],
    warnings: [],
    credentialNames: ["HF_TOKEN"],
  });
});

describe("Import / Export tab", () => {
  it("exports the full selection: control include list + the inference bundle, then downloads", async () => {
    renderTab();
    await userEvent.click(screen.getByRole("button", { name: "Export" }));
    await waitFor(() =>
      expect(mocks.exportControlBundle).toHaveBeenCalledWith([
        "agents",
        "runs",
        "traces",
        "sessions",
        "mcp",
        "rag",
      ]),
    );
    expect(mocks.exportInferenceBundle).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(downloads).toHaveLength(1));
    expect(downloads[0]).toMatch(/^theygent-export-\d{4}-\d{2}-\d{2}\.zip$/);
  });

  it("All unchecks everything; checking Traces auto-checks Runs", async () => {
    renderTab();
    await userEvent.click(screen.getByRole("checkbox", { name: "All" }));
    for (const name of ["Agents", "Runs", "Traces", "Model registry"]) {
      expect(screen.getByRole("checkbox", { name })).not.toBeChecked();
    }
    expect(screen.getByRole("button", { name: "Export" })).toBeDisabled();

    await userEvent.click(screen.getByRole("checkbox", { name: "Traces" }));
    expect(screen.getByRole("checkbox", { name: "Runs" })).toBeChecked();

    await userEvent.click(screen.getByRole("button", { name: "Export" }));
    await waitFor(() => expect(mocks.exportControlBundle).toHaveBeenCalledWith(["runs", "traces"]));
    expect(mocks.exportInferenceBundle).not.toHaveBeenCalled();
  });

  it("a picked bundle previews counts + the prominent warnings BEFORE anything is written", async () => {
    renderTab();
    fireEvent.change(screen.getByLabelText("Import file"), {
      target: { files: [bundleZipFile()] },
    });

    // The preview is explicit that nothing has happened yet. ("Connections" only exists in the
    // preview counts — the export card's labels never mention it.)
    await screen.findByText("Preview — nothing imported yet");
    expect(screen.getByText("Connections")).toBeInTheDocument();
    expect(screen.getByText("Model registrations")).toBeInTheDocument();
    // The two spec'd warnings, verbatim themes: HF weight downloads + secrets never exported.
    expect(
      screen.getByText(
        /2 models will be registered — TheYgent will download their weights from Hugging Face/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Secrets are never exported — 1 connection will need credentials re-entered/,
      ),
    ).toBeInTheDocument();
    // The inference plane's credential NAMES are surfaced by name before anything is written.
    expect(
      screen.getByText(/Credentials to re-enter on the inference plane: HF_TOKEN/),
    ).toBeInTheDocument();
    expect(mocks.importControlBundle).not.toHaveBeenCalled();
    expect(mocks.importInferenceBundle).not.toHaveBeenCalled();
  });

  it("applies only through the ConfirmDialog, then renders the merged report", async () => {
    renderTab();
    fireEvent.change(screen.getByLabelText("Import file"), {
      target: { files: [bundleZipFile()] },
    });
    await screen.findByText("Preview — nothing imported yet");

    await userEvent.click(screen.getByRole("button", { name: "Import…" }));
    expect(screen.getByText("Import this bundle?")).toBeInTheDocument();
    expect(mocks.importControlBundle).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Import" })); // the dialog's confirm
    await waitFor(() => expect(mocks.importControlBundle).toHaveBeenCalledTimes(1));
    // The bundle's formatVersion and credentialNames pass through VERBATIM — the server's version
    // fence and the credential echo both depend on it.
    expect(mocks.importInferenceBundle).toHaveBeenCalledWith({
      formatVersion: 1,
      models: [
        {
          logicalId: "local-fast",
          binding: { binding: "llamacpp", model: "/m/x.gguf", source: "local-path" },
          install: { repo: "org/model", variantId: "x.gguf" },
        },
        {
          logicalId: "hub-model",
          binding: { binding: "mlx", model: "org/mlx", source: "hf" },
          install: null,
        },
      ],
      credentialNames: ["HF_TOKEN"],
    });

    // The merged report: control counters, inference totals, credential names, flat warnings.
    await screen.findByText("Import report");
    expect(screen.getByText(/created 1 · needs_secret 1/)).toBeInTheDocument();
    expect(screen.getByText(/Model registry: 2 registered · 0 skipped/)).toBeInTheDocument();
    expect(screen.getByText(/Credentials to re-enter on the inference plane/)).toBeInTheDocument();
    expect(screen.getByText("HF_TOKEN")).toBeInTheDocument();
    expect(screen.getByText(/connection conn_1 needs its secret re-entered/)).toBeInTheDocument();
  });

  it("a dead inference plane is a warning in the report — the control half still renders", async () => {
    mocks.importInferenceBundle.mockRejectedValue(new TypeError("fetch failed"));
    renderTab();
    fireEvent.change(screen.getByLabelText("Import file"), {
      target: { files: [bundleZipFile()] },
    });
    await screen.findByText("Preview — nothing imported yet");
    await userEvent.click(screen.getByRole("button", { name: "Import…" }));
    await userEvent.click(screen.getByRole("button", { name: "Import" })); // the dialog's confirm

    // Partial success: the control counters render and the failed plane is a NAMED warning.
    await screen.findByText("Import report");
    expect(screen.getByText(/created 1 · needs_secret 1/)).toBeInTheDocument();
    expect(screen.getByText("inference_import_failed")).toBeInTheDocument();
    expect(screen.getByText(/inference plane: fetch failed/)).toBeInTheDocument();
    expect(screen.queryByText(/Model registry:/)).not.toBeInTheDocument();
  });

  it("cancelling the preview discards it without writing", async () => {
    renderTab();
    fireEvent.change(screen.getByLabelText("Import file"), {
      target: { files: [bundleZipFile()] },
    });
    await screen.findByText("Preview — nothing imported yet");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText("Preview — nothing imported yet")).not.toBeInTheDocument();
    expect(mocks.importControlBundle).not.toHaveBeenCalled();
  });
});
