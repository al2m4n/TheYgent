// The RAG page — define and manage retrieval sources: named document collections your agents
// retrieve from. A source is either an `upload` bucket (drop files into it) or a `crawl` of a site;
// either way the documents are chunked and embedded server-side against the source's pinned
// embedding model (a logical id served by the user's inference plane — the bytes never leave the
// user's trust domain). Ingestion is a background job: starting one hands a progress card to the
// global notification center, which polls the source until it settles. Each row also carries an
// inline query tester — the same retrieval call a `rag` node makes at run time.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Database, Plus } from "lucide-react";
import { type DragEvent, useMemo, useRef, useState } from "react";
import { FilterBar } from "../components/Filters";
import { SearchableSelect } from "../components/SearchableSelect";
import { Badge, ConfirmDialog, ErrorBanner, Page, SectionHeading, Spinner } from "../components/ui";
import { Button } from "../components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "../components/ui/card";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "../components/ui/empty";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "../components/ui/field";
import { Input } from "../components/ui/input";
import {
  Item,
  ItemActions,
  ItemContent,
  ItemFooter,
  ItemGroup,
  ItemMedia,
  ItemTitle,
} from "../components/ui/item";
import { Switch } from "../components/ui/switch";
import {
  type RagQueryMatch,
  type RagSource,
  type RagSourceKind,
  type RagSourceStatus,
  api,
} from "../lib/api";
import { type Tone, countBy, toggle } from "../lib/categories";
import { notify, trackIngest } from "../lib/notify";

// The one place a source status maps to a colour, so the row badge, the filter chips, and the
// ingest card all agree.
const STATUS_TONE: Record<RagSourceStatus, Tone> = {
  empty: "slate",
  ingesting: "blue",
  ready: "green",
  failed: "red",
  cancelled: "slate",
};

const KIND_TONE: Record<RagSourceKind, Tone> = { upload: "violet", crawl: "cyan" };

// The document formats the server-side extractor accepts for an upload source.
const UPLOAD_ACCEPT = ".pdf,.docx,.pptx,.xlsx,.md,.txt,.html,.csv,.json,.epub";
const UPLOAD_EXTENSIONS = new Set(UPLOAD_ACCEPT.split(","));

/** Split dropped/picked files into uploadable vs rejected-by-extension. The `accept` attribute
 * only filters the native picker — a drag-and-drop delivers anything, so the same gate runs
 * here (the server would 422 them anyway; catching it client-side keeps the batch going). */
function partitionFiles(files: FileList): { ok: File[]; rejected: string[] } {
  const ok: File[] = [];
  const rejected: string[] = [];
  for (const file of Array.from(files)) {
    const dot = file.name.lastIndexOf(".");
    const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
    if (UPLOAD_EXTENSIONS.has(ext)) ok.push(file);
    else rejected.push(file.name);
  }
  return { ok, rejected };
}

export function Rag() {
  return (
    <Page className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-foreground">RAG sources</h1>
        <p className="text-xs text-muted-foreground">
          Named document collections your agents retrieve from — upload files or crawl a site, then
          wire a rag node (or capability) at the source by id.
        </p>
      </div>
      <SourceList />
    </Page>
  );
}

function SourceList() {
  const qc = useQueryClient();
  const sources = useQuery({ queryKey: ["ragSources"], queryFn: () => api.listRagSources() });
  const invalidate = () => qc.invalidateQueries({ queryKey: ["ragSources"] });
  const remove = useMutation({ mutationFn: api.deleteRagSource, onSuccess: invalidate });
  const [adding, setAdding] = useState(false);
  // Deleting a source destroys its documents and vectors, so it goes through the shared
  // confirmation dialog instead of firing on the row button.
  const [confirmRemove, setConfirmRemove] = useState<RagSource | null>(null);
  const [kindSel, setKindSel] = useState<string[]>([]);
  const [statusSel, setStatusSel] = useState<string[]>([]);
  const [q, setQ] = useState("");

  const list = sources.data ?? [];
  const kindCounts = useMemo(() => countBy(list, (s) => s.kind), [list]);
  const statusCounts = useMemo(() => countBy(list, (s) => s.status), [list]);
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return list.filter((s) => {
      if (kindSel.length && !kindSel.includes(s.kind)) return false;
      if (statusSel.length && !statusSel.includes(s.status)) return false;
      if (!needle) return true;
      const rootUrl = String(s.config?.root_url ?? "");
      return [s.name, s.embedding_model, rootUrl, s.id].some((field) =>
        field.toLowerCase().includes(needle),
      );
    });
  }, [list, kindSel, statusSel, q]);

  const kindFacet = {
    label: "Kind",
    selected: kindSel,
    onToggle: (v: string) => setKindSel((s) => toggle(s, v)),
    options: (Object.keys(kindCounts) as RagSourceKind[]).sort().map((k) => ({
      value: k,
      label: k,
      tone: KIND_TONE[k],
      count: kindCounts[k],
    })),
  };
  const statusFacet = {
    label: "Status",
    selected: statusSel,
    onToggle: (v: string) => setStatusSel((s) => toggle(s, v)),
    // Lifecycle order, not alphabetical — the chips read as the ingest story.
    options: (["empty", "ingesting", "ready", "failed", "cancelled"] as const)
      .filter((s) => statusCounts[s])
      .map((s) => ({ value: s, label: s, tone: STATUS_TONE[s], count: statusCounts[s] })),
  };

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <SectionHeading>Sources</SectionHeading>
        <Button onClick={() => setAdding((a) => !a)}>
          {adding ? (
            "Close"
          ) : (
            <>
              <Plus size={14} /> New source
            </>
          )}
        </Button>
      </div>

      {adding && (
        <CreateForm
          onDone={() => {
            setAdding(false);
            invalidate();
          }}
          onCancel={() => setAdding(false)}
        />
      )}

      <ErrorBanner error={sources.error ?? remove.error} />
      {sources.isLoading ? (
        <Spinner label="Loading retrieval sources…" />
      ) : list.length === 0 ? (
        <Empty className="border py-10">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <Database />
            </EmptyMedia>
            <EmptyTitle>No retrieval sources yet</EmptyTitle>
            <EmptyDescription>
              Create one above — upload documents or crawl a site — then reference it from a rag
              node in the editor.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <>
          <FilterBar
            search={q}
            onSearch={setQ}
            searchPlaceholder="Search name, model, URL…"
            facets={[kindFacet, statusFacet]}
            total={list.length}
            shown={filtered.length}
            onClear={() => {
              setKindSel([]);
              setStatusSel([]);
              setQ("");
            }}
          />
          {filtered.length === 0 ? (
            <Empty className="border py-10">
              <EmptyDescription>No sources match the current filters.</EmptyDescription>
            </Empty>
          ) : (
            <ItemGroup className="gap-2">
              {filtered.map((s) => (
                <SourceRow
                  key={s.id}
                  source={s}
                  onRemove={() => setConfirmRemove(s)}
                  removing={remove.isPending && remove.variables === s.id}
                />
              ))}
            </ItemGroup>
          )}
        </>
      )}
      {confirmRemove !== null && (
        <ConfirmDialog
          title="Delete retrieval source"
          message={
            <>
              Delete <span className="font-medium text-foreground">{confirmRemove.name}</span>? Its
              documents and vectors are deleted; agents referencing it will error.
            </>
          }
          onConfirm={() => {
            remove.mutate(confirmRemove.id);
            setConfirmRemove(null);
          }}
          onCancel={() => setConfirmRemove(null)}
        />
      )}
    </section>
  );
}

function SourceRow({
  source,
  onRemove,
  removing,
}: {
  source: RagSource;
  onRemove: () => void;
  removing: boolean;
}) {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["ragSources"] });
  const [showDocs, setShowDocs] = useState(false);
  const [showQuery, setShowQuery] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [rowError, setRowError] = useState<unknown>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // Start (or re-run) the crawl, then hand the job to the global center's progress card.
  const ingest = useMutation({
    mutationFn: () => api.ingestRagSource(source.id),
    onSuccess: (s) => {
      setRowError(null);
      trackIngest(s);
      invalidate();
    },
    onError: setRowError,
  });

  // Sequential raw-body uploads (picker or drag-and-drop); the LAST response carries the source
  // already flipped to "ingesting", so that's the one handed to trackIngest. One bad file never
  // aborts the rest — its error is collected and the batch continues.
  const onFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const { ok, rejected } = partitionFiles(files);
    if (rejected.length > 0) {
      notify.error(`Skipped unsupported file(s): ${rejected.join(", ")}`);
    }
    if (ok.length === 0) return;
    setUploading(true);
    setRowError(null);
    const failures: string[] = [];
    let last: RagSource | null = null;
    for (const file of ok) {
      try {
        last = await api.uploadRagDocument(source.id, file);
      } catch (e) {
        failures.push(`${file.name}: ${e instanceof Error ? e.message : String(e)}`);
      }
    }
    if (failures.length > 0) setRowError(new Error(failures.join(" · ")));
    if (last) {
      trackIngest(last);
      invalidate();
      // An expanded documents list must show the new rows, not the pre-upload snapshot.
      qc.invalidateQueries({ queryKey: ["ragDocuments", source.id] });
    }
    setUploading(false);
    if (fileRef.current) fileRef.current.value = "";
  };

  // Drag-and-drop is the second door to the same upload path. dragenter/leave fire on every
  // child crossing, so a depth counter (not a boolean) keeps the highlight stable.
  const [dragDepth, setDragDepth] = useState(0);
  const droppable = source.kind === "upload" && !removing && source.status !== "ingesting";
  const dragProps = droppable
    ? {
        onDragEnter: (e: DragEvent) => {
          if (e.dataTransfer.types.includes("Files")) setDragDepth((d) => d + 1);
        },
        onDragLeave: () => setDragDepth((d) => Math.max(0, d - 1)),
        onDragOver: (e: DragEvent) => {
          if (e.dataTransfer.types.includes("Files")) e.preventDefault(); // allow the drop
        },
        onDrop: (e: DragEvent) => {
          e.preventDefault();
          setDragDepth(0);
          void onFiles(e.dataTransfer.files);
        },
      }
    : {};

  const busy = removing || ingest.isPending || uploading;
  const ingesting = source.status === "ingesting";
  const crawled = source.documents > 0 || source.chunks > 0 || source.status !== "empty";

  return (
    <Item
      variant="outline"
      className={`bg-card transition-shadow ${dragDepth > 0 ? "ring-2 ring-primary/70" : ""}`}
      {...dragProps}
    >
      <ItemMedia variant="icon">
        <Database />
      </ItemMedia>
      <ItemContent>
        <ItemTitle>
          <button
            type="button"
            onClick={() => setShowDocs((v) => !v)}
            aria-expanded={showDocs}
            title={showDocs ? "Hide documents" : "Show documents"}
            className="flex items-center gap-1 hover:underline"
          >
            <ChevronRight
              size={13}
              aria-hidden
              className={`shrink-0 text-muted-foreground transition-transform ${showDocs ? "rotate-90" : ""}`}
            />
            {source.name}
          </button>
        </ItemTitle>
        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          <Badge tone={KIND_TONE[source.kind]}>{source.kind}</Badge>
          <Badge tone={STATUS_TONE[source.status]}>{source.status}</Badge>
          <span className="text-muted-foreground">
            {source.documents} documents · {source.chunks} chunks
          </span>
          <span className="mono text-muted-foreground">{source.embedding_model}</span>
        </div>
        {source.status === "failed" && source.error && (
          <p className="text-[11px] text-red-600 dark:text-red-400">{source.error}</p>
        )}
      </ItemContent>
      <ItemActions>
        {source.kind === "crawl" ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => ingest.mutate()}
            disabled={busy || ingesting}
            title="Fetch and embed the site's pages"
          >
            {ingest.isPending || ingesting ? "Crawling…" : crawled ? "Re-crawl" : "Crawl"}
          </Button>
        ) : (
          <>
            <input
              ref={fileRef}
              type="file"
              multiple
              accept={UPLOAD_ACCEPT}
              className="hidden"
              aria-label="Upload documents"
              onChange={(e) => onFiles(e.target.files)}
            />
            <Button
              variant="outline"
              size="sm"
              onClick={() => fileRef.current?.click()}
              disabled={busy || ingesting}
              title="Add documents to this source — or drag & drop files onto this row"
            >
              {uploading ? "Uploading…" : "Upload files"}
            </Button>
          </>
        )}
        <Button
          variant="outline"
          size="sm"
          onClick={() => setShowQuery((v) => !v)}
          aria-pressed={showQuery}
          title="Test a retrieval query against this source"
        >
          {showQuery ? "Hide query" : "Query"}
        </Button>
        <Button variant="destructive" size="sm" onClick={onRemove} disabled={busy}>
          {removing ? "Deleting…" : "Delete"}
        </Button>
      </ItemActions>
      {rowError != null && (
        <ItemFooter className="flex-col items-stretch justify-start border-t pt-2">
          <ErrorBanner error={rowError} />
        </ItemFooter>
      )}
      {showDocs && (
        <ItemFooter className="flex-col items-stretch justify-start gap-1 border-t pt-2">
          <DocumentList sourceId={source.id} />
        </ItemFooter>
      )}
      {showQuery && (
        <ItemFooter className="flex-col items-stretch justify-start gap-2 border-t pt-2">
          <QueryTester source={source} />
        </ItemFooter>
      )}
    </Item>
  );
}

// The per-source documents, lazy-loaded only once the row is expanded.
function DocumentList({ sourceId }: { sourceId: string }) {
  const docs = useQuery({
    queryKey: ["ragDocuments", sourceId],
    queryFn: () => api.listRagDocuments(sourceId),
    retry: false,
  });
  if (docs.isLoading) return <Spinner label="Loading documents…" />;
  if (docs.error) return <ErrorBanner error={docs.error} />;
  if (!docs.data || docs.data.length === 0) {
    return <p className="text-xs text-muted-foreground">No documents yet.</p>;
  }
  return (
    <ul className="space-y-1">
      {docs.data.map((d) => (
        <li key={d.id} className="flex flex-wrap items-center gap-2 text-xs">
          <Badge tone={d.status === "embedded" ? "green" : d.status === "failed" ? "red" : "slate"}>
            {d.status}
          </Badge>
          <span className="mono truncate text-foreground" title={d.uri}>
            {d.uri}
          </span>
          {d.title && (
            <span className="truncate text-muted-foreground" title={d.title}>
              {d.title}
            </span>
          )}
          <span className="ml-auto shrink-0 text-muted-foreground">{d.chunks} chunks</span>
          {d.status === "failed" && d.error && (
            <span className="w-full text-[11px] text-red-600 dark:text-red-400">{d.error}</span>
          )}
        </li>
      ))}
    </ul>
  );
}

// The inline query tester — the same retrieval call a rag node makes, so what you see here is what
// an agent gets.
function QueryTester({ source }: { source: RagSource }) {
  const [query, setQuery] = useState("");
  const run = useMutation({
    mutationFn: () => api.queryRagSource(source.id, { query: query.trim() }),
  });
  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <Input
          value={query}
          placeholder="Ask this source something…"
          aria-label="Test query"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && query.trim() && !run.isPending) run.mutate();
          }}
        />
        <Button
          size="sm"
          onClick={() => run.mutate()}
          disabled={!query.trim() || run.isPending}
          className="shrink-0"
        >
          {run.isPending ? "Running…" : "Run"}
        </Button>
      </div>
      <ErrorBanner error={run.error} />
      {run.data &&
        (run.data.matches.length === 0 ? (
          <p className="text-xs text-muted-foreground">No matches.</p>
        ) : (
          <div className="space-y-1.5">
            {run.data.matches.map((m) => (
              <MatchCard key={m.chunk_id} match={m} />
            ))}
          </div>
        ))}
    </div>
  );
}

function MatchCard({ match }: { match: RagQueryMatch }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="space-y-1 rounded-md border bg-[var(--c-surface)] px-2.5 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="blue">{match.score.toFixed(2)}</Badge>
        {match.similarity != null && (
          <span className="text-muted-foreground">sim {match.similarity.toFixed(2)}</span>
        )}
        {match.heading && (
          <span className="truncate text-foreground" title={match.heading}>
            {match.heading}
          </span>
        )}
        <span className="mono ml-auto max-w-[50%] truncate text-muted-foreground" title={match.uri}>
          {match.title || match.uri}
        </span>
      </div>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        title={expanded ? "Collapse" : "Expand"}
        className={`w-full whitespace-pre-wrap text-left text-muted-foreground ${expanded ? "" : "line-clamp-3"}`}
      >
        {match.text}
      </button>
    </div>
  );
}

// ── the create form (toggleable card, like the MCP page's register form) ─────────────────────────

function CreateForm({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<RagSourceKind>("upload");
  const [embeddingModel, setEmbeddingModel] = useState("");
  const [rootUrl, setRootUrl] = useState("");
  const [maxPages, setMaxPages] = useState(200);
  const [renderJs, setRenderJs] = useState(false);

  // Registered inference-plane models narrowed to the embeddings task — a quick pick that fills
  // the id field. Free text stays authoritative (an unreachable plane never blocks creating).
  const { data: models } = useQuery({
    queryKey: ["inferenceModels"],
    queryFn: api.listModels,
    retry: false,
    staleTime: 30_000,
  });
  const embedOptions = (models ?? [])
    .filter((m) => m.binding.modality === "embeddings")
    .map((m) => ({ value: m.logicalId, label: m.logicalId }));

  const create = useMutation({
    mutationFn: async () => {
      const created = await api.createRagSource({
        name: name.trim(),
        kind,
        embedding_model: embeddingModel.trim(),
        config:
          kind === "crawl"
            ? { root_url: rootUrl.trim(), max_pages: maxPages, render_js: renderJs }
            : {},
      });
      // A crawl source starts its first crawl immediately — the progress card follows it.
      if (kind === "crawl") {
        try {
          trackIngest(await api.ingestRagSource(created.id));
        } catch (e) {
          notify.error(`Crawl didn't start: ${(e as Error).message}`);
        }
      }
      return created;
    },
    onSuccess: onDone,
  });

  const valid =
    name.trim() && embeddingModel.trim() && (kind === "upload" || rootUrl.trim().length > 0);

  return (
    <Card size="sm">
      <CardHeader className="border-b">
        <CardTitle>New retrieval source</CardTitle>
        <CardDescription>
          Documents are chunked and embedded against the model below; the vectors stay in your
          control plane.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <FieldGroup className="gap-4">
          <div className="grid grid-cols-2 gap-4">
            <Field>
              <FieldLabel htmlFor="rag-name">Name</FieldLabel>
              <Input
                id="rag-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="product-docs"
              />
            </Field>
            <Field>
              <FieldLabel>Kind</FieldLabel>
              <div className="grid grid-cols-2 gap-1">
                {(["upload", "crawl"] as const).map((k) => (
                  <button
                    key={k}
                    type="button"
                    aria-pressed={kind === k}
                    onClick={() => setKind(k)}
                    className={`rounded-md border px-2 py-1.5 text-xs capitalize ${
                      kind === k
                        ? "border-blue-500 bg-blue-500/10 text-blue-700 dark:text-blue-200"
                        : "border-border text-muted-foreground hover:border-ring"
                    }`}
                  >
                    {k}
                  </button>
                ))}
              </div>
              <FieldDescription>
                {kind === "upload" ? "Drop files into the source." : "Fetch a site's pages."}
              </FieldDescription>
            </Field>
          </div>
          <Field>
            <FieldLabel htmlFor="rag-embedding">Embedding model</FieldLabel>
            {embedOptions.length > 0 && (
              <SearchableSelect
                options={embedOptions}
                value={embeddingModel}
                onChange={setEmbeddingModel}
                placeholder="Pick a registered embeddings model…"
                ariaLabel="Registered embeddings models"
              />
            )}
            <Input
              id="rag-embedding"
              value={embeddingModel}
              onChange={(e) => setEmbeddingModel(e.target.value)}
              placeholder="embed-small"
              className="mono"
            />
            <FieldDescription>
              a logical model id served by your inference plane (an embeddings model)
            </FieldDescription>
          </Field>
          {kind === "crawl" && (
            <>
              <div className="grid grid-cols-[1fr_8rem] gap-4">
                <Field>
                  <FieldLabel htmlFor="rag-root-url">Root URL</FieldLabel>
                  <Input
                    id="rag-root-url"
                    value={rootUrl}
                    onChange={(e) => setRootUrl(e.target.value)}
                    placeholder="https://docs.example.com"
                    className="mono"
                  />
                </Field>
                <Field>
                  <FieldLabel htmlFor="rag-max-pages">Max pages</FieldLabel>
                  <Input
                    id="rag-max-pages"
                    type="number"
                    min={1}
                    value={maxPages}
                    onChange={(e) => setMaxPages(Math.max(1, Number(e.target.value) || 1))}
                  />
                </Field>
              </div>
              <Field orientation="horizontal">
                <Switch
                  id="rag-render-js"
                  checked={renderJs}
                  onCheckedChange={(v) => setRenderJs(v === true)}
                />
                <div>
                  <FieldLabel htmlFor="rag-render-js">
                    Render JavaScript (headless browser)
                  </FieldLabel>
                  <FieldDescription>
                    For script-rendered sites. Needs a one-time{" "}
                    <span className="mono">playwright install chromium</span> on the control-plane
                    machine.
                  </FieldDescription>
                </div>
              </Field>
            </>
          )}
        </FieldGroup>
        <ErrorBanner error={create.error} />
      </CardContent>
      <CardFooter className="justify-end gap-2">
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button disabled={!valid || create.isPending} onClick={() => create.mutate()}>
          {create.isPending
            ? "Creating…"
            : kind === "crawl"
              ? "Create + crawl now"
              : "Create source"}
        </Button>
      </CardFooter>
    </Card>
  );
}
