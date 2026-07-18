// Registries — the model registry page of the interface.
//
// The page opens on your INSTALLED models (the registry) with an "Add model" flow. Adding from a
// hub is an *add-on*, not the headline: you paste a Hugging Face id/URL (or register a hosted/local
// model by hand) right in the Add panel, or open the separate **Browse** screen (a dedicated
// "add a model from a hub" surface, framed source-agnostic so other hubs slot in later).
//
// All data comes from the inference plane's /admin/* surface (the user-controlled plane). Discovery +
// install run THERE — theygent never sees the download (the sovereignty promise).

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Brain,
  ChevronDown,
  ChevronUp,
  Cpu,
  Download,
  Eye,
  ImagePlus,
  Lock,
  MessageSquare,
  Mic,
  PackageOpen,
  Plus,
  Search,
  SearchX,
  ServerOff,
  Star,
  Volume2,
  Waypoints,
  Wrench,
} from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { ModelBench } from "../bench/ModelBench";
import { CategoryBadge, FilterBar } from "../components/Filters";
import { TimeAgo } from "../components/TimeAgo";
import {
  ConfirmDialog,
  ErrorBanner,
  Field,
  Input,
  Modal,
  NoteBanner,
  Page,
  Textarea,
  linkClass,
} from "../components/ui";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Checkbox } from "../components/ui/checkbox";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "../components/ui/empty";
import {
  Item,
  ItemActions,
  ItemContent,
  ItemGroup,
  ItemMedia,
  ItemTitle,
} from "../components/ui/item";
import { NativeSelect, NativeSelectOption } from "../components/ui/native-select";
import { Skeleton } from "../components/ui/skeleton";
import { Spinner } from "../components/ui/spinner";
import { type CatalogEntry, type CatalogVariant, type Fit, type ModelView, api } from "../lib/api";
import { countBy, engineTone, toggle, toneOf } from "../lib/categories";
import { formatBytes, relativeTime } from "../lib/format";
import { notify, trackDownload } from "../lib/notify";
import { useInView } from "../lib/useInView";
import { cn } from "../lib/utils";

// ── helpers ───────────────────────────────────────────────────────────────────

const ENGINE_LABEL: Record<string, string> = { mlx: "MLX", llamacpp: "llama.cpp", vllm: "vLLM" };
const engineLabel = (e: string) => ENGINE_LABEL[e] ?? e;

// A badge tinted by the shared category tone system (lib/categories), so a capability or fit reads
// the same colour here as anywhere else in the app.
function ToneBadge({
  tone = "slate",
  children,
}: {
  tone?: string;
  children: ReactNode;
}) {
  return (
    <Badge variant="secondary" className={cn(toneOf(tone).badge)}>
      {children}
    </Badge>
  );
}

// Hub timestamps can be years old, so this wraps the shared relativeTime (identical output for
// anything under a month — one formatter app-wide) and only adds coarse month/year buckets on top.
// Returns null when the hub omits the date so the caller can drop the "updated …" segment entirely.
function coarseRelativeTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  const days = Math.floor((Date.now() - then) / 86_400_000);
  if (days < 30) return relativeTime(iso);
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(0)}k`;
  return String(n);
}

function slugify(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
}

// Parse a pasted Hugging Face reference: a full URL (https://huggingface.co/org/name/…) or a bare
// repo id (org/name). Returns the normalized "org/name".
function parseHfRef(input: string): string {
  const s = input.trim();
  const m = s.match(/huggingface\.co\/([^/\s]+\/[^/\s?#]+)/i);
  if (m) return m[1];
  return s.replace(/^\/+|\/+$/g, "");
}

function minimalEntry(ref: string): CatalogEntry {
  return {
    provider: "huggingface",
    ref,
    title: ref.split("/").pop() || ref,
    description: "",
    category: "models",
    kind: "model",
    sovereignty: "in-domain",
    engines: [],
    badges: {},
    variants: [],
  };
}

// The registry's `model` field is a full local path for downloaded weights (source=local-path) and a
// short upstream id for reachable/hub models. An absolute path blows out the column and reads as
// noise, so a local model shows just its file/dir name; everything else shows the id as-is. The full
// value always rides in the tooltip.
function modelDisplay(binding: ModelView["binding"]): { text: string; full?: string } {
  const model = binding.model;
  if (!model) return { text: "—" };
  if (binding.source === "local-path") {
    const base =
      model
        .replace(/[/\\]+$/, "")
        .split(/[/\\]/)
        .pop() || model;
    return { text: base, full: model };
  }
  return { text: model, full: model };
}

const FIT_TONE: Record<Fit, string> = {
  fits: "green",
  tight: "amber",
  "too-large": "red",
  unknown: "slate",
};
const FIT_LABEL: Record<Fit, string> = {
  fits: "fits",
  tight: "tight",
  "too-large": "too large",
  unknown: "size unknown",
};

const SIZE_OPTIONS = [
  { value: "", label: "Any size" },
  { value: "small", label: "Small (<3B)" },
  { value: "medium", label: "Medium (3–15B)" },
  { value: "large", label: "Large (>15B)" },
];

// ── the Registries page: installed models + an Add flow ──────────────────────
// Install progress is reported to the global NotificationCenter (bottom-right, persists across
// navigation). Starting an install just spawns a toast.

export function Registries() {
  const [adding, setAdding] = useState(false);
  const [browsing, setBrowsing] = useState(false);

  return (
    <Page>
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-foreground">Registries</h1>
          <p className="text-xs text-muted-foreground">
            Models registered in your inference plane.
          </p>
        </div>
        <Button onClick={() => setAdding(true)}>
          <Plus size={14} /> Add model
        </Button>
      </div>

      <InstalledPanel />

      {adding && (
        <Modal title="Add a model" width="max-w-2xl" onClose={() => setAdding(false)}>
          <AddModelPanel
            onClose={() => setAdding(false)}
            onBrowse={() => {
              // The browse pop-up replaces this one rather than stacking on top of it.
              setAdding(false);
              setBrowsing(true);
            }}
          />
        </Modal>
      )}

      {browsing && <BrowseModal onClose={() => setBrowsing(false)} />}
    </Page>
  );
}

function AddModelPanel({
  onClose,
  onBrowse,
}: {
  onClose: () => void;
  onBrowse: () => void;
}) {
  const qc = useQueryClient();
  const [source, setSource] = useState<"hf" | "manual">("hf");
  const [paste, setPaste] = useState("");
  const [resolved, setResolved] = useState<string | null>(null);

  const register = useMutation({
    mutationFn: ({ logicalId, body }: { logicalId: string; body: unknown }) =>
      api.putModel(logicalId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models"] });
      onClose();
    },
  });

  return (
    <div className="space-y-3">
      {/* A tabs-styled segmented switch. Real <button>s (not tab-role triggers) because the two
          panes are alternate FORMS, and callers/tests address them as plain buttons. */}
      <div className="inline-flex h-8 w-fit items-center justify-center rounded-lg bg-muted p-[3px] text-muted-foreground">
        {(
          [
            ["hf", "Hugging Face"],
            ["manual", "Hosted / local"],
          ] as const
        ).map(([k, label]) => (
          <button
            key={k}
            type="button"
            onClick={() => setSource(k)}
            className={cn(
              "inline-flex h-[calc(100%-1px)] items-center justify-center rounded-md border border-transparent px-3 py-0.5 text-sm font-medium whitespace-nowrap transition-all",
              source === k
                ? "bg-background text-foreground shadow-sm dark:border-input dark:bg-input/30"
                : "text-foreground/60 hover:text-foreground dark:text-muted-foreground dark:hover:text-foreground",
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {source === "hf" ? (
        <div className="space-y-3">
          <div className="flex items-end gap-2">
            <div className="min-w-0 flex-1">
              <Field label="Paste a Hugging Face model (id or URL)">
                <Input
                  value={paste}
                  placeholder="mlx-community/Qwen2.5-0.5B-Instruct-4bit"
                  onChange={(e) => setPaste(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && paste.trim()) setResolved(parseHfRef(paste));
                  }}
                />
              </Field>
            </div>
            <Button
              variant="outline"
              disabled={!paste.trim()}
              onClick={() => setResolved(parseHfRef(paste))}
            >
              Add
            </Button>
          </div>
          <div className="text-xs text-muted-foreground">
            or{" "}
            <button type="button" onClick={onBrowse} className={`${linkClass} hover:underline`}>
              Browse Hugging Face →
            </button>{" "}
            to search the hub in a pop-up.
          </div>
          {resolved && (
            <Card className="gap-0 py-0">
              <CardHeader className="px-4 py-2">
                <CardDescription className="mono truncate text-[11px]" title={resolved}>
                  {resolved}
                </CardDescription>
              </CardHeader>
              <ModelDetail entry={minimalEntry(resolved)} onStarted={onClose} />
            </Card>
          )}
        </div>
      ) : (
        <ManualRegisterForm
          error={register.error}
          pending={register.isPending}
          onSubmit={(logicalId, body) => register.mutate({ logicalId, body })}
        />
      )}
    </div>
  );
}

// ── Installed models (the registry table) ────────────────────────────────────

function InstalledPanel() {
  const qc = useQueryClient();
  const {
    data: models,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["models"],
    queryFn: () => api.listModels(),
  });
  const { data: engines } = useQuery({ queryKey: ["engines"], queryFn: () => api.getEngines() });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["models"] });
    qc.invalidateQueries({ queryKey: ["engines"] });
  };
  const warm = useMutation({ mutationFn: api.warmModel, onSuccess: invalidate });
  const evict = useMutation({ mutationFn: api.evictModel, onSuccess: invalidate });
  const remove = useMutation({ mutationFn: api.deleteModel, onSuccess: invalidate });
  // Deleting unregisters the logical id agents reference — irreversible, so it asks first.
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  // The per-model bench opens in a modal (no separate page) — test/benchmark right here.
  const [benchModel, setBenchModel] = useState<ModelView | null>(null);
  // Clicking a row opens the registration itself: hub installs (source=hf) reopen the catalog
  // detail; everything else gets an editable settings form. Buttons inside the row keep their own
  // actions — the row handler ignores clicks that land on any button.
  const [inspectModel, setInspectModel] = useState<ModelView | null>(null);

  // Filters: by engine (the model's binding — the category) and by resident/cold state, plus a
  // free-text search over the logical id + underlying model. Counts come from the full list.
  const [engineSel, setEngineSel] = useState<string[]>([]);
  const [stateSel, setStateSel] = useState<string[]>([]);
  const [q, setQ] = useState("");

  const list = models ?? [];
  const engineCounts = useMemo(() => countBy(list, (m) => m.binding.binding), [list]);
  const stateCounts = useMemo(
    () => countBy(list, (m) => (isResident(m) ? "resident" : "cold")),
    [list],
  );

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return list.filter((m) => {
      if (engineSel.length && !engineSel.includes(m.binding.binding)) return false;
      const st = isResident(m) ? "resident" : "cold";
      if (stateSel.length && !stateSel.includes(st)) return false;
      if (needle && !`${m.logicalId} ${m.binding.model ?? ""}`.toLowerCase().includes(needle)) {
        return false;
      }
      return true;
    });
  }, [list, engineSel, stateSel, q]);

  const residentCount = engines
    ? Array.isArray(engines.resident)
      ? engines.resident.length
      : Object.keys((engines.resident as Record<string, unknown>) ?? {}).length
    : 0;

  const engineFacet = {
    label: "Engine",
    selected: engineSel,
    onToggle: (v: string) => setEngineSel((s) => toggle(s, v)),
    options: Object.keys(engineCounts)
      .sort()
      .map((b) => ({
        value: b,
        label: engineLabel(b),
        tone: engineTone(b),
        count: engineCounts[b],
      })),
  };
  const stateFacet = {
    label: "State",
    selected: stateSel,
    onToggle: (v: string) => setStateSel((s) => toggle(s, v)),
    options: ["resident", "cold"]
      .filter((s) => stateCounts[s])
      .map((s) => ({
        value: s,
        label: s,
        tone: s === "resident" ? ("green" as const) : ("slate" as const),
        count: stateCounts[s],
      })),
  };

  return (
    <div className="space-y-4">
      {engines && (
        <p className="text-xs text-muted-foreground">
          Resident Models: <span className="text-foreground">{residentCount}</span> /{" "}
          {engines.maxResident}
        </p>
      )}
      <ErrorBanner
        error={
          error
            ? `Could not reach the inference plane: ${(error as Error).message}`
            : (warm.error ?? evict.error ?? remove.error)
        }
      />
      {isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-full" />
        </div>
      ) : error ? null : list.length === 0 ? (
        <Empty className="border border-dashed">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <PackageOpen />
            </EmptyMedia>
            <EmptyTitle>No models registered yet</EmptyTitle>
            <EmptyDescription>Use “Add model” above.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <>
          <FilterBar
            search={q}
            onSearch={setQ}
            searchPlaceholder="Search id, model…"
            facets={[engineFacet, stateFacet]}
            total={list.length}
            shown={filtered.length}
            onClear={() => {
              setEngineSel([]);
              setStateSel([]);
              setQ("");
            }}
          />
          {filtered.length === 0 ? (
            <Empty className="border border-dashed py-8">
              <EmptyDescription>No models match the current filters.</EmptyDescription>
            </Empty>
          ) : (
            <ItemGroup className="gap-2">
              {filtered.map((m: ModelView) => {
                const st = isResident(m) ? "resident" : "cold";
                const { text, full } = modelDisplay(m.binding);
                const warming = warm.isPending && warm.variables === m.logicalId;
                const evicting = evict.isPending && evict.variables === m.logicalId;
                const removing = remove.isPending && remove.variables === m.logicalId;
                // Per-row busy (like the MCP/RAG rows) — an action in flight on one model disables
                // only that model's actions, not the same action on every other row.
                const busy = warming || evicting || removing;
                return (
                  <Item key={m.logicalId} variant="outline" className="bg-card">
                    <ItemMedia variant="icon">
                      <Cpu />
                    </ItemMedia>
                    <ItemContent>
                      <ItemTitle>
                        {/* The logical id opens the registration (settings / hub detail). */}
                        <button
                          type="button"
                          onClick={() => setInspectModel(m)}
                          title="Open the registration"
                          className="mono hover:underline"
                        >
                          {m.logicalId}
                        </button>
                      </ItemTitle>
                      <div className="flex flex-wrap items-center gap-1.5 text-xs">
                        <CategoryBadge
                          tone={engineTone(m.binding.binding)}
                          active={engineSel.includes(m.binding.binding)}
                          onClick={() => setEngineSel((s) => toggle(s, m.binding.binding))}
                          title={`Filter by ${engineLabel(m.binding.binding)}`}
                        >
                          {engineLabel(m.binding.binding)}
                        </CategoryBadge>
                        <CategoryBadge
                          tone={st === "resident" ? "green" : "slate"}
                          active={stateSel.includes(st)}
                          onClick={() => setStateSel((s) => toggle(s, st))}
                          title={`Filter by ${st}`}
                        >
                          {st}
                        </CategoryBadge>
                        <span
                          className="mono max-w-[16rem] truncate text-muted-foreground"
                          title={full}
                        >
                          {text}
                        </span>
                        <CapabilitiesCell logicalId={m.logicalId} />
                      </div>
                    </ItemContent>
                    <ItemActions>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setBenchModel(m)}
                        title="Test & benchmark this model"
                      >
                        Bench
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busy}
                        onClick={() => warm.mutate(m.logicalId)}
                        title="Warm — load the model into the engine"
                      >
                        {warming ? "Warming…" : "Warm"}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busy}
                        onClick={() => evict.mutate(m.logicalId)}
                        title="Evict — unload the model from memory"
                      >
                        {evicting ? "Evicting…" : "Evict"}
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        disabled={busy}
                        onClick={() => setConfirmDelete(m.logicalId)}
                        title="Delete — unregister this model"
                      >
                        {removing ? "Deleting…" : "Delete"}
                      </Button>
                    </ItemActions>
                  </Item>
                );
              })}
            </ItemGroup>
          )}
        </>
      )}
      {benchModel && (
        <Modal
          title={`Bench · ${benchModel.logicalId}`}
          width="max-w-3xl"
          onClose={() => setBenchModel(null)}
        >
          <ModelBench model={benchModel} />
        </Modal>
      )}
      {inspectModel &&
        (inspectModel.binding.source === "hf" && inspectModel.binding.model ? (
          // A hub install: reopen the same catalog detail the browse/paste flows use — the hub ref
          // is the binding's `model`, and the entry is marked installed under this logical id.
          <Modal
            title={inspectModel.logicalId}
            width="max-w-2xl"
            onClose={() => setInspectModel(null)}
          >
            <Card className="gap-0 py-0">
              <CardHeader className="px-4 py-2">
                <CardDescription
                  className="mono truncate text-[11px]"
                  title={inspectModel.binding.model}
                >
                  {inspectModel.binding.model}
                </CardDescription>
              </CardHeader>
              <ModelDetail
                entry={{
                  ...minimalEntry(inspectModel.binding.model),
                  installed: true,
                  installedAs: inspectModel.logicalId,
                }}
              />
            </Card>
          </Modal>
        ) : (
          <RegistrationSettingsModal model={inspectModel} onClose={() => setInspectModel(null)} />
        ))}
      {confirmDelete && (
        <ConfirmDialog
          title={`Delete ${confirmDelete}?`}
          message={
            <>
              Removes the registration — agents referencing{" "}
              <span className="mono">{confirmDelete}</span> will fail. This cannot be undone.
            </>
          }
          onConfirm={() => {
            remove.mutate(confirmDelete);
            setConfirmDelete(null);
          }}
          onCancel={() => setConfirmDelete(null)}
        />
      )}
    </div>
  );
}

function isResident(m: ModelView): boolean {
  const state = m.state as { resident?: boolean } | undefined;
  return Boolean(state?.resident);
}

// Capabilities are PROBED on demand (a click), never on list load: probing warms the engine
// (spawns the model). tool-use / structured / vision / context come from the engine; `reasoning`
// is detected from the model's chat template.
function CapabilitiesCell({ logicalId }: { logicalId: string }) {
  const [show, setShow] = useState(false);
  const { data, isFetching, error, refetch } = useQuery({
    queryKey: ["model-caps", logicalId],
    queryFn: () => api.getModelCapabilities(logicalId),
    enabled: show,
    retry: false,
  });

  if (!show) {
    return (
      <Button
        variant="outline"
        size="icon-sm"
        aria-label="Probe capabilities"
        title="Probe capabilities — loads the model into the engine"
        onClick={() => setShow(true)}
      >
        <Search size={14} />
      </Button>
    );
  }
  if (isFetching) return <span className="text-xs text-muted-foreground">Probing…</span>;
  if (error) {
    return (
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs text-destructive">{(error as Error).message}</span>
        <Button variant="ghost" size="sm" onClick={() => refetch()}>
          Retry
        </Button>
      </div>
    );
  }
  if (!data) return <span className="text-xs text-muted-foreground">—</span>;

  return (
    <div className="flex flex-wrap items-center gap-1">
      {hasAnyCaps(data) ? (
        <CapabilityBadges caps={data} />
      ) : (
        <span className="text-xs text-muted-foreground">none reported</span>
      )}
    </div>
  );
}

function ManualRegisterForm({
  onSubmit,
  error,
  pending = false,
}: {
  onSubmit: (logicalId: string, body: unknown) => void;
  error?: unknown;
  pending?: boolean;
}) {
  const [logicalId, setLogicalId] = useState("");
  const [binding, setBinding] = useState("openai-compatible");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [credentialRef, setCredentialRef] = useState("");
  const [customCred, setCustomCred] = useState(false);
  const reachable = binding === "openai-compatible";

  // Reachable bindings pick a stored credential (a secret://NAME ref) from the user-side store.
  const { data: creds } = useQuery({
    queryKey: ["credentials"],
    queryFn: () => api.listCredentials(),
    enabled: reachable,
  });

  function submit() {
    if (!logicalId || !model) return;
    // A managed binding registered by hand means "weights already on disk" → source=local-path
    // (to DOWNLOAD a model instead, use the Hugging Face tab). Reachable = a passthrough URL + ref.
    const body = reachable
      ? { binding, model, baseUrl, ...(credentialRef ? { credentialRef } : {}) }
      : { binding, source: "local-path", model };
    onSubmit(logicalId, body);
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        Register a hosted API (OpenAI-compatible) or a model whose weights are already on disk.
      </p>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Logical id">
          <Input
            value={logicalId}
            placeholder="triage-fast"
            onChange={(e) => setLogicalId(e.target.value)}
          />
        </Field>
        <Field label="Binding">
          <NativeSelect
            className="w-full"
            value={binding}
            onChange={(e) => setBinding(e.target.value)}
          >
            <NativeSelectOption value="openai-compatible">openai-compatible</NativeSelectOption>
            <NativeSelectOption value="mlx">mlx</NativeSelectOption>
            <NativeSelectOption value="vllm">vllm</NativeSelectOption>
            <NativeSelectOption value="llamacpp">llamacpp</NativeSelectOption>
          </NativeSelect>
        </Field>
        <Field label={reachable ? "Model (upstream id)" : "Model (path to weights on disk)"}>
          <Input
            value={model}
            placeholder={reachable ? "gpt-4o-mini" : "/path/to/model"}
            onChange={(e) => setModel(e.target.value)}
          />
        </Field>
        {reachable ? (
          <>
            <Field label="Base URL">
              <Input
                value={baseUrl}
                placeholder="https://api.openai.com/v1"
                onChange={(e) => setBaseUrl(e.target.value)}
              />
            </Field>
            <Field label="Credential (resolved locally)">
              {customCred ? (
                <Input
                  value={credentialRef}
                  placeholder="secret://OPENAI_API_KEY"
                  onChange={(e) => setCredentialRef(e.target.value)}
                />
              ) : (
                <NativeSelect
                  className="w-full"
                  value={credentialRef}
                  onChange={(e) => {
                    if (e.target.value === "__custom__") {
                      setCustomCred(true);
                      setCredentialRef("");
                    } else setCredentialRef(e.target.value);
                  }}
                >
                  <NativeSelectOption value="">— no credential —</NativeSelectOption>
                  {(creds ?? []).map((c) => (
                    <NativeSelectOption key={c.name} value={`secret://${c.name}`}>
                      {c.name}
                    </NativeSelectOption>
                  ))}
                  <NativeSelectOption value="__custom__">Custom ref…</NativeSelectOption>
                </NativeSelect>
              )}
            </Field>
          </>
        ) : (
          <div className="col-span-2 text-[11px] leading-relaxed text-muted-foreground">
            Registers with <span className="mono">source: local-path</span> — point “Model” at
            weights already on disk (a GGUF for <span className="mono">llamacpp</span>, an MLX model
            directory for <span className="mono">mlx</span>). To download a model, use the Hugging
            Face tab.
          </div>
        )}
      </div>
      {reachable && (
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          Credentials resolve on your machine (<span className="mono">secret://NAME</span>) and
          never leave it. Add or edit them in Settings → Local credentials.
        </p>
      )}
      <ErrorBanner error={error} />
      <Button disabled={!logicalId.trim() || !model.trim() || pending} onClick={submit}>
        {pending ? "Registering…" : "Register"}
      </Button>
    </div>
  );
}

// ── registration settings (row-click on a non-hub registration) ──────────────

// The registered task vocabulary a binding can declare. `images.generation` is legal on the wire
// but not offered here (generation models register through their own flow); an out-of-vocabulary
// current value is kept as an extra option so opening + saving never silently rewrites it.
const MODALITY_OPTIONS = ["chat", "vision", "embeddings", "audio.transcription", "audio.speech"];

// Editable settings for a registration that ISN'T a hub install: reachable (openai-compatible)
// APIs and managed models pointing at local weights. PUT /admin/models/{id} is an upsert, so save
// overlays the edited fields onto the binding exactly as fetched (same camelCase wire shape,
// nothing stripped) and re-registers under the same logical id. Renaming is not offered — a
// different logical id is a new registration, not an edit.
function RegistrationSettingsModal({
  model,
  onClose,
}: {
  model: ModelView;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const binding = model.binding;
  const reachable = binding.binding === "openai-compatible";

  const originalModality = binding.modality ?? "";
  const originalCredentialRef =
    typeof binding.credentialRef === "string" ? binding.credentialRef : "";
  const [modelField, setModelField] = useState(binding.model ?? "");
  const [source, setSource] = useState(binding.source ?? "local-path");
  const [modality, setModality] = useState(originalModality);
  const [baseUrl, setBaseUrl] = useState(
    typeof binding.baseUrl === "string" ? binding.baseUrl : "",
  );
  const [credentialRef, setCredentialRef] = useState(originalCredentialRef);
  // Guarded JSON: params must stay a JSON object — the error shows inline and blocks Save.
  const [paramsText, setParamsText] = useState(() => JSON.stringify(binding.params ?? {}, null, 2));
  const [paramsError, setParamsError] = useState<string | null>(null);

  // Lifecycle controls render only when the fetched binding carries a lifecycle block (managed
  // registrations do; reachable ones never will).
  const lifecycle =
    binding.lifecycle && typeof binding.lifecycle === "object"
      ? (binding.lifecycle as { keepWarm?: boolean; idleTimeoutSec?: number } & Record<
          string,
          unknown
        >)
      : null;
  const [keepWarm, setKeepWarm] = useState(Boolean(lifecycle?.keepWarm));
  const [idleTimeout, setIdleTimeout] = useState(
    lifecycle?.idleTimeoutSec != null ? String(lifecycle.idleTimeoutSec) : "",
  );

  // Reachable bindings pick a stored credential (a secret://NAME ref) from the user-side store.
  const { data: creds } = useQuery({
    queryKey: ["credentials"],
    queryFn: () => api.listCredentials(),
    enabled: reachable,
  });
  const credOptions = (creds ?? []).map((c) => `secret://${c.name}`);

  function parseParams(): Record<string, unknown> {
    let parsed: unknown;
    try {
      parsed = JSON.parse(paramsText.trim() || "{}");
    } catch {
      throw new Error("params is not valid JSON");
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("params must be a JSON object");
    }
    return parsed as Record<string, unknown>;
  }

  const save = useMutation({
    mutationFn: () => {
      // Start from the binding as fetched and overlay only the edited fields — the wire shape
      // (camelCase, all keys) goes back exactly as it came. An empty modality means "let the
      // plane default it": the key is left out entirely rather than sent as null.
      const { modality: _current, ...kept } = binding;
      const next: Record<string, unknown> = { ...kept, model: modelField.trim() };
      if (modality) next.modality = modality;
      if (reachable) {
        next.baseUrl = baseUrl.trim();
        next.credentialRef = credentialRef || null;
        next.params = parseParams();
      } else {
        next.source = source;
        if (lifecycle) {
          const idle = Number.parseInt(idleTimeout, 10);
          next.lifecycle = {
            ...lifecycle,
            keepWarm,
            ...(Number.isFinite(idle) ? { idleTimeoutSec: idle } : {}),
          };
        }
      }
      return api.putModel(model.logicalId, next);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models"] });
      notify.success(`Updated ${model.logicalId}`);
      onClose();
    },
  });

  const invalid = !modelField.trim() || (reachable && (!baseUrl.trim() || Boolean(paramsError)));

  return (
    <Modal title={model.logicalId} width="max-w-2xl" onClose={onClose}>
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Logical id (read-only)">
            <Input value={model.logicalId} readOnly disabled />
          </Field>
          <Field label="Binding">
            <Input value={binding.binding} readOnly disabled />
          </Field>
          <Field label={reachable ? "Model (upstream id)" : "Model (path or hub repo)"}>
            <Input
              value={modelField}
              placeholder={reachable ? "gpt-4o-mini" : "/path/to/model"}
              onChange={(e) => setModelField(e.target.value)}
            />
          </Field>
          {reachable ? (
            <Field label="Base URL">
              <Input
                value={baseUrl}
                placeholder="https://api.openai.com/v1"
                onChange={(e) => setBaseUrl(e.target.value)}
              />
            </Field>
          ) : (
            <Field label="Source">
              <NativeSelect
                className="w-full"
                value={source}
                onChange={(e) => setSource(e.target.value)}
              >
                <NativeSelectOption value="hf">hf</NativeSelectOption>
                <NativeSelectOption value="local-path">local-path</NativeSelectOption>
                <NativeSelectOption value="url">url</NativeSelectOption>
              </NativeSelect>
            </Field>
          )}
          <Field label="Modality">
            <NativeSelect
              className="w-full"
              value={modality}
              onChange={(e) => setModality(e.target.value)}
            >
              <NativeSelectOption value="">— default (chat) —</NativeSelectOption>
              {MODALITY_OPTIONS.map((m) => (
                <NativeSelectOption key={m} value={m}>
                  {m}
                </NativeSelectOption>
              ))}
              {originalModality && !MODALITY_OPTIONS.includes(originalModality) && (
                <NativeSelectOption value={originalModality}>{originalModality}</NativeSelectOption>
              )}
            </NativeSelect>
          </Field>
          {reachable && (
            <Field label="Credential (resolved locally)">
              <NativeSelect
                className="w-full"
                value={credentialRef}
                onChange={(e) => setCredentialRef(e.target.value)}
              >
                <NativeSelectOption value="">— no credential —</NativeSelectOption>
                {credOptions.map((ref) => (
                  <NativeSelectOption key={ref} value={ref}>
                    {ref.replace(/^secret:\/\//, "")}
                  </NativeSelectOption>
                ))}
                {originalCredentialRef && !credOptions.includes(originalCredentialRef) && (
                  <NativeSelectOption value={originalCredentialRef}>
                    {originalCredentialRef}
                  </NativeSelectOption>
                )}
              </NativeSelect>
            </Field>
          )}
          {!reachable && lifecycle && (
            <>
              <Field label="Idle timeout (seconds)">
                <Input
                  type="number"
                  min={0}
                  value={idleTimeout}
                  onChange={(e) => setIdleTimeout(e.target.value)}
                />
              </Field>
              <label className="flex items-center gap-2 self-end pb-1.5 text-sm text-foreground">
                <Checkbox checked={keepWarm} onCheckedChange={(v) => setKeepWarm(v === true)} />
                Keep warm (never auto-evict)
              </label>
            </>
          )}
          {reachable && (
            <div className="col-span-2">
              <Field label="Params (JSON object)">
                <Textarea
                  rows={4}
                  spellCheck={false}
                  className={cn("mono text-xs", paramsError && "border-destructive")}
                  value={paramsText}
                  onChange={(e) => {
                    const text = e.target.value;
                    setParamsText(text);
                    try {
                      const parsed = JSON.parse(text.trim() || "{}");
                      setParamsError(
                        parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
                          ? null
                          : "must be a JSON object",
                      );
                    } catch (err) {
                      setParamsError((err as Error).message);
                    }
                  }}
                />
              </Field>
              {paramsError && <p className="mt-1 text-[11px] text-destructive">{paramsError}</p>}
            </div>
          )}
        </div>
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          Renaming isn't an edit — register under a new logical id instead. Saving re-registers{" "}
          <span className="mono">{model.logicalId}</span> with the updated binding.
        </p>
        <ErrorBanner error={save.error} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={invalid || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// ── the Browse modal: add a model from a hub (a pop-up over the registry) ──
// A secondary, add-on surface — it overlays the Registries page (like the editor's issues panel)
// rather than being its own route, so the user browses, installs, and closes without leaving.

export function BrowseModal({ onClose }: { onClose: () => void }) {
  // Progress shows in the global NotificationCenter, so the modal stays open while you install more.
  return (
    <Modal
      title="Browse and install directly into your inference plane"
      width="max-w-3xl"
      onClose={onClose}
    >
      <div className="space-y-4">
        <BrowsePanel />
      </div>
    </Modal>
  );
}

// Capability filters — matched against the browse-time hints on each entry (client-side; HF has no
// server-side capability filter). Keys are CatalogEntry boolean fields.
// Capability chips — CLIENT-SIDE booleans over the loaded page (HF exposes no capability filter),
// multi-select (AND). Vision is NOT here: it's a first-class task below (server-side), so it filters
// the listing query itself rather than the loaded slice.
const CAP_FILTERS = [
  { key: "reasoning", label: "Reasoning", icon: Brain },
  { key: "toolCalling", label: "Tools", icon: Wrench },
] as const;
type CapKey = (typeof CAP_FILTERS)[number]["key"];

// Task chips — SERVER-SIDE via the hub's pipeline tag (a model has exactly one task), so these are
// single-select and narrow the listing query, unlike the client-side capability chips beside them.
const TASK_CHIPS = [
  { value: "chat", label: "Chat", icon: MessageSquare },
  { value: "vision", label: "Vision", icon: Eye },
  { value: "images.generation", label: "Image generation", icon: ImagePlus },
  { value: "embeddings", label: "Embeddings", icon: Waypoints },
  { value: "audio.transcription", label: "Speech-to-text", icon: Mic },
  { value: "audio.speech", label: "Text-to-speech", icon: Volume2 },
];

// The hub pipeline tag → the task label shown on a card (chat/text-generation is the default and
// wears no badge). Mirrors the modality the install will register.
const TASK_OF_PIPELINE: Record<string, string> = {
  "image-text-to-text": "vision",
  "feature-extraction": "embeddings",
  "sentence-similarity": "embeddings",
  "automatic-speech-recognition": "speech-to-text",
  "text-to-speech": "text-to-speech",
  "text-to-image": "image generation",
};

function BrowsePanel() {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [sort, setSort] = useState("trending");
  const [size, setSize] = useState("");
  const [task, setTask] = useState(""); // server-side task (modality) filter; "" ⇒ any
  const [engineSel, setEngineSel] = useState<string[]>([]); // empty ⇒ all ready engines
  const [capsFilter, setCapsFilter] = useState<CapKey[]>([]); // client-side capability filter
  const [limit, setLimit] = useState(30);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(search.trim()), 350);
    return () => clearTimeout(t);
  }, [search]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: intentionally reset paging on input change
  useEffect(() => setLimit(30), [debounced, sort, size, task, engineSel]);

  const { data, isFetching, error } = useQuery({
    queryKey: ["catalog", debounced, sort, size, task, engineSel.join(","), limit],
    queryFn: () =>
      api.searchCatalogModels({
        search: debounced,
        sort,
        size: size || undefined,
        modality: task || undefined,
        engines: engineSel,
        limit,
      }),
    // Growing `limit` on scroll changes the key — keep the current list on screen while the bigger
    // page loads instead of blanking to the skeleton.
    placeholderData: keepPreviousData,
  });

  // Infinite scroll: the hub catalog has no cursor, so "more" is a bigger `limit`, capped at the
  // server's max of 100. Grow it as the sentinel nears the viewport; a short page (fewer entries than
  // the requested limit) means the hub returned everything it has for this query.
  const CATALOG_MAX = 100;
  const canLoadMore = !!data && data.entries.length >= limit && limit < CATALOG_MAX;
  const loadMoreRef = useInView(() => setLimit((l) => Math.min(l + 30, CATALOG_MAX)), {
    enabled: canLoadMore && !isFetching,
  });
  // Rendered at the end of the list AND under the "no caps match" empty state, so scrolling keeps
  // widening the loaded set even while the capability filter is hiding everything loaded so far.
  const moreSentinel = (
    <>
      {canLoadMore && <div ref={loadMoreRef} aria-hidden className="h-px" />}
      {isFetching && data && (
        <div className="flex justify-center py-2">
          <Spinner className="text-muted-foreground" />
        </div>
      )}
    </>
  );

  const ready = data?.engines ?? [];
  const noEngine = data !== undefined && ready.length === 0;
  // `engineSel` empty is the "all engines" sentinel. Clicking a chip toggles THAT chip relative to
  // what's currently shown (all, when empty) — so from all-on, clicking one turns it off and leaves
  // the rest on (not the inverse). The last active chip can't be turned off (≥1 always on), and a
  // back-to-all set normalizes to the empty sentinel.
  const toggleEngine = (e: string) =>
    setEngineSel((sel) => {
      const active = sel.length === 0 ? ready : sel;
      const next = active.includes(e) ? active.filter((x) => x !== e) : [...active, e];
      if (next.length === 0) return active; // keep at least one engine on
      return next.length === ready.length ? [] : next;
    });

  const toggleCap = (k: CapKey) =>
    setCapsFilter((s) => (s.includes(k) ? s.filter((x) => x !== k) : [...s, k]));

  // Capability filter is CLIENT-SIDE over the loaded page (HF exposes no capability filter). An
  // entry passes only if it has every active cap; scrolling pulls further pages to widen the set.
  const shownEntries = useMemo(() => {
    const entries = data?.entries ?? [];
    if (capsFilter.length === 0) return entries;
    return entries.filter((e) => capsFilter.every((k) => Boolean(e[k])));
  }, [data, capsFilter]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-0 flex-1">
          <Field label="Search Hugging Face">
            <Input
              value={search}
              placeholder="e.g. qwen, llama, phi…"
              onChange={(e) => setSearch(e.target.value)}
            />
          </Field>
        </div>
        <div className="w-44">
          <Field label="Size">
            <NativeSelect className="w-full" value={size} onChange={(e) => setSize(e.target.value)}>
              {SIZE_OPTIONS.map((o) => (
                <NativeSelectOption key={o.value} value={o.value}>
                  {o.label}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </Field>
        </div>
        <div className="w-40">
          <Field label="Sort">
            <NativeSelect className="w-full" value={sort} onChange={(e) => setSort(e.target.value)}>
              <NativeSelectOption value="trending">Trending</NativeSelectOption>
              <NativeSelectOption value="downloads">Most downloaded</NativeSelectOption>
              <NativeSelectOption value="likes">Most liked</NativeSelectOption>
            </NativeSelect>
          </Field>
        </div>
      </div>

      {ready.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>Engines:</span>
          {ready.map((e) => {
            const on = engineSel.length === 0 || engineSel.includes(e);
            return (
              <CategoryBadge
                key={e}
                tone="blue"
                active={on}
                onClick={() => toggleEngine(e)}
                title={`Filter by ${engineLabel(e)}`}
              >
                {engineLabel(e)}
              </CategoryBadge>
            );
          })}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span>Filter:</span>
        {/* Task chips (violet) — single-select, server-side: a model has ONE task, and this narrows
            the listing query itself. */}
        {TASK_CHIPS.map(({ value, label, icon: Icon }) => (
          <CategoryBadge
            key={value}
            tone="violet"
            active={task === value}
            icon={<Icon size={12} strokeWidth={2} />}
            onClick={() => setTask((cur) => (cur === value ? "" : value))}
            title={`Show only ${label.toLowerCase()} models`}
          >
            {label}
          </CategoryBadge>
        ))}
        {/* Capability chips (blue) — multi-select, client-side over the loaded page (metadata hints). */}
        {CAP_FILTERS.map(({ key, label, icon: Icon }) => {
          const on = capsFilter.includes(key);
          return (
            <CategoryBadge
              key={key}
              tone="blue"
              active={on}
              icon={<Icon size={12} strokeWidth={2} />}
              onClick={() => toggleCap(key)}
              title={`Show only ${label.toLowerCase()} models (from metadata hints)`}
            >
              {label}
            </CategoryBadge>
          );
        })}
        {capsFilter.length > 0 && (
          <span className="text-[11px] text-muted-foreground/70">
            · capabilities filter the loaded list
          </span>
        )}
      </div>

      <ErrorBanner
        error={error && `Could not reach the inference plane: ${(error as Error).message}`}
      />

      {noEngine ? (
        <Empty className="border border-dashed">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <ServerOff />
            </EmptyMedia>
            <EmptyTitle>No local engine is ready</EmptyTitle>
            <EmptyDescription>
              Install MLX (<code className="mono">uv tool install mlx-lm</code>) or llama.cpp, then
              refresh — discovery only shows models you can actually run.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : isFetching && !data ? (
        <SkeletonList />
      ) : !data || data.entries.length === 0 ? (
        <Empty className="border border-dashed">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <SearchX />
            </EmptyMedia>
            <EmptyTitle>No matching models</EmptyTitle>
            <EmptyDescription>Try a different search.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : shownEntries.length === 0 ? (
        <>
          <Empty className="border border-dashed py-8">
            <EmptyDescription>
              None of the {data.entries.length} loaded models match the capability filter — keep
              scrolling to load more, or clear the filter.
            </EmptyDescription>
          </Empty>
          {moreSentinel}
        </>
      ) : (
        <div className="space-y-2">
          {shownEntries.map((entry) => (
            <ModelCard
              key={entry.ref}
              entry={entry}
              expanded={selected === entry.ref}
              onToggle={() => setSelected((s) => (s === entry.ref ? null : entry.ref))}
            />
          ))}
          {/* Scroll sentinel: grows the page as it nears the viewport — no "load more" button. */}
          {moreSentinel}
        </div>
      )}
    </div>
  );
}

function SkeletonList() {
  return (
    <div className="space-y-2">
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="space-y-2 rounded-xl bg-card px-4 py-3 ring-1 ring-foreground/10">
          <Skeleton className="h-3.5 w-1/3" />
          <Skeleton className="h-2.5 w-1/2" />
        </div>
      ))}
    </div>
  );
}

// ── shared catalog pieces (used by Browse cards + the Add-panel paste flow) ────

// The one capability-badge set: browse-time hints (Hugging Face metadata, no download — chat
// template + architectures + GGUF header) AND the installed-model probe (CapabilitiesCell) both
// render through here, so the two surfaces can never drift. The install-time probe stays
// authoritative; `showApprox` marks browse-time values as hints it will confirm, while probe
// results carry their own `approximate` flag.
type CapsHints = Pick<CatalogEntry, "reasoning" | "toolCalling" | "vision" | "maxContext"> & {
  structuredOutput?: boolean;
  approximate?: boolean;
};

function hasAnyCaps(caps: CapsHints): boolean {
  return Boolean(
    caps.reasoning || caps.toolCalling || caps.structuredOutput || caps.vision || caps.maxContext,
  );
}

function CapabilityBadges({
  caps,
  showApprox = false,
}: {
  caps: CapsHints;
  showApprox?: boolean;
}) {
  if (!hasAnyCaps(caps)) return null;
  return (
    <>
      {caps.reasoning && <ToneBadge tone="blue">reasoning</ToneBadge>}
      {caps.toolCalling && <ToneBadge tone="green">tools</ToneBadge>}
      {caps.structuredOutput && <ToneBadge tone="green">structured</ToneBadge>}
      {caps.vision && <ToneBadge tone="green">vision</ToneBadge>}
      {/* Context windows are powers of two (32768 = 32k), so divide by 1024, not 1000. */}
      {caps.maxContext ? (
        <Badge variant="secondary">{`${Math.round(caps.maxContext / 1024)}k ctx`}</Badge>
      ) : null}
      {showApprox ? (
        <span title="static hint from model metadata — the probe confirms it on install">
          <ToneBadge tone="amber">approx</ToneBadge>
        </span>
      ) : caps.approximate ? (
        <ToneBadge tone="amber">approx</ToneBadge>
      ) : null}
    </>
  );
}

function ModelCard({
  entry,
  expanded,
  onToggle,
}: {
  entry: CatalogEntry;
  expanded: boolean;
  onToggle: () => void;
}) {
  const updated = coarseRelativeTime(entry.updatedAt);
  return (
    <Card className="gap-0 py-0">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="w-full text-left transition-colors hover:bg-muted/50"
      >
        <CardHeader className="flex flex-row items-center justify-between gap-3 px-4 py-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <CardTitle className="truncate text-sm" title={entry.title}>
                {entry.title}
              </CardTitle>
              {entry.installed && <ToneBadge tone="green">✓ installed</ToneBadge>}
            </div>
            <CardDescription className="mono truncate text-[11px]" title={entry.ref}>
              {entry.ref}
            </CardDescription>
            <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
              {entry.params && <span className="text-foreground">{entry.params}</span>}
              {entry.gated && (
                <span
                  title="needs a Hugging Face token"
                  className="inline-flex items-center gap-0.5"
                >
                  <Lock size={11} className="shrink-0" /> gated
                </span>
              )}
              {entry.license && <span>{entry.license}</span>}
              {updated && (
                <span>
                  updated <TimeAgo iso={entry.updatedAt} label={updated} />
                </span>
              )}
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-1">
              {/* Non-chat tasks wear their modality — it's what the install will register. */}
              {typeof entry.badges.pipelineTag === "string" &&
                TASK_OF_PIPELINE[entry.badges.pipelineTag] && (
                  <ToneBadge tone="violet">{TASK_OF_PIPELINE[entry.badges.pipelineTag]}</ToneBadge>
                )}
              <CapabilityBadges caps={entry} />
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2 text-[11px] text-muted-foreground">
            {entry.engines.map((e) => (
              <Badge key={e} variant="secondary">
                {engineLabel(e)}
              </Badge>
            ))}
            {typeof entry.badges.downloads === "number" && (
              <span title="downloads" className="inline-flex items-center gap-0.5">
                <Download size={12} className="shrink-0" /> {compact(entry.badges.downloads)}
              </span>
            )}
            {typeof entry.badges.likes === "number" && (
              <span title="stars / likes" className="inline-flex items-center gap-0.5">
                <Star size={12} className="shrink-0" /> {compact(entry.badges.likes)}
              </span>
            )}
            <span className="text-muted-foreground/70">
              {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </span>
          </div>
        </CardHeader>
      </button>
      {expanded && <ModelDetail entry={entry} />}
    </Card>
  );
}

function ModelDetail({
  entry,
  onStarted,
}: {
  entry: CatalogEntry;
  // Called when an install begins (the paste flow uses it to close the Add panel). Browse omits it
  // so the modal stays open for installing more.
  onStarted?: () => void;
}) {
  const ref_ = entry.ref;
  const { data, isLoading, error } = useQuery({
    queryKey: ["catalog-model", ref_],
    queryFn: () => api.getCatalogModel(ref_),
  });
  const [installing, setInstalling] = useState<CatalogVariant | null>(null);
  // The detail fetch sets `description` to the model-card blurb; the list entry's is just the author.
  const blurb =
    data?.description && data.description !== entry.description ? data.description : null;

  return (
    <CardContent className="space-y-3 border-t py-3">
      <div className="flex items-center justify-between gap-3">
        {entry.installed ? (
          <span className="text-[11px] text-emerald-700 dark:text-emerald-300">
            ✓ Installed as <span className="mono">{entry.installedAs}</span>
          </span>
        ) : (
          <span />
        )}
        <a
          href={`https://huggingface.co/${ref_}`}
          target="_blank"
          rel="noopener noreferrer"
          className={`text-[11px] hover:underline ${linkClass}`}
        >
          View on Hugging Face ↗
        </a>
      </div>
      {blurb && <p className="text-xs leading-relaxed text-muted-foreground">{blurb}</p>}
      {data && hasAnyCaps(data) && (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            Capabilities
          </span>
          <CapabilityBadges caps={data} showApprox />
          <span className="text-[10px] text-muted-foreground/70">
            from model metadata — the probe confirms these once installed
          </span>
        </div>
      )}
      <ErrorBanner error={error} />
      {isLoading ? (
        <div className="space-y-1.5">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : !data || data.variants.length === 0 ? (
        <p className="text-xs text-muted-foreground">No installable variants for your engines.</p>
      ) : (
        <div className="space-y-1.5">
          {data.variants.map((v) => (
            <VariantRow
              key={`${v.engine}:${v.id}`}
              variant={v}
              onInstall={() => setInstalling(v)}
            />
          ))}
        </div>
      )}
      {installing && (
        <InstallDialog
          repo={ref_}
          title={data?.title ?? ref_}
          variant={installing}
          onClose={() => setInstalling(null)}
          onStarted={() => {
            setInstalling(null);
            onStarted?.();
          }}
        />
      )}
    </CardContent>
  );
}

function VariantRow({ variant, onInstall }: { variant: CatalogVariant; onInstall: () => void }) {
  return (
    <Item variant="outline" className="flex-nowrap gap-3 px-3 py-2">
      <ItemContent className="min-w-0 flex-row flex-wrap items-center gap-2">
        <span className="mono text-sm text-foreground">{variant.label}</span>
        <Badge variant="secondary">{engineLabel(variant.engine)}</Badge>
        {variant.recommended && <ToneBadge tone="blue">recommended</ToneBadge>}
        {variant.quality && (
          <span className="text-[11px] text-muted-foreground">{variant.quality}</span>
        )}
      </ItemContent>
      <ItemActions className="shrink-0 gap-3">
        <span className="text-xs text-muted-foreground">{formatBytes(variant.sizeBytes)}</span>
        <span title={variant.fitReason ?? undefined}>
          <ToneBadge tone={FIT_TONE[variant.fit]}>{FIT_LABEL[variant.fit]}</ToneBadge>
        </span>
        <Button
          size="sm"
          variant={variant.fit === "too-large" ? "outline" : "default"}
          onClick={onInstall}
        >
          Install
        </Button>
      </ItemActions>
    </Item>
  );
}

function InstallDialog({
  repo,
  title,
  variant,
  onClose,
  onStarted,
}: {
  repo: string;
  title: string;
  variant: CatalogVariant;
  onClose: () => void;
  onStarted: () => void;
}) {
  const suggested = useMemo(() => {
    const base = slugify(title || repo.split("/").pop() || repo);
    const quant = variant.engine === "llamacpp" ? `-${slugify(variant.label)}` : "";
    return `${base}${quant}`;
  }, [repo, title, variant]);
  const [logicalId, setLogicalId] = useState(suggested);

  const install = useMutation({
    mutationFn: () =>
      api.installCatalogModel({
        repo,
        engine: variant.engine,
        variantId: variant.id,
        logicalId: logicalId.trim(),
      }),
    onSuccess: (job) => {
      // Hand the job to the global center: a live progress card appears bottom-right and follows the
      // user across pages. The dialog/panel then closes (onStarted).
      trackDownload(job);
      notify.success(`Downloading ${job.logicalId}`, {
        description: `${repo} · ${engineLabel(variant.engine)}`,
      });
      onStarted();
    },
  });

  return (
    <Modal title="Install model" width="max-w-md" onClose={onClose}>
      <div className="space-y-4">
        <p className="mono text-[11px] text-muted-foreground">
          {repo} · {variant.label} · {formatBytes(variant.sizeBytes)} ·{" "}
          {engineLabel(variant.engine)}
        </p>
        {variant.fit === "too-large" && (
          <NoteBanner>
            This variant likely exceeds your machine's memory. It may fail to load or run slowly.
          </NoteBanner>
        )}
        <Field label="Logical id (how agents reference it)">
          <Input
            value={logicalId}
            onChange={(e) => setLogicalId(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && logicalId.trim() && !install.isPending) install.mutate();
            }}
            placeholder="my-local-model"
          />
        </Field>
        <ErrorBanner error={install.error} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!logicalId.trim() || install.isPending}
            onClick={() => install.mutate()}
          >
            {install.isPending ? "Starting…" : "Download & install"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// (Live download progress renders in the global NotificationCenter — see lib/notify.tsx.)
