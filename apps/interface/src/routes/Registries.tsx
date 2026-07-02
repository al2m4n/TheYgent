// Registries — the model registry page of the interface.
//
// The page opens on your INSTALLED models (the registry) with an "Add model" flow. Adding from a
// hub is an *add-on*, not the headline: you paste a Hugging Face id/URL (or register a hosted/local
// model by hand) right in the Add panel, or open the separate **Browse** screen (a dedicated
// "add a model from a hub" surface, framed source-agnostic so other hubs slot in later).
//
// All data comes from the inference plane's /admin/* surface (the user-controlled plane). Discovery +
// install run THERE — theygent never sees the download (the sovereignty promise).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronUp, Download, Lock, Plus, Star } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { ModelBench } from "../bench/ModelBench";
import { CategoryBadge, FilterBar } from "../components/Filters";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  Empty,
  ErrorBanner,
  Field,
  Input,
  Modal,
  NoteBanner,
  Page,
  Select,
  Spinner,
  Table,
  Td,
  Th,
  linkClass,
} from "../components/ui";
import { type CatalogEntry, type CatalogVariant, type Fit, type ModelView, api } from "../lib/api";
import { countBy, engineTone, toggle } from "../lib/categories";
import { formatBytes, relativeTime } from "../lib/format";
import { notify, trackDownload } from "../lib/notify";

// ── helpers ───────────────────────────────────────────────────────────────────

const ENGINE_LABEL: Record<string, string> = { mlx: "MLX", llamacpp: "llama.cpp", vllm: "vLLM" };
const engineLabel = (e: string) => ENGINE_LABEL[e] ?? e;

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
// Install progress no longer lives here — it's reported to the global NotificationCenter
// (bottom-right, persists across navigation). Starting an install just spawns a toast.

export function Registries() {
  const [adding, setAdding] = useState(false);
  const [browsing, setBrowsing] = useState(false);

  return (
    <Page>
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">Registries</h1>
          <p className="text-xs text-slate-500">Models registered in your inference plane.</p>
        </div>
        <Button variant="primary" onClick={() => setAdding((a) => !a)}>
          {adding ? (
            "Close"
          ) : (
            <>
              <Plus size={14} /> Add model
            </>
          )}
        </Button>
      </div>

      {adding && (
        <AddModelPanel onClose={() => setAdding(false)} onBrowse={() => setBrowsing(true)} />
      )}

      <InstalledPanel />

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
    <Card className="mb-4 space-y-3 p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-200">Add a model</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="text-slate-500 hover:text-slate-300"
        >
          ✕
        </button>
      </div>

      <div className="flex rounded-md border border-slate-700 p-0.5 text-sm">
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
            className={`rounded px-3 py-1 ${
              source === k ? "bg-slate-700 text-slate-100" : "text-slate-400 hover:text-slate-200"
            }`}
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
            <Button disabled={!paste.trim()} onClick={() => setResolved(parseHfRef(paste))}>
              Add
            </Button>
          </div>
          <div className="text-xs text-slate-500">
            or{" "}
            <button type="button" onClick={onBrowse} className={`${linkClass} hover:underline`}>
              Browse Hugging Face →
            </button>{" "}
            to search the hub in a pop-up.
          </div>
          {resolved && (
            <Card className="overflow-hidden">
              <div className="border-b border-slate-800 px-4 py-2">
                <div className="mono truncate text-[11px] text-slate-400">{resolved}</div>
              </div>
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
    </Card>
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
        <p className="text-xs text-slate-400">
          Resident engines: <span className="text-slate-200">{residentCount}</span> /{" "}
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
        <Spinner />
      ) : error ? null : list.length === 0 ? (
        <Empty>No models registered yet. Use “Add model” above.</Empty>
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
            <Empty>No models match the current filters.</Empty>
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Logical id</Th>
                  <Th>Engine</Th>
                  <Th>Model</Th>
                  <Th>State</Th>
                  <Th>Capabilities</Th>
                  <Th>Actions</Th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((m: ModelView) => {
                  const st = isResident(m) ? "resident" : "cold";
                  return (
                    <tr key={m.logicalId} className="align-top hover:bg-slate-800/30">
                      <Td className="mono text-slate-100">{m.logicalId}</Td>
                      <Td>
                        <CategoryBadge
                          tone={engineTone(m.binding.binding)}
                          active={engineSel.includes(m.binding.binding)}
                          onClick={() => setEngineSel((s) => toggle(s, m.binding.binding))}
                          title={`Filter by ${engineLabel(m.binding.binding)}`}
                        >
                          {engineLabel(m.binding.binding)}
                        </CategoryBadge>
                      </Td>
                      <Td className="mono text-slate-300">
                        <span
                          className="block max-w-[220px] truncate"
                          title={m.binding.model ?? undefined}
                        >
                          {m.binding.model ?? "—"}
                        </span>
                      </Td>
                      <Td>
                        <CategoryBadge
                          tone={st === "resident" ? "green" : "slate"}
                          active={stateSel.includes(st)}
                          onClick={() => setStateSel((s) => toggle(s, st))}
                          title={`Filter by ${st}`}
                        >
                          {st}
                        </CategoryBadge>
                      </Td>
                      <Td>
                        <CapabilitiesCell logicalId={m.logicalId} />
                      </Td>
                      <Td>
                        <div className="flex flex-wrap gap-1">
                          <Button variant="primary" onClick={() => setBenchModel(m)}>
                            Bench
                          </Button>
                          <Button
                            disabled={warm.isPending}
                            onClick={() => warm.mutate(m.logicalId)}
                          >
                            {warm.isPending && warm.variables === m.logicalId ? "Warming…" : "Warm"}
                          </Button>
                          <Button
                            disabled={evict.isPending}
                            onClick={() => evict.mutate(m.logicalId)}
                          >
                            {evict.isPending && evict.variables === m.logicalId
                              ? "Evicting…"
                              : "Evict"}
                          </Button>
                          <Button
                            variant="danger"
                            disabled={remove.isPending}
                            onClick={() => setConfirmDelete(m.logicalId)}
                          >
                            {remove.isPending && remove.variables === m.logicalId
                              ? "Deleting…"
                              : "Delete"}
                          </Button>
                        </div>
                      </Td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
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
        title="Probe capabilities — loads the model into the engine"
        onClick={() => setShow(true)}
      >
        Probe
      </Button>
    );
  }
  if (isFetching) return <span className="text-xs text-slate-500">Probing…</span>;
  if (error) {
    return (
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs text-rose-700 dark:text-rose-300">{(error as Error).message}</span>
        <Button variant="ghost" onClick={() => refetch()}>
          Retry
        </Button>
      </div>
    );
  }
  if (!data) return <span className="text-xs text-slate-500">—</span>;

  return (
    <div className="flex flex-wrap items-center gap-1">
      {hasAnyCaps(data) ? (
        <CapabilityBadges caps={data} />
      ) : (
        <span className="text-xs text-slate-500">none reported</span>
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
      <p className="text-xs text-slate-500">
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
          <Select value={binding} onChange={(e) => setBinding(e.target.value)}>
            <option value="openai-compatible">openai-compatible</option>
            <option value="mlx">mlx</option>
            <option value="vllm">vllm</option>
            <option value="llamacpp">llamacpp</option>
          </Select>
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
                <Select
                  value={credentialRef}
                  onChange={(e) => {
                    if (e.target.value === "__custom__") {
                      setCustomCred(true);
                      setCredentialRef("");
                    } else setCredentialRef(e.target.value);
                  }}
                >
                  <option value="">— no credential —</option>
                  {(creds ?? []).map((c) => (
                    <option key={c.name} value={`secret://${c.name}`}>
                      {c.name}
                    </option>
                  ))}
                  <option value="__custom__">Custom ref…</option>
                </Select>
              )}
            </Field>
          </>
        ) : (
          <div className="col-span-2 text-[11px] leading-relaxed text-slate-500">
            Registers with <span className="mono">source: local-path</span> — point “Model” at
            weights already on disk (a GGUF for <span className="mono">llamacpp</span>, an MLX model
            directory for <span className="mono">mlx</span>). To download a model, use the Hugging
            Face tab.
          </div>
        )}
      </div>
      {reachable && (
        <p className="text-[11px] leading-relaxed text-slate-500">
          Credentials resolve on your machine (<span className="mono">secret://NAME</span>) and
          never leave it. Add or edit them in Settings → Local credentials.
        </p>
      )}
      <ErrorBanner error={error} />
      <Button
        variant="primary"
        disabled={!logicalId.trim() || !model.trim() || pending}
        onClick={submit}
      >
        {pending ? "Registering…" : "Register"}
      </Button>
    </div>
  );
}

// ── the Browse modal: add a model from a hub (a pop-up over the registry) ──
// A secondary, add-on surface — it overlays the Registries page (like the editor's issues panel)
// rather than being its own route, so the user browses, installs, and closes without leaving.

export function BrowseModal({ onClose }: { onClose: () => void }) {
  // Progress shows in the global NotificationCenter, so the modal stays open while you install more.
  return (
    <Modal title="Add a model from a hub" width="max-w-3xl" onClose={onClose}>
      <div className="space-y-4">
        <div>
          <p className="text-xs text-slate-500">
            Browse and install directly into your inference plane.
          </p>
          <div className="mt-2 flex items-center gap-2 text-xs text-slate-500">
            <span>Source:</span>
            <Badge tone="blue">Hugging Face</Badge>
            <span className="text-slate-600">· more hubs later</span>
          </div>
        </div>
        <BrowsePanel />
      </div>
    </Modal>
  );
}

// Capability filters — matched against the browse-time hints on each entry (client-side; HF has no
// server-side capability filter). Keys are CatalogEntry boolean fields.
const CAP_FILTERS = [
  { key: "reasoning", label: "Reasoning" },
  { key: "toolCalling", label: "Tools" },
  { key: "vision", label: "Vision" },
] as const;
type CapKey = (typeof CAP_FILTERS)[number]["key"];

function BrowsePanel() {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [sort, setSort] = useState("trending");
  const [size, setSize] = useState("");
  const [engineSel, setEngineSel] = useState<string[]>([]); // empty ⇒ all ready engines
  const [capsFilter, setCapsFilter] = useState<CapKey[]>([]); // client-side capability filter
  const [limit, setLimit] = useState(30);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(search.trim()), 350);
    return () => clearTimeout(t);
  }, [search]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: intentionally reset paging on input change
  useEffect(() => setLimit(30), [debounced, sort, size, engineSel]);

  const { data, isFetching, error } = useQuery({
    queryKey: ["catalog", debounced, sort, size, engineSel.join(","), limit],
    queryFn: () =>
      api.searchCatalogModels({
        search: debounced,
        sort,
        size: size || undefined,
        engines: engineSel,
        limit,
      }),
  });

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
  // entry passes only if it has every active cap; "Load more" pulls further pages to widen the set.
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
            <Select value={size} onChange={(e) => setSize(e.target.value)}>
              {SIZE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <div className="w-40">
          <Field label="Sort">
            <Select value={sort} onChange={(e) => setSort(e.target.value)}>
              <option value="trending">Trending</option>
              <option value="downloads">Most downloaded</option>
              <option value="likes">Most liked</option>
            </Select>
          </Field>
        </div>
      </div>

      {ready.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
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

      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <span>Capabilities:</span>
        {CAP_FILTERS.map(({ key, label }) => {
          const on = capsFilter.includes(key);
          return (
            <CategoryBadge
              key={key}
              tone="blue"
              active={on}
              onClick={() => toggleCap(key)}
              title={`Show only ${label.toLowerCase()} models (from metadata hints)`}
            >
              {label}
            </CategoryBadge>
          );
        })}
        {capsFilter.length > 0 && (
          <span className="text-[11px] text-slate-600">· filters the loaded list</span>
        )}
      </div>

      <ErrorBanner
        error={error && `Could not reach the inference plane: ${(error as Error).message}`}
      />

      {noEngine ? (
        <Empty>
          No local engine is ready. Install MLX (
          <code className="mono">uv tool install mlx-lm</code>) or llama.cpp, then refresh —
          discovery only shows models you can actually run.
        </Empty>
      ) : isFetching && !data ? (
        <SkeletonList />
      ) : !data || data.entries.length === 0 ? (
        <Empty>No matching models. Try a different search.</Empty>
      ) : shownEntries.length === 0 ? (
        <Empty>
          None of the {data.entries.length} loaded models match the capability filter — try “Load
          more” to fetch further pages, or clear the filter.
        </Empty>
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
          {data.entries.length >= limit && (
            <div className="pt-1 text-center">
              <Button onClick={() => setLimit((l) => l + 30)} disabled={isFetching}>
                {isFetching ? "Loading…" : "Load more"}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SkeletonList() {
  return (
    <div className="space-y-2">
      {[0, 1, 2, 3, 4].map((i) => (
        <div
          key={i}
          className="animate-pulse rounded-lg border border-slate-800 bg-[var(--c-surface-2)] px-4 py-3"
        >
          <div className="h-3.5 w-1/3 rounded bg-slate-800" />
          <div className="mt-2 h-2.5 w-1/2 rounded bg-slate-800/70" />
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
      {caps.reasoning && <Badge tone="blue">reasoning</Badge>}
      {caps.toolCalling && <Badge tone="green">tools</Badge>}
      {caps.structuredOutput && <Badge tone="green">structured</Badge>}
      {caps.vision && <Badge tone="green">vision</Badge>}
      {/* Context windows are powers of two (32768 = 32k), so divide by 1024, not 1000. */}
      {caps.maxContext ? <Badge>{`${Math.round(caps.maxContext / 1024)}k ctx`}</Badge> : null}
      {showApprox ? (
        <span title="static hint from model metadata — the probe confirms it on install">
          <Badge tone="amber">approx</Badge>
        </span>
      ) : caps.approximate ? (
        <Badge tone="amber">approx</Badge>
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
    <Card className="overflow-hidden">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left hover:bg-slate-800/30"
      >
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-slate-100">{entry.title}</span>
            {entry.installed && <Badge tone="green">✓ installed</Badge>}
          </div>
          <div className="mono truncate text-[11px] text-slate-500">{entry.ref}</div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-500">
            {entry.params && <span className="text-slate-300">{entry.params}</span>}
            {entry.gated && (
              <span title="needs a Hugging Face token" className="inline-flex items-center gap-0.5">
                <Lock size={11} className="shrink-0" /> gated
              </span>
            )}
            {entry.license && <span>{entry.license}</span>}
            {updated && <span>updated {updated}</span>}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-1">
            <CapabilityBadges caps={entry} />
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2 text-[11px] text-slate-400">
          {entry.engines.map((e) => (
            <Badge key={e}>{engineLabel(e)}</Badge>
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
          <span className="text-slate-600">
            {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          </span>
        </div>
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
    <div className="space-y-3 border-t border-slate-800 px-4 py-3">
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
      {blurb && <p className="text-xs leading-relaxed text-slate-400">{blurb}</p>}
      {data && hasAnyCaps(data) && (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-[10px] uppercase tracking-wide text-slate-500">Capabilities</span>
          <CapabilityBadges caps={data} showApprox />
          <span className="text-[10px] text-slate-600">
            from model metadata — the probe confirms these once installed
          </span>
        </div>
      )}
      <ErrorBanner error={error} />
      {isLoading ? (
        <Spinner label="Loading variants…" />
      ) : !data || data.variants.length === 0 ? (
        <p className="text-xs text-slate-500">No installable variants for your engines.</p>
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
    </div>
  );
}

function VariantRow({ variant, onInstall }: { variant: CatalogVariant; onInstall: () => void }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded border border-slate-800 bg-[var(--c-surface)] px-3 py-2">
      <div className="flex min-w-0 items-center gap-2">
        <span className="mono text-sm text-slate-100">{variant.label}</span>
        <Badge>{engineLabel(variant.engine)}</Badge>
        {variant.recommended && <Badge tone="blue">recommended</Badge>}
        {variant.quality && <span className="text-[11px] text-slate-500">{variant.quality}</span>}
      </div>
      <div className="flex shrink-0 items-center gap-3">
        <span className="text-xs text-slate-400">{formatBytes(variant.sizeBytes)}</span>
        <span title={variant.fitReason ?? undefined}>
          <Badge tone={FIT_TONE[variant.fit]}>{FIT_LABEL[variant.fit]}</Badge>
        </span>
        <Button variant={variant.fit === "too-large" ? "default" : "primary"} onClick={onInstall}>
          Install
        </Button>
      </div>
    </div>
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
        <p className="mono text-[11px] text-slate-500">
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
            variant="primary"
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

// (Live download progress now renders in the global NotificationCenter — see lib/notify.tsx.)
