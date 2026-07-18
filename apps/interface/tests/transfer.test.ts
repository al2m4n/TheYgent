// The transfer module: the zip round-trips in memory (build → parse gives back the same halves),
// a bare exported agent .json wraps into an agents-only control bundle, the preview warnings
// derive from what the bundle actually says, and apply sequences control → artifacts → inference
// with one merged report + query invalidation.

import { QueryClient } from "@tanstack/react-query";
import { strToU8, zipSync } from "fflate";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { ControlBundle, InferenceBundle } from "../src/lib/api";

const mocks = vi.hoisted(() => ({
  exportControlBundle: vi.fn(),
  exportInferenceBundle: vi.fn(),
  downloadArtifact: vi.fn(),
  importControlBundle: vi.fn(),
  putArtifact: vi.fn(),
  importInferenceBundle: vi.fn(),
  getAgent: vi.fn(),
  getAgentVersion: vi.fn(),
}));

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>();
  return { ...actual, api: { ...actual.api, ...mocks } };
});

import { ApiError } from "../src/lib/api";
import {
  applyImport,
  buildAgentsExportZip,
  buildExportZip,
  parseImportFile,
} from "../src/lib/transfer";

// jsdom implements neither URL.createObjectURL nor a navigating anchor click — stub both so the
// download side of an export is observable instead of crashing.
const downloads: string[] = [];
beforeAll(() => {
  Object.assign(URL, {
    createObjectURL: vi.fn(() => "blob:transfer-test"),
    revokeObjectURL: vi.fn(),
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloads.push(this.download);
  });
});

beforeEach(() => {
  downloads.length = 0;
});

afterEach(() => {
  vi.clearAllMocks();
});

const IR = {
  schemaVersion: "1.0",
  id: "agent.echo",
  name: "Echo",
  version: "0.1.0",
  contentHash: "abc123",
  models: {},
  tools: {},
  nodes: [],
  edges: [],
};

function controlFixture(): ControlBundle {
  return {
    format_version: 1,
    exported_at: "2026-07-18T00:00:00Z",
    agents: [
      {
        id: "agent.echo",
        name: "Echo",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-02T00:00:00Z",
        versions: [
          {
            version: "0.1.0",
            content_hash: "abc123",
            created_at: "2026-01-01T00:00:00Z",
            ir: IR,
            view: null,
          },
        ],
        io_policy: null,
        triggers: [],
      },
    ],
    runs: [{ id: "run_1" }, { id: "run_2" }],
    artifact_refs: [
      { ref: "art_kept", content_type: "audio/wav" },
      { ref: "art_gone", content_type: "text/plain" },
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
      {
        id: "conn_2",
        name: "notion",
        kind: "mcp_server",
        config: { auth: { type: "oauth" } },
        enabled: true,
        has_secret: false,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ],
    mcp_servers: [
      {
        name: "files",
        transport: "stdio",
        command: "mcp-files",
        args: [],
        env_keys: ["API_KEY"],
        cwd: null,
        url: null,
        header_keys: [],
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ],
    rag_sources: [
      {
        id: "rag_1",
        name: "docs",
        kind: "crawl",
        embedding_model: "embed-small",
        config: {},
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
      {
        id: "rag_2",
        name: "papers",
        kind: "upload",
        embedding_model: "embed-small",
        config: {},
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ],
  };
}

function inferenceFixture(): InferenceBundle {
  return {
    formatVersion: 1,
    models: [
      {
        logicalId: "local-fast",
        binding: { binding: "llamacpp", model: "/models/x.gguf", source: "local-path" },
        install: { repo: "org/model", variantId: "x.gguf" },
      },
      {
        logicalId: "hub-model",
        binding: { binding: "mlx", model: "org/mlx-model", source: "hf" },
        install: null,
      },
      {
        logicalId: "orphan",
        binding: { binding: "vllm", model: "/mnt/w", source: "local-path" },
        install: null,
      },
    ],
    credentialNames: ["HF_TOKEN"],
  };
}

describe("buildExportZip → parseImportFile round trip", () => {
  it("zips both halves + artifact bytes and parses them back identically", async () => {
    mocks.exportControlBundle.mockResolvedValue(controlFixture());
    mocks.exportInferenceBundle.mockResolvedValue(inferenceFixture());
    mocks.downloadArtifact.mockImplementation(async (ref: string) => {
      if (ref === "art_gone") throw new ApiError("not found", 404, "artifact_not_found");
      return new Blob(["wav-bytes"], { type: "audio/wav" });
    });

    const result = await buildExportZip([
      "agents",
      "runs",
      "traces",
      "sessions",
      "mcp",
      "rag",
      "registries",
    ]);

    // The control include list maps 1:1 (registries never crosses the control plane).
    expect(mocks.exportControlBundle).toHaveBeenCalledWith([
      "agents",
      "runs",
      "traces",
      "sessions",
      "mcp",
      "rag",
    ]);
    expect(mocks.exportInferenceBundle).toHaveBeenCalledTimes(1);
    // The missing artifact is a note, not a failure.
    expect(result.notes).toEqual(["Artifact art_gone no longer exists — skipped."]);
    expect(result.filename).toMatch(/^theygent-export-\d{4}-\d{2}-\d{2}\.zip$/);
    expect(downloads).toEqual([result.filename]);
    expect(result.counts).toMatchObject({ agents: 1, runs: 2, models: 3 });

    const preview = await parseImportFile(new File([result.blob], result.filename));
    expect(preview.source).toBe("zip");
    expect(preview.control).toEqual(controlFixture());
    expect(preview.inference).toEqual(inferenceFixture());
    expect(preview.artifacts).toHaveLength(1);
    expect(preview.artifacts[0].ref).toBe("art_kept");
    // The manifest carries the mime a raw zip entry cannot.
    expect(preview.artifacts[0].contentType).toBe("audio/wav");
    expect(new TextDecoder().decode(preview.artifacts[0].bytes)).toBe("wav-bytes");
    expect(preview.counts).toMatchObject({ agents: 1, runs: 2, connections: 2, models: 3 });
  });

  it("derives the preview warnings from what the bundle says", async () => {
    mocks.exportControlBundle.mockResolvedValue(controlFixture());
    mocks.exportInferenceBundle.mockResolvedValue(inferenceFixture());
    mocks.downloadArtifact.mockResolvedValue(new Blob(["x"]));
    const { blob, filename } = await buildExportZip(["agents", "mcp", "rag", "registries"]);

    const preview = await parseImportFile(new File([blob], filename));
    expect(preview.warnings).toEqual({
      // the catalog install + the hf-source binding — never the pathless local-path one
      modelDownloads: 2,
      weightsUnavailable: 1,
      needsSecret: 1,
      needsOauth: 1,
      needsEnv: 1,
      needsIngest: 1,
      needsUpload: 1,
      // the source plane's credential NAMES, verbatim — the preview must name them
      credentialNames: ["HF_TOKEN"],
    });
  });

  it("refuses an empty selection", async () => {
    await expect(buildExportZip([])).rejects.toThrow(/nothing selected/);
  });

  it("refuses a zip with no bundle inside", async () => {
    const stray = zipSync({ "readme.txt": strToU8("hi") });
    await expect(parseImportFile(new File([stray.slice()], "stray.zip"))).rejects.toThrow(
      /no theygent bundle/,
    );
  });
});

describe("bare agent .json wrapping", () => {
  it("wraps an exported agent document into an agents-only bundle, view split back out", async () => {
    const doc = { ...IR, view: { nodes: { n1: { position: { x: 1, y: 2 } } } } };
    const file = new File([JSON.stringify(doc)], "theygent-agent-agent.echo-v0.1.0.json", {
      type: "application/json",
    });
    const preview = await parseImportFile(file);
    expect(preview.source).toBe("json");
    expect(preview.inference).toBeNull();
    const agents = preview.control?.agents ?? [];
    expect(agents).toHaveLength(1);
    expect(agents[0].id).toBe("agent.echo");
    expect(agents[0].versions).toHaveLength(1);
    const v = agents[0].versions[0];
    expect(v.version).toBe("0.1.0");
    expect(v.content_hash).toBe("abc123");
    // The embedded layout moves back to the separate view block — the ir travels view-stripped.
    expect(v.ir).not.toHaveProperty("view");
    expect(v.view).toEqual({ nodes: { n1: { position: { x: 1, y: 2 } } } });
    expect(preview.counts).toEqual({ agents: 1 });
  });

  it("rejects a .json that is not an agent document", async () => {
    const file = new File([JSON.stringify({ hello: "world" })], "notes.json");
    await expect(parseImportFile(file)).rejects.toThrow(/missing id, version, or schemaVersion/);
  });

  it("rejects invalid JSON loudly", async () => {
    const file = new File(["{nope"], "broken.json");
    await expect(parseImportFile(file)).rejects.toThrow(/not valid JSON/);
  });
});

describe("applyImport", () => {
  it("posts control, restores artifacts, posts inference; merges the report and invalidates", async () => {
    mocks.importControlBundle.mockResolvedValue({
      agents: { created: 1 },
      warnings: [{ code: "needs_secret", message: "conn_1" }],
    });
    mocks.putArtifact.mockResolvedValue({ ref: "art_kept", contentType: "audio/wav", bytes: 9 });
    mocks.importInferenceBundle.mockResolvedValue({
      registered: ["local-fast", "hub-model"],
      skipped: ["orphan"],
      downloads: [{ jobId: "job_1", logicalId: "local-fast", repo: "org/model" }],
      warnings: [{ logicalId: "orphan", code: "weights_unavailable", message: "path missing" }],
      credentialNames: ["HF_TOKEN"],
    });

    const control = controlFixture();
    const inference = inferenceFixture();
    const qc = new QueryClient();
    const invalidate = vi.spyOn(qc, "invalidateQueries");

    const report = await applyImport(
      {
        source: "zip",
        control,
        inference,
        artifacts: [{ ref: "art_kept", contentType: "audio/wav", bytes: strToU8("wav-bytes") }],
        counts: {},
        warnings: {
          modelDownloads: 2,
          weightsUnavailable: 1,
          needsSecret: 1,
          needsOauth: 1,
          needsEnv: 1,
          needsIngest: 1,
          needsUpload: 1,
          credentialNames: ["HF_TOKEN"],
        },
      },
      qc,
    );

    expect(mocks.importControlBundle).toHaveBeenCalledWith(control);
    expect(mocks.putArtifact).toHaveBeenCalledTimes(1);
    const [ref, blob, contentType] = mocks.putArtifact.mock.calls[0];
    expect(ref).toBe("art_kept");
    expect(blob).toBeInstanceOf(Blob);
    expect(contentType).toBe("audio/wav");
    // The inference half posts VERBATIM — formatVersion keeps the server's version fence engaged,
    // credentialNames ride through so the result can echo them back.
    expect(mocks.importInferenceBundle).toHaveBeenCalledWith({
      formatVersion: 1,
      models: inference.models,
      credentialNames: ["HF_TOKEN"],
    });

    expect(report.artifactsRestored).toBe(1);
    expect(report.downloads).toEqual([
      { jobId: "job_1", logicalId: "local-fast", repo: "org/model" },
    ]);
    // Both planes' warnings land in ONE flat list.
    expect(report.warnings.map((w) => w.code)).toEqual(["needs_secret", "weights_unavailable"]);

    const keys = invalidate.mock.calls.map(([f]) => (f?.queryKey as string[])[0]);
    for (const expected of ["agents", "runs", "sessions", "connections", "models"]) {
      expect(keys).toContain(expected);
    }
  });

  it("a failed artifact restore becomes a warning, not an abort", async () => {
    mocks.importControlBundle.mockResolvedValue({ warnings: [] });
    mocks.putArtifact.mockRejectedValue(new ApiError("bad ref", 400, "invalid_artifact_ref"));
    const qc = new QueryClient();

    const report = await applyImport(
      {
        source: "zip",
        control: { format_version: 1, exported_at: "2026-07-18T00:00:00Z" },
        inference: null,
        artifacts: [{ ref: "art_x", contentType: "text/plain", bytes: strToU8("x") }],
        counts: {},
        warnings: {
          modelDownloads: 0,
          weightsUnavailable: 0,
          needsSecret: 0,
          needsOauth: 0,
          needsEnv: 0,
          needsIngest: 0,
          needsUpload: 0,
          credentialNames: [],
        },
      },
      qc,
    );
    expect(report.artifactsRestored).toBe(0);
    expect(report.warnings).toEqual([
      { code: "artifact_restore_failed", message: "artifact art_x: bad ref" },
    ]);
  });

  it("a failed inference import becomes a warning — the control half's report survives", async () => {
    mocks.importControlBundle.mockResolvedValue({
      agents: { created: 1 },
      warnings: [],
    });
    mocks.putArtifact.mockResolvedValue({ ref: "art_kept", contentType: "audio/wav", bytes: 9 });
    // The machine-migration case: the inference plane is down (or its URL override is stale).
    mocks.importInferenceBundle.mockRejectedValue(new TypeError("fetch failed"));

    const qc = new QueryClient();
    const invalidate = vi.spyOn(qc, "invalidateQueries");

    const report = await applyImport(
      {
        source: "zip",
        control: controlFixture(),
        inference: inferenceFixture(),
        artifacts: [{ ref: "art_kept", contentType: "audio/wav", bytes: strToU8("wav-bytes") }],
        counts: {},
        warnings: {
          modelDownloads: 2,
          weightsUnavailable: 1,
          needsSecret: 1,
          needsOauth: 1,
          needsEnv: 1,
          needsIngest: 1,
          needsUpload: 1,
          credentialNames: ["HF_TOKEN"],
        },
      },
      qc,
    );

    // The already-applied control half is reported, not discarded.
    expect(report.control).toEqual({ agents: { created: 1 }, warnings: [] });
    expect(report.artifactsRestored).toBe(1);
    expect(report.inference).toBeNull();
    expect(report.downloads).toEqual([]);
    // The dead plane is a NAMED warning in the merged report, not a thrown total failure.
    expect(report.warnings).toEqual([
      { code: "inference_import_failed", message: "inference plane: fetch failed" },
    ]);
    // The lists the control import grew still refresh.
    const keys = invalidate.mock.calls.map(([f]) => (f?.queryKey as string[])[0]);
    for (const expected of ["agents", "runs", "sessions", "connections", "models"]) {
      expect(keys).toContain(expected);
    }
  });
});

describe("buildAgentsExportZip", () => {
  it("bundles every version oldest-first as an agents-only zip", async () => {
    mocks.getAgent.mockResolvedValue({
      id: "agent.echo",
      name: "Echo",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-02T00:00:00Z",
      versions: [
        // newest first, as the registry returns them
        { version: "0.2.0", content_hash: "h2", seq: 2, created_at: "2026-01-02T00:00:00Z" },
        { version: "0.1.0", content_hash: "h1", seq: 1, created_at: "2026-01-01T00:00:00Z" },
      ],
    });
    mocks.getAgentVersion.mockImplementation(async (_id: string, version: string) => ({
      agent_id: "agent.echo",
      version,
      content_hash: version === "0.1.0" ? "h1" : "h2",
      seq: version === "0.1.0" ? 1 : 2,
      created_at: "2026-01-01T00:00:00Z",
      ir: { ...IR, version },
      view: null,
    }));

    const result = await buildAgentsExportZip(["agent.echo"]);
    expect(result.counts).toEqual({ agents: 1 });
    expect(downloads).toEqual([result.filename]);

    const preview = await parseImportFile(new File([result.blob], result.filename));
    const agent = preview.control?.agents?.[0];
    expect(agent?.id).toBe("agent.echo");
    expect(agent?.versions.map((v) => v.version)).toEqual(["0.1.0", "0.2.0"]);
    expect(preview.inference).toBeNull();
  });
});
