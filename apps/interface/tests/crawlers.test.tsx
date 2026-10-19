// The crawler node in the interface: the Crawlers browser (store cards with pricing and 30-day
// success rate), the install gesture (a configured node, wired into the llm, token asked once and
// stored as a connection), the adapter helper behind it, and the inspector's fixed-input helpers.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type IRDocument, NODE_TYPES } from "@theygent/ir-types";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/lib/api", () => ({
  api: {
    browseCrawlers: vi.fn(),
    getCrawler: vi.fn(),
    convertCrawler: vi.fn(),
    listConnections: vi.fn(),
    createConnection: vi.fn(),
    patchConnection: vi.fn(),
  },
}));

import { addCrawlerNode, irToReactFlow, reactFlowToIr } from "../src/adapter";
import { fieldControl, linesToValue } from "../src/components/CrawlerNodePanel";
import { CrawlersDialog, categoryLabel, pricingLabel } from "../src/components/CrawlersDialog";
import { api } from "../src/lib/api";

const mocked = (fn: unknown) => fn as ReturnType<typeof vi.fn>;

function renderWithClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function llmIr(): IRDocument {
  return {
    schemaVersion: "1.0",
    id: "a",
    name: "a",
    version: "0.1.0",
    models: {},
    tools: {},
    nodes: [
      {
        id: "n_llm",
        type: "llm",
        kind: "activity",
        label: null,
        config: { model: "default", messages: [] },
        ports: {
          in: [
            { id: "in", type: "any", required: true },
            { id: "tools", type: "any", required: false, role: "tool" },
          ],
          out: [{ id: "out", type: "any", required: true }],
        },
      },
    ],
    edges: [],
  } as IRDocument;
}

const CARD = {
  provider: "apify",
  actor: "apify/rag-web-browser",
  title: "Web browser",
  description: "Search the web and read the top pages as Markdown.",
  author: "Apify",
  categories: ["AI", "OPEN_SOURCE"],
  pictureUrl: null,
  url: null,
  pricing: { model: "PAY_PER_EVENT", priceUsd: 0.00228, unit: "Fetched page" },
  users30d: 32000,
  runs30d: 1000,
  successRate30d: 0.99,
  rating: 4.7,
  reviews: 35,
};

const CONFIG = {
  provider: "apify",
  actor: "apify/rag-web-browser",
  connection: "",
  input: {},
  expose: ["query"],
  scalar: [],
  parameterSchema: {
    type: "object",
    properties: { query: { type: "string", description: "What to search for." } },
    required: ["query"],
  },
  description: "Search the web and read the top pages as Markdown.",
  limits: { timeoutSeconds: 120, maxChargeUsd: 0.25, maxItems: 10, itemLimit: 5 },
};

describe("CrawlersDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked(api.browseCrawlers).mockResolvedValue({
      entries: [CARD, { ...CARD, actor: "compass/places", title: "Places", successRate30d: 0.23 }],
      total: 2,
      offset: 0,
      limit: 24,
    });
    mocked(api.getCrawler).mockResolvedValue({
      entry: CARD,
      readmeSummary: "## Web browser",
      defaultRunOptions: {},
      fields: [
        {
          name: "query",
          type: "string",
          description: "What to search for.",
          required: true,
          exposable: true,
        },
        {
          name: "maxResults",
          type: "integer",
          description: "How many pages.",
          required: false,
          exposable: true,
        },
        {
          name: "proxyConfiguration",
          type: "object",
          editor: "proxy",
          description: "Proxy.",
          required: false,
          exposable: false,
        },
      ],
      suggested: { config: CONFIG, unfillable: [], totalFields: 3, suggestedId: "rag_web_browser" },
    });
    mocked(api.convertCrawler).mockImplementation(async (body: { connection: string }) => ({
      config: { ...CONFIG, connection: body.connection },
      unfillable: [],
      totalFields: 3,
      suggestedId: "rag_web_browser",
    }));
  });

  it("cards show the pricing headline and the 30-day success rate", async () => {
    mocked(api.listConnections).mockResolvedValue([]);
    renderWithClient(<CrawlersDialog ir={llmIr()} onChange={vi.fn()} onClose={vi.fn()} />);
    expect(await screen.findByText("Web browser")).toBeInTheDocument();
    expect(screen.getByText("99.0% of 1.0k runs succeeded")).toBeInTheDocument();
    // An unreliable actor reads as such before anyone installs it.
    expect(screen.getByText("23.0% of 1.0k runs succeeded")).toBeInTheDocument();
    expect(screen.getAllByText("Pay per event · $0.0023 / fetched page")).toHaveLength(2);
    expect(screen.getAllByText("32k users / 30 days")).toHaveLength(2);
  });

  it("install asks for the token once, stores it as a connection, and wires the node", async () => {
    mocked(api.listConnections).mockResolvedValue([]);
    mocked(api.createConnection).mockResolvedValue({ id: "con_apify", name: "apify" });
    const onChange = vi.fn();
    const onClose = vi.fn();
    renderWithClient(
      <CrawlersDialog ir={llmIr()} attachTo="n_llm" onChange={onChange} onClose={onClose} />,
    );
    await userEvent.click(await screen.findByText("Web browser"));
    // The suggested field is pre-ticked; the proxy editor is never offered to the model.
    expect(await screen.findByRole("checkbox", { name: "query" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "maxResults" })).not.toBeChecked();
    expect(screen.queryByRole("checkbox", { name: "proxyConfiguration" })).toBeNull();

    const add = screen.getByRole("button", { name: "Add crawler" });
    expect(add).toBeDisabled(); // no connection and no token yet
    await userEvent.type(screen.getByPlaceholderText("apify_api_…"), "apify_api_secret");
    await userEvent.click(add);

    expect(api.createConnection).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "http_auth",
        secret: "apify_api_secret",
        config: expect.objectContaining({ provider: "apify", auth: { type: "bearer" } }),
      }),
    );
    expect(api.convertCrawler).toHaveBeenCalledWith(
      expect.objectContaining({ actor: "apify/rag-web-browser", expose: ["query"] }),
    );
    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as IRDocument;
    const node = next.nodes?.find((n) => n.id === "rag_web_browser");
    expect(node?.type).toBe("crawler");
    // The node references the connection, never the token.
    expect(node?.config).toMatchObject({ connection: "con_apify" });
    expect(JSON.stringify(next)).not.toContain("apify_api_secret");
    expect(
      next.edges?.some(
        (e) => e.channel === "tool" && e.source === "rag_web_browser" && e.target === "n_llm",
      ),
    ).toBe(true);
    expect(onClose).toHaveBeenCalled();
  });

  it("an existing provider connection is reused instead of asking for a token", async () => {
    mocked(api.listConnections).mockResolvedValue([
      {
        id: "con_existing",
        name: "apify",
        kind: "http_auth",
        config: { provider: "apify" },
        hasSecret: true,
        enabled: true,
      },
    ]);
    const onChange = vi.fn();
    renderWithClient(<CrawlersDialog ir={llmIr()} onChange={onChange} onClose={vi.fn()} />);
    await userEvent.click(await screen.findByText("Web browser"));
    await screen.findByRole("checkbox", { name: "query" });
    await screen.findByRole("option", { name: "apify" });
    await userEvent.click(screen.getByRole("button", { name: "Add crawler" }));
    expect(api.createConnection).not.toHaveBeenCalled();
    const next = onChange.mock.calls[0][0] as IRDocument;
    expect(next.nodes?.find((n) => n.type === "crawler")?.config).toMatchObject({
      connection: "con_existing",
    });
  });
});

describe("addCrawlerNode", () => {
  it("drops a node with the registry's ports and round-trips through the canvas", () => {
    const { ir, id } = addCrawlerNode(llmIr(), {
      config: { ...CONFIG, connection: "con_x" },
      suggestedId: "rag_web_browser",
      attachTo: "n_llm",
    });
    const node = ir.nodes?.find((n) => n.id === id);
    expect(node?.kind).toBe(NODE_TYPES.crawler.kind);
    expect(node?.ports?.out?.map((p) => p.id)).toEqual(["out", "err", "use"]);
    // A crawler never mints an ir.tools binding (it dispatches by node id at run time).
    expect(ir.tools).toEqual({});
    const back = reactFlowToIr(irToReactFlow(ir), ir);
    expect(back.nodes?.find((n) => n.id === id)?.config).toEqual(node?.config);
  });

  it("a colliding id gets a suffix; replace keeps the node's id and edges", () => {
    let { ir } = addCrawlerNode(llmIr(), {
      config: CONFIG,
      suggestedId: "web",
      attachTo: "n_llm",
    });
    const second = addCrawlerNode(ir, { config: CONFIG, suggestedId: "web" });
    expect(second.id).toBe("web_2");
    ir = addCrawlerNode(ir, {
      config: { ...CONFIG, actor: "compass/places" },
      suggestedId: "places",
      replace: "web",
    }).ir;
    expect(ir.nodes?.find((n) => n.id === "web")?.config).toMatchObject({
      actor: "compass/places",
    });
    expect(ir.edges?.filter((e) => e.source === "web")).toHaveLength(1);
  });
});

describe("crawler inspector helpers", () => {
  it("maps actor editors to form controls", () => {
    const f = (over: Record<string, unknown>) =>
      ({ name: "x", description: "", required: false, exposable: true, ...over }) as never;
    expect(fieldControl(f({ type: "boolean" }))).toBe("checkbox");
    expect(fieldControl(f({ type: "integer" }))).toBe("number");
    expect(fieldControl(f({ type: "string", enum: ["a"] }))).toBe("select");
    expect(fieldControl(f({ type: "array", editor: "requestListSources" }))).toBe("urls");
    expect(fieldControl(f({ type: "array", editor: "stringList" }))).toBe("list");
    expect(fieldControl(f({ type: "array", enum: ["a", "b"] }))).toBe("multiselect");
    expect(fieldControl(f({ type: "object", editor: "proxy" }))).toBe("json");
  });

  it("turns one-per-line text into the shapes actors want", () => {
    expect(linesToValue("a\n\n b \n", "list")).toEqual(["a", "b"]);
    expect(linesToValue("https://x/\nhttps://y/", "urls")).toEqual([
      { url: "https://x/" },
      { url: "https://y/" },
    ]);
  });

  it("labels store categories, keeping acronyms", () => {
    expect(categoryLabel("LEAD_GENERATION")).toBe("Lead generation");
    expect(categoryLabel("AI")).toBe("AI");
    expect(categoryLabel("SEO_TOOLS")).toBe("SEO tools");
  });

  it("labels pricing models", () => {
    expect(pricingLabel({ model: "FREE", priceUsd: null, unit: null })).toBe("Free");
    expect(pricingLabel({ model: "FLAT_PRICE_PER_MONTH", priceUsd: 20, unit: "month" })).toBe(
      "Flat price per month · $20.00 / month",
    );
  });
});
