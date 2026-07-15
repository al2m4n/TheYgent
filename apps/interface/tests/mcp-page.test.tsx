// Tests for the MCP page (RTL over a mocked control plane): the unified server list (name-keyed
// definitions merged with mcp_server connections), the hub browser (cursor pagination + the
// mechanical install form), the add-server connection forms, and the interactive authorization
// flow. The control plane is mocked at `fetch`, so this is pure FE wiring — the backend's own
// tests cover the registry client, install planning, and the token machinery.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { notify } from "../src/lib/notify";
import { Mcp } from "../src/routes/Mcp";

// ── fixtures ──────────────────────────────────────────────────────────────────────────────────────

const SERVER = { name: "files", transport: "stdio", connected: true, url: null, headerKeys: [] };

const CONN_BEARER = {
  id: "con_1",
  name: "github",
  kind: "mcp_server",
  config: {
    transport: "http",
    url: "https://gh.example/mcp",
    headers: {},
    auth: { type: "bearer" },
    origin: { registry: "official", name: "io.github.acme/github", version: "1.2.0" },
  },
  hasSecret: true,
  enabled: true,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

// An http_auth connection must NOT appear in the MCP server list (it is REST-tool auth).
const CONN_HTTP_AUTH = {
  id: "con_h",
  name: "crm-key",
  kind: "http_auth",
  config: {},
  hasSecret: true,
  enabled: true,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

const CONN_OAUTH = {
  id: "con_2",
  name: "linear",
  kind: "mcp_server",
  config: { transport: "http", url: "https://linear.example/mcp", auth: { type: "oauth" } },
  hasSecret: false,
  enabled: true,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

// A generated (OpenAPI) connection. The connection dump ELIDES the full parsed spec to a summary,
// so the settings modal must never send config back (that would overwrite the real spec server-side
// and wipe the derived tools).
const CONN_OPENAPI = {
  id: "con_3",
  name: "petstore",
  kind: "mcp_server",
  config: {
    transport: "openapi",
    url: "https://petstore.example/openapi.json",
    spec: { title: "Petstore", version: "1.0.0", pathCount: 12 },
    auth: { type: "bearer" },
  },
  hasSecret: true,
  enabled: true,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

const CONN_STDIO = {
  id: "con_4",
  name: "fs-conn",
  kind: "mcp_server",
  config: { transport: "stdio", command: "npx", args: ["-y", "@mcp/fs", "/tmp"], env: {} },
  hasSecret: false,
  enabled: true,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
};

const ENTRY_A = {
  registry: "official",
  name: "io.github.acme/alpha",
  title: "Alpha",
  description: "the first hub server",
  version: "1.0.0",
  status: "active",
  isLatest: true,
  updatedAt: "2026-06-01T00:00:00+00:00",
  repositoryUrl: "https://github.example/acme/alpha",
  websiteUrl: null,
  stars: 12,
  transports: ["stdio"],
  packageTypes: ["npm"],
  deprecationMessage: null,
  installed: false,
  installedAs: null,
  installedConnection: null,
};

const ENTRY_B = {
  ...ENTRY_A,
  name: "io.github.acme/beta",
  title: "Beta",
  description: "the second hub server",
};

const CANDIDATE = {
  id: "pkg-npm",
  kind: "stdio",
  label: "npx @acme/alpha",
  inputs: [
    {
      name: "API_KEY",
      description: "the service key",
      required: true,
      secret: true,
      default: null,
      choices: null,
      placeholder: "sk-…",
      target: "env",
    },
  ],
  supportsOauth: false,
  warnings: [],
  command: "npx",
  args: ["-y", "@acme/alpha"],
  url: null,
};

// ── the fetch mock ────────────────────────────────────────────────────────────────────────────────

function jsonResponse(data: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => data } as unknown as Response;
}

// Route a stubbed request by its API path, HERMETICALLY (tolerate a relative dev-proxy base and
// strip its prefix), exactly like the other suites.
function pathOf(url: string): string {
  return new URL(url, "http://localhost").pathname.replace(/^\/__[a-z]+/, "");
}

interface Recorded {
  url: string;
  method: string;
  body?: unknown;
}

// Stub `fetch` with per-test routes over a base that answers the page's two list queries.
// Unrouted paths get an empty object so incidental queries never crash a test.
function mockRoutes(
  routes: (path: string, method: string, url: string, body: unknown) => Response | undefined,
  base: { servers?: unknown[]; connections?: unknown[] } = {},
): Recorded[] {
  const calls: Recorded[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ url, method, body });
    const path = pathOf(url);
    const custom = routes(path, method, url, body);
    if (custom) return custom;
    if (path === "/admin/mcp/servers") return jsonResponse({ servers: base.servers ?? [] });
    if (path === "/connections" && method === "GET") {
      return jsonResponse({ connections: base.connections ?? [] });
    }
    return jsonResponse({});
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

const noRoutes = () => undefined;

function renderMcp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Mcp />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  notify.dismiss(); // sonner state is module-global — clear before the next test
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ── the unified server list ───────────────────────────────────────────────────────────────────────

describe("MCP page — unified server list", () => {
  it("merges name-keyed definitions and mcp_server connections with their badges", async () => {
    mockRoutes(noRoutes, {
      servers: [SERVER],
      connections: [CONN_BEARER, CONN_HTTP_AUTH],
    });
    renderMcp();

    // Both sources list; the http_auth connection (REST-tool auth, not a server) does not.
    await screen.findByText("files");
    expect(screen.getByText("github")).toBeInTheDocument();
    expect(screen.queryByText("crm-key")).not.toBeInTheDocument();

    // Transport badges from both shapes (facet chips repeat the labels — presence is enough).
    expect(screen.getAllByText("stdio").length).toBeGreaterThan(0);
    expect(screen.getAllByText("http").length).toBeGreaterThan(0);

    // Source badges, the hub-origin badge (registry id + version), and the auth chip.
    expect(screen.getByText("defined")).toBeInTheDocument();
    expect(screen.getByText("connection")).toBeInTheDocument();
    expect(screen.getByText("official · v1.2.0")).toBeInTheDocument();
    expect(screen.getByText("bearer")).toBeInTheDocument();

    // Liveness chips: the registered server is connected; the connection starts idle.
    expect(screen.getAllByText("connected").length).toBeGreaterThan(0);
    expect(screen.getAllByText("idle").length).toBeGreaterThan(0);

    // The encrypted-secret lock marks the connection row.
    expect(screen.getByLabelText("has secret")).toBeInTheDocument();
  });

  it("lists a connection's tools through the connection tools endpoint", async () => {
    const calls = mockRoutes(
      (path, method) => {
        if (path === "/connections/con_1/mcp/tools" && method === "GET") {
          return jsonResponse({
            tools: [{ name: "create_issue", description: "open an issue", inputSchema: {} }],
          });
        }
        return undefined;
      },
      { servers: [], connections: [CONN_BEARER] },
    );
    renderMcp();

    await screen.findByText("github");
    fireEvent.click(screen.getByRole("button", { name: "Tools" }));
    await screen.findByText("create_issue");
    expect(screen.getByText(/open an issue/)).toBeInTheDocument();
    expect(calls.some((c) => pathOf(c.url) === "/connections/con_1/mcp/tools")).toBe(true);
  });
});

// ── the hub browser ───────────────────────────────────────────────────────────────────────────────

describe("MCP page — browse hubs", () => {
  const hubRoutes = (path: string, _method: string, url: string): Response | undefined => {
    if (path === "/admin/mcp/registries") {
      return jsonResponse({
        registries: [{ id: "official", label: "Official", url: "https://registry.example" }],
      });
    }
    if (path === "/admin/mcp/catalog") {
      const cursor = new URL(url, "http://localhost").searchParams.get("cursor");
      return jsonResponse(
        cursor === "c2"
          ? { entries: [ENTRY_B], nextCursor: null }
          : { entries: [ENTRY_A], nextCursor: "c2" },
      );
    }
    if (path === "/admin/mcp/catalog/entry") {
      return jsonResponse({ entry: ENTRY_A, candidates: [CANDIDATE] });
    }
    return undefined;
  };

  it("lists catalog entries and paginates by appending pages via Load more", async () => {
    mockRoutes(hubRoutes);
    renderMcp();

    fireEvent.click(screen.getByRole("button", { name: /Browse hubs/ }));
    await screen.findByText("Browse MCP hubs");

    // The first page renders with the card metadata.
    await screen.findByText("Alpha");
    expect(screen.getByText("io.github.acme/alpha")).toBeInTheDocument();
    expect(screen.getByText("v1.0.0")).toBeInTheDocument();
    expect(screen.getByText("npm")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument(); // stars

    // Load more APPENDS the next page (Alpha stays), then disappears at the cursor's end.
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("Beta");
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument(),
    );
  });

  it("renders the install form mechanically from the candidate and posts the install body", async () => {
    const calls = mockRoutes((path, method, url, body) => {
      if (path === "/admin/mcp/catalog/install" && method === "POST") {
        const b = body as { connectionName: string };
        return jsonResponse(
          {
            id: "con_9",
            name: b.connectionName,
            kind: "mcp_server",
            config: { transport: "stdio", origin: { registry: "official" } },
            hasSecret: true,
            enabled: true,
            created_at: "2026-01-01T00:00:00+00:00",
            updated_at: "2026-01-01T00:00:00+00:00",
          },
          201,
        );
      }
      return hubRoutes(path, method, url);
    });
    renderMcp();

    fireEvent.click(screen.getByRole("button", { name: /Browse hubs/ }));
    await screen.findByText("Alpha");
    fireEvent.click(screen.getByRole("button", { name: /Alpha/ })); // expand the card
    await screen.findByText("npx @acme/alpha"); // the candidate row
    fireEvent.click(screen.getByRole("button", { name: "Install" }));
    await screen.findByText("Install Alpha"); // the dialog

    // The form is derived from the candidate's declared inputs: a secret input renders as a
    // password field, with its description and placeholder.
    const key = screen.getByPlaceholderText("sk-…");
    expect(key).toHaveAttribute("type", "password");
    expect(screen.getByText("the service key")).toBeInTheDocument();
    // The suggested connection name is the slugified last path segment of the entry name.
    expect(screen.getByDisplayValue("alpha")).toBeInTheDocument();

    fireEvent.change(key, { target: { value: "sk-test" } });
    const dialogs = screen.getAllByRole("dialog");
    fireEvent.click(within(dialogs[dialogs.length - 1]).getByRole("button", { name: "Install" }));

    await waitFor(() => {
      const post = calls.find(
        (c) => c.method === "POST" && pathOf(c.url) === "/admin/mcp/catalog/install",
      );
      expect(post?.body).toEqual({
        registry: "official",
        name: "io.github.acme/alpha",
        version: "1.0.0",
        candidateId: "pkg-npm",
        connectionName: "alpha",
        values: { API_KEY: "sk-test" },
        useOauth: false,
      });
    });
  });
});

// ── the add-server forms ──────────────────────────────────────────────────────────────────────────

describe("MCP page — add server", () => {
  it("Remote form composes a bearer-auth mcp_server connection (secret write-only)", async () => {
    const calls = mockRoutes((path, method) => {
      if (path === "/connections" && method === "POST") {
        return jsonResponse(
          {
            id: "con_9",
            name: "gh",
            kind: "mcp_server",
            config: { transport: "http" },
            hasSecret: true,
            enabled: true,
            created_at: "2026-01-01T00:00:00+00:00",
            updated_at: "2026-01-01T00:00:00+00:00",
          },
          201,
        );
      }
      return undefined;
    });
    renderMcp();

    fireEvent.click(screen.getByRole("button", { name: /Add server/ }));
    await screen.findByText("Add an MCP server");
    fireEvent.click(screen.getByRole("button", { name: "Remote" }));

    fireEvent.change(screen.getByPlaceholderText("github"), { target: { value: "gh" } });
    fireEvent.change(screen.getByPlaceholderText("https://host/mcp"), {
      target: { value: "https://gh.example/mcp" },
    });
    fireEvent.change(screen.getByLabelText("Auth"), { target: { value: "bearer" } });
    const token = await screen.findByPlaceholderText("paste the token…");
    expect(token).toHaveAttribute("type", "password");
    fireEvent.change(token, { target: { value: "tok-123" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && pathOf(c.url) === "/connections");
      expect(post?.body).toEqual({
        name: "gh",
        kind: "mcp_server",
        config: {
          transport: "http",
          url: "https://gh.example/mcp",
          headers: {},
          auth: { type: "bearer" },
        },
        secret: "tok-123",
      });
    });
  });

  it("Stdio form routes secret env lines to the write-only secret as one JSON map", async () => {
    const calls = mockRoutes((path, method) => {
      if (path === "/connections" && method === "POST") {
        return jsonResponse(
          {
            id: "con_5",
            name: "fs",
            kind: "mcp_server",
            config: { transport: "stdio" },
            hasSecret: true,
            enabled: true,
            created_at: "2026-01-01T00:00:00+00:00",
            updated_at: "2026-01-01T00:00:00+00:00",
          },
          201,
        );
      }
      return undefined;
    });
    renderMcp();

    fireEvent.click(screen.getByRole("button", { name: /Add server/ }));
    await screen.findByText("Add an MCP server");
    // Stdio is the default pane.
    fireEvent.change(screen.getByPlaceholderText("filesystem"), { target: { value: "fs" } });
    fireEvent.change(screen.getByPlaceholderText("npx"), { target: { value: "npx" } });
    fireEvent.change(screen.getByPlaceholderText(/server-filesystem/), {
      target: { value: "-y @modelcontextprotocol/server-filesystem /tmp" },
    });
    fireEvent.change(screen.getByPlaceholderText("API_KEY=…"), {
      target: { value: "API_KEY=sk-9" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && pathOf(c.url) === "/connections");
      expect(post?.body).toEqual({
        name: "fs",
        kind: "mcp_server",
        config: {
          transport: "stdio",
          command: "npx",
          args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
          env: {},
          auth: { type: "env" },
        },
        secret: JSON.stringify({ API_KEY: "sk-9" }),
      });
    });
  });
});

// ── the interactive authorization flow ────────────────────────────────────────────────────────────

describe("MCP page — oauth connect", () => {
  it("Connect calls :start and opens the authorization URL in a new tab", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const calls = mockRoutes(
      (path, method) => {
        if (path === "/connections/con_2/mcp-oauth" && method === "GET") {
          return jsonResponse({
            authorized: false,
            pending: false,
            lastError: null,
            connected: false,
          });
        }
        if (path === "/connections/con_2/mcp-oauth:start" && method === "POST") {
          return jsonResponse({
            status: "pending",
            authorizationUrl: "https://provider.example/authorize",
          });
        }
        return undefined;
      },
      { servers: [], connections: [CONN_OAUTH] },
    );
    renderMcp();

    await screen.findByText("linear");
    // The unauthorized state is visible before the user acts.
    await screen.findByText("needs auth");
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    await waitFor(() =>
      expect(open).toHaveBeenCalledWith("https://provider.example/authorize", "_blank"),
    );
    expect(
      calls.some(
        (c) => c.method === "POST" && pathOf(c.url) === "/connections/con_2/mcp-oauth:start",
      ),
    ).toBe(true);
  });
});

// ── secret rotation ───────────────────────────────────────────────────────────────────────────────

describe("MCP page — secret rotation", () => {
  it("rotates a connection secret from the settings modal; oauth rows offer no rotate field", async () => {
    const calls = mockRoutes(
      (path, method) => {
        if (method === "PATCH" && path === "/connections/con_1") {
          return jsonResponse({ ...CONN_BEARER });
        }
        return undefined;
      },
      { servers: [], connections: [CONN_BEARER, CONN_OAUTH] },
    );
    renderMcp();

    await screen.findByText("github");
    await screen.findByText("linear");

    // Open the OAuth row's settings: its grant is broker-managed (re-authorize with Connect), so the
    // settings modal must NOT invite a hand-written secret.
    fireEvent.click(screen.getByText("linear"));
    await screen.findByText("linear — settings");
    expect(screen.queryByPlaceholderText("write-only — never shown again")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    // The bearer connection can rotate its secret from its settings modal; the write rides the
    // connection PATCH alongside the (unchanged) config.
    fireEvent.click(screen.getByText("github"));
    await screen.findByText("github — settings");
    const input = await screen.findByPlaceholderText("write-only — never shown again");
    expect(input).toHaveAttribute("type", "password");
    fireEvent.change(input, { target: { value: "new-token" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      const patch = calls.find(
        (c) => c.method === "PATCH" && pathOf(c.url) === "/connections/con_1",
      );
      expect(patch?.body).toMatchObject({ name: "github", enabled: true, secret: "new-token" });
      // The stored config round-trips untouched (we start from it and only overwrite edited keys).
      expect((patch?.body as { config: { url: string } }).config.url).toBe(
        "https://gh.example/mcp",
      );
    });
  });
});

// ── the settings modal ──────────────────────────────────────────────────────────────────────────

describe("MCP page — settings modal", () => {
  it("a generated server's save omits config, so its elided spec is never written back", async () => {
    const calls = mockRoutes(
      (path, method) => {
        if (method === "PATCH" && path === "/connections/con_3") {
          return jsonResponse({ ...CONN_OPENAPI });
        }
        return undefined;
      },
      { servers: [], connections: [CONN_OPENAPI] },
    );
    renderMcp();

    await screen.findByText("petstore");
    fireEvent.click(screen.getByText("petstore"));
    await screen.findByText("petstore — settings");
    // A generated server exposes only rename/toggle/rotate — no config fields, and a clear note.
    expect(screen.getByText(/Generated openapi server/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      const patch = calls.find(
        (c) => c.method === "PATCH" && pathOf(c.url) === "/connections/con_3",
      );
      expect(patch).toBeDefined();
      // config MUST be absent — sending the summarized spec back would wipe the derived tools.
      expect(patch?.body).not.toHaveProperty("config");
      expect(patch?.body).toMatchObject({ name: "petstore", enabled: true });
    });
  });

  it("a stdio connection's save writes the reconfigured command/args back through config", async () => {
    const calls = mockRoutes(
      (path, method) => {
        if (method === "PATCH" && path === "/connections/con_4") {
          return jsonResponse({ ...CONN_STDIO });
        }
        return undefined;
      },
      { servers: [], connections: [CONN_STDIO] },
    );
    renderMcp();

    await screen.findByText("fs-conn");
    fireEvent.click(screen.getByText("fs-conn"));
    await screen.findByText("fs-conn — settings");
    // The command field is seeded from config; change it and save.
    const command = screen.getByPlaceholderText("npx");
    expect(command).toHaveValue("npx");
    fireEvent.change(command, { target: { value: "uvx" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      const patch = calls.find(
        (c) => c.method === "PATCH" && pathOf(c.url) === "/connections/con_4",
      );
      expect(patch?.body).toMatchObject({
        name: "fs-conn",
        enabled: true,
        config: { transport: "stdio", command: "uvx", args: ["-y", "@mcp/fs", "/tmp"], env: {} },
      });
    });
  });
});
