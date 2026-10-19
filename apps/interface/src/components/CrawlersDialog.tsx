// The Crawlers browser: search the provider's store (proxied and cached by the control plane — the
// browser never calls the provider), read an actor's card (pricing, users and 30-day success rate),
// tick the fields the model should fill, and install: one configured `crawler` node lands on the
// canvas, wired into the llm's tools port when opened from one. The token is asked for once and
// stored as a connection; the node references the connection, never the token.

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import type { IRDocument } from "@theygent/ir-types";
import { ArrowLeft, Globe, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { addCrawlerNode } from "../adapter";
import {
  type ConnectionRecord,
  type CrawlerCard,
  type CrawlerDetail,
  type CrawlerPricing,
  api,
} from "../lib/api";
import { Badge, Button, ErrorBanner, Field, Input, Modal, Select, Spinner } from "./ui";
import { Checkbox } from "./ui/checkbox";

const PAGE = 24;
const API_BASE = "https://api.apify.com/v2";

interface Props {
  ir: IRDocument;
  onChange: (ir: IRDocument) => void;
  onClose: () => void;
  /** An llm node id — the installed crawler is wired into its tools port. */
  attachTo?: string;
  /** A crawler node id to re-point at the chosen actor (keeps its id, edges and position). */
  replace?: string;
}

// Words the store writes in capitals that stay capitalized in a label.
const ACRONYMS = new Set(["ai", "seo", "mcp", "api", "llm"]);

/** `LEAD_GENERATION` → `Lead generation`, `SEO_TOOLS` → `SEO tools`. */
export function categoryLabel(c: string): string {
  const words = c
    .toLowerCase()
    .split("_")
    .map((w) => (ACRONYMS.has(w) ? w.toUpperCase() : w));
  const label = words.join(" ");
  return label.charAt(0).toUpperCase() + label.slice(1);
}

/** The pricing line a card shows: the model, and the headline price when there is one. */
export function pricingLabel(p: CrawlerPricing): string {
  if (p.model === "FREE") return "Free";
  const model = categoryLabel(p.model);
  if (p.priceUsd == null) return model;
  const price = p.priceUsd < 0.01 ? p.priceUsd.toPrecision(2) : p.priceUsd.toFixed(2);
  return `${model} · $${price}${p.unit ? ` / ${p.unit.toLowerCase()}` : ""}`;
}

function compact(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k`;
  return String(n);
}

/** The 30-day success rate as a badge — the reliability signal before anyone installs. */
export function SuccessBadge({ rate, runs }: { rate: number | null; runs: number }) {
  if (rate == null) return <Badge tone="slate">no runs in 30 days</Badge>;
  const tone = rate >= 0.95 ? "green" : rate >= 0.8 ? "amber" : "red";
  return (
    <Badge tone={tone}>
      {(rate * 100).toFixed(rate >= 0.995 ? 0 : 1)}% of {compact(runs)} runs succeeded
    </Badge>
  );
}

/** The provider token connections a crawler node can reference: `http_auth` connections, the ones
 * stamped for this provider first. */
export function providerConnections(conns: ConnectionRecord[] | undefined): ConnectionRecord[] {
  const http = (conns ?? []).filter((c) => c.kind === "http_auth");
  return [
    ...http.filter((c) => c.config?.provider === "apify"),
    ...http.filter((c) => c.config?.provider !== "apify"),
  ];
}

export function CrawlersDialog({ ir, onChange, onClose, attachTo, replace }: Props) {
  const [actor, setActor] = useState<string | null>(null);
  return (
    <Modal title="Add a crawler" onClose={onClose} width="max-w-2xl">
      {actor ? (
        <ActorDetail
          actor={actor}
          ir={ir}
          attachTo={attachTo}
          replace={replace}
          onBack={() => setActor(null)}
          onInstalled={(next) => {
            onChange(next);
            onClose();
          }}
        />
      ) : (
        <StoreSearch onPick={setActor} />
      )}
    </Modal>
  );
}

function StoreSearch({ onPick }: { onPick: (actor: string) => void }) {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [limit, setLimit] = useState(PAGE);
  useEffect(() => {
    const t = setTimeout(() => {
      setDebounced(search.trim());
      setLimit(PAGE);
    }, 350);
    return () => clearTimeout(t);
  }, [search]);

  const page = useQuery({
    queryKey: ["crawlerCatalog", debounced, limit],
    queryFn: () => api.browseCrawlers({ search: debounced || undefined, limit }),
    retry: false,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  });
  const entries = page.data?.entries ?? [];
  const total = page.data?.total ?? null;

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        Hosted scrapers and crawlers from the Apify Store. A crawler node runs one of them as a
        step, or as a tool your model calls with only the fields you choose.
      </p>
      <div className="relative">
        <Search
          size={13}
          aria-hidden
          className="absolute top-1/2 left-2.5 -translate-y-1/2 text-muted-foreground"
        />
        <Input
          className="pl-8"
          placeholder="Search crawlers — web search, maps, a site crawl…"
          aria-label="Search crawlers"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          autoFocus
        />
      </div>
      {page.isError ? (
        <ErrorBanner error={page.error} />
      ) : page.isLoading ? (
        <Spinner label="Searching the store…" />
      ) : entries.length === 0 ? (
        <p className="text-xs text-muted-foreground">No crawlers match.</p>
      ) : (
        <div className="max-h-[55vh] space-y-1.5 overflow-y-auto pr-1">
          {entries.map((e) => (
            <CrawlerCardRow key={e.actor} card={e} onPick={() => onPick(e.actor)} />
          ))}
          {total != null && total > entries.length && limit < 96 && (
            <Button className="w-full" onClick={() => setLimit((l) => l + PAGE)}>
              Show more ({entries.length} of {compact(total)})
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

function CrawlerCardRow({ card, onPick }: { card: CrawlerCard; onPick: () => void }) {
  return (
    <button
      type="button"
      onClick={onPick}
      className="flex w-full items-start gap-2.5 rounded-md border border-border px-2.5 py-2 text-left hover:bg-muted/50"
    >
      <Globe size={16} aria-hidden className="mt-0.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 space-y-1">
        <span className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-sm font-medium text-foreground">{card.title}</span>
          <span className="mono text-[11px] text-muted-foreground">{card.actor}</span>
        </span>
        <span className="line-clamp-2 block text-xs text-muted-foreground">{card.description}</span>
        <span className="flex flex-wrap items-center gap-1">
          <SuccessBadge rate={card.successRate30d} runs={card.runs30d} />
          <Badge tone="violet">{pricingLabel(card.pricing)}</Badge>
          <Badge>{compact(card.users30d)} users / 30 days</Badge>
          {card.categories.slice(0, 2).map((c) => (
            <Badge key={c}>{categoryLabel(c)}</Badge>
          ))}
          <span className="text-[11px] text-muted-foreground">by {card.author}</span>
        </span>
      </span>
    </button>
  );
}

function ActorDetail({
  actor,
  ir,
  attachTo,
  replace,
  onBack,
  onInstalled,
}: {
  actor: string;
  ir: IRDocument;
  attachTo?: string;
  replace?: string;
  onBack: () => void;
  onInstalled: (ir: IRDocument) => void;
}) {
  const qc = useQueryClient();
  const detail = useQuery({
    queryKey: ["crawler", actor],
    queryFn: () => api.getCrawler(actor),
    retry: false,
    staleTime: 300_000,
  });
  const conns = useQuery({
    queryKey: ["connections"],
    queryFn: api.listConnections,
    retry: false,
    staleTime: 30_000,
  });
  const candidates = useMemo(() => providerConnections(conns.data), [conns.data]);

  const [expose, setExpose] = useState<Set<string> | null>(null);
  const [connection, setConnection] = useState<string>("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // Pre-tick the converter's suggestion once the detail arrives, and default the connection to
  // the first provider connection.
  useEffect(() => {
    if (detail.data && expose === null) {
      setExpose(new Set((detail.data.suggested.config.expose as string[]) ?? []));
    }
  }, [detail.data, expose]);
  useEffect(() => {
    if (!connection && candidates.length > 0) setConnection(candidates[0].id);
  }, [candidates, connection]);

  if (detail.isError) {
    return (
      <div className="space-y-3">
        <BackButton onBack={onBack} />
        <ErrorBanner error={detail.error} />
      </div>
    );
  }
  if (!detail.data || expose === null) return <Spinner label="Reading the actor…" />;
  const d: CrawlerDetail = detail.data;
  const exposable = d.fields.filter((f) => f.exposable);
  const fixedCount = d.fields.length - expose.size;
  const cap = (d.suggested.config.limits as { maxChargeUsd?: number } | undefined)?.maxChargeUsd;

  const install = async () => {
    setBusy(true);
    setError(null);
    try {
      let connId = connection;
      if (!connId) {
        const created = await api.createConnection({
          name: "apify",
          kind: "http_auth",
          config: { baseUrl: API_BASE, auth: { type: "bearer" }, provider: "apify" },
          secret: token.trim(),
        });
        connId = created.id;
        setToken(""); // the token is write-only — never kept in the page
        qc.invalidateQueries({ queryKey: ["connections"] });
      }
      const order = d.fields.map((f) => f.name).filter((n) => expose.has(n));
      const conv = await api.convertCrawler({
        actor,
        expose: order,
        connection: connId,
        description: (d.suggested.config.description as string | null) ?? null,
      });
      const { ir: next } = addCrawlerNode(ir, {
        config: conv.config,
        suggestedId: conv.suggestedId,
        label: d.entry.title,
        attachTo,
        replace,
      });
      onInstalled(next);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const toggle = (name: string, on: boolean) =>
    setExpose((prev) => {
      const next = new Set(prev ?? []);
      if (on) next.add(name);
      else next.delete(name);
      return next;
    });

  return (
    <div className="space-y-3">
      <BackButton onBack={onBack} />
      <div>
        <div className="flex flex-wrap items-baseline gap-x-2">
          <h3 className="text-sm font-semibold text-foreground">{d.entry.title}</h3>
          <span className="mono text-[11px] text-muted-foreground">{d.entry.actor}</span>
        </div>
        <div className="mt-1 flex flex-wrap gap-1">
          <SuccessBadge rate={d.entry.successRate30d} runs={d.entry.runs30d} />
          <Badge tone="violet">{pricingLabel(d.entry.pricing)}</Badge>
          <Badge>{compact(d.entry.users30d)} users / 30 days</Badge>
        </div>
        <p className="mt-2 text-xs text-muted-foreground">{d.entry.description}</p>
      </div>

      <div className="space-y-1.5">
        <span className="block text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          Fields the model fills
        </span>
        <p className="text-[11px] text-muted-foreground">
          Everything else stays fixed on the node ({fixedCount} field{fixedCount === 1 ? "" : "s"})
          — set them in the inspector after installing. Fewer fields mean a shorter prompt and no
          model-chosen result counts or paid add-ons.
        </p>
        <div className="max-h-48 overflow-y-auto rounded-md border border-border">
          {exposable.length === 0 ? (
            <p className="px-2.5 py-2 text-xs text-muted-foreground">
              No field of this actor can be filled by a model; it runs on its fixed input.
            </p>
          ) : (
            exposable.map((f) => (
              <label
                key={f.name}
                className="flex cursor-pointer items-start gap-2 border-b border-border/60 px-2.5 py-1.5 last:border-b-0 hover:bg-muted/50"
              >
                <Checkbox
                  className="mt-0.5"
                  aria-label={f.name}
                  checked={expose.has(f.name)}
                  onCheckedChange={(v) => toggle(f.name, v === true)}
                />
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1.5">
                    <span className="mono text-xs text-foreground">{f.name}</span>
                    {f.required && <Badge tone="amber">required</Badge>}
                  </span>
                  <span
                    className="block truncate text-[11px] text-muted-foreground"
                    title={f.description}
                  >
                    {f.title ? `${f.title} — ` : ""}
                    {f.description}
                  </span>
                </span>
              </label>
            ))
          )}
        </div>
        {d.suggested.unfillable.length > 0 && (
          <p className="text-[11px] text-amber-700 dark:text-amber-300">
            Required with no default: {d.suggested.unfillable.join(", ")} — set{" "}
            {d.suggested.unfillable.length === 1 ? "it" : "them"} in the node's fixed input before
            running.
          </p>
        )}
      </div>

      {candidates.length > 0 ? (
        <Field label="Apify connection">
          <Select value={connection} onChange={(e) => setConnection(e.target.value)}>
            {candidates.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
                {c.hasSecret ? "" : " (no token yet)"}
              </option>
            ))}
            <option value="">+ a new connection with a token</option>
          </Select>
        </Field>
      ) : null}
      {!connection && (
        <Field label="Apify API token (stored encrypted, never shown again)">
          <Input
            type="password"
            value={token}
            placeholder="apify_api_…"
            onChange={(e) => setToken(e.target.value)}
          />
        </Field>
      )}
      <p className="text-[11px] text-muted-foreground">
        Runs bill the Apify account that owns the token. Each call is capped at
        {cap != null ? ` $${cap}` : " the node's charge cap"} (change it in the node's limits), and
        never above the platform ceiling in Settings → Crawlers.
      </p>
      <ErrorBanner error={error} />
      <div className="flex justify-end gap-2">
        <Button onClick={onBack}>Back</Button>
        <Button
          variant="primary"
          disabled={busy || (!connection && !token.trim())}
          onClick={install}
        >
          {busy ? "Installing…" : replace ? "Use this crawler" : "Add crawler"}
        </Button>
      </div>
    </div>
  );
}

function BackButton({ onBack }: { onBack: () => void }) {
  return (
    <button
      type="button"
      onClick={onBack}
      className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
    >
      <ArrowLeft size={12} aria-hidden /> All crawlers
    </button>
  );
}
