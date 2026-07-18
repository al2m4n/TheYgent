// The MCP page — one unified list of the MCP servers your agents can call, however they were
// registered: name-keyed definitions (the stdio registry) and `mcp_server` CONNECTIONS
// (encrypted auth, hub installs, generated openapi/graphql servers). Two
// entry points sit in the header: **Browse hubs** (install a server from a public MCP registry)
// and **Add server** (define one by hand — stdio subprocess, remote http/sse, or a generated
// server derived from an OpenAPI spec / GraphQL endpoint). Secrets are write-only: they go to
// the encrypted store server-side and never round-trip back to the browser. Each server's Tools
// panel folds out a per-tool runner: run a single tool through a throwaway one-node graph and
// see its raw output.

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronUp, Globe, Lock, Plus, Server, Star } from "lucide-react";
import { type CSSProperties, useEffect, useMemo, useRef, useState } from "react";
import { buildToolGraph } from "../bench/toolgraph";
import { CategoryBadge, FilterBar } from "../components/Filters";
import { TimeAgo } from "../components/TimeAgo";
import {
  Badge,
  ConfirmDialog,
  ErrorBanner,
  Field,
  Input,
  Modal,
  NoteBanner,
  Page,
  SectionHeading,
  Select,
  Spinner,
  Textarea,
  linkClass,
} from "../components/ui";
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
  ItemFooter,
  ItemGroup,
  ItemMedia,
  ItemTitle,
} from "../components/ui/item";
import { Skeleton } from "../components/ui/skeleton";
import { Switch } from "../components/ui/switch";
import {
  type CreateConnectionBody,
  type GeneratedPreview,
  type McpCatalogEntry,
  type McpInstallCandidate,
  type McpInstallInput,
  type McpToolDescriptor,
  api,
} from "../lib/api";
import { countBy, toggle, transportTone } from "../lib/categories";
import { notify } from "../lib/notify";
import { cn } from "../lib/utils";

export function Mcp() {
  const [browsing, setBrowsing] = useState(false);
  const [adding, setAdding] = useState(false);
  return (
    <Page className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-foreground">MCP servers</h1>
          <p className="text-xs text-muted-foreground">
            The external tools your agents can call — defined by hand or installed from a hub.
            Secrets are stored encrypted server-side and never shown again.
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" onClick={() => setBrowsing(true)}>
            <Globe size={14} /> Browse hubs
          </Button>
          <Button onClick={() => setAdding(true)}>
            <Plus size={14} /> Add server
          </Button>
        </div>
      </div>
      <ServerList />
      {browsing && <BrowseHubsModal onClose={() => setBrowsing(false)} />}
      {adding && <AddServerModal onClose={() => setAdding(false)} />}
    </Page>
  );
}

// ── the unified server list (name-keyed definitions + mcp_server connections) ─────────────────────

/** One row of the merged list — a registered server (`connectionId === null`) or an `mcp_server`
 * connection. Normalized here so badges, filters, and actions read one shape. */
interface UnifiedServer {
  key: string;
  name: string;
  transport: string;
  source: "defined" | "connection";
  connected: boolean;
  hasSecret: boolean;
  authType: string | null;
  origin: { registry?: string; name?: string; version?: string } | null;
  connectionId: string | null;
  // The connection's stored config (transport + command/args/env or url/headers + auth), so the
  // settings modal can pre-fill without a second fetch. Null for name-defined servers — the control
  // plane exposes only their summary, not their full config.
  config: Record<string, unknown> | null;
  enabled: boolean;
}

function ServerList() {
  const qc = useQueryClient();
  const servers = useQuery({ queryKey: ["mcpServers"], queryFn: () => api.listMcpServers() });
  const connections = useQuery({ queryKey: ["connections"], queryFn: () => api.listConnections() });
  // A connection row carries no liveness on the wire (it is a stored registration) — the
  // warm/close responses do, so they feed this map and the chip reflects the last known state.
  const [connState, setConnState] = useState<Record<string, boolean>>({});

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["mcpServers"] });
    qc.invalidateQueries({ queryKey: ["connections"] });
  };

  const merged: UnifiedServer[] = useMemo(() => {
    const defined = (servers.data ?? []).map((s) => ({
      key: `srv:${s.name}`,
      name: s.name,
      transport: s.transport,
      source: "defined" as const,
      connected: s.connected,
      hasSecret: false,
      authType: null,
      origin: null,
      connectionId: null,
      config: null,
      enabled: true,
    }));
    const conns = (connections.data ?? [])
      .filter((c) => c.kind === "mcp_server")
      .map((c) => {
        const cfg = (c.config ?? {}) as Record<string, unknown>;
        const auth = (cfg.auth ?? null) as { type?: string } | null;
        const origin = (cfg.origin ?? null) as UnifiedServer["origin"];
        return {
          key: `con:${c.id}`,
          name: c.name,
          transport: typeof cfg.transport === "string" ? cfg.transport : "stdio",
          source: "connection" as const,
          connected: connState[c.id] ?? false,
          hasSecret: c.hasSecret,
          authType: auth?.type ?? null,
          origin,
          connectionId: c.id,
          config: c.config,
          enabled: c.enabled,
        };
      });
    return [...defined, ...conns];
  }, [servers.data, connections.data, connState]);

  const warm = useMutation({
    mutationFn: async (s: UnifiedServer) => {
      if (s.connectionId) {
        const r = await api.warmConnectionMcp(s.connectionId);
        setConnState((m) => ({ ...m, [r.id]: r.connected }));
      } else {
        await api.warmMcpServer(s.name);
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["mcpServers"] }),
  });
  const close = useMutation({
    mutationFn: async (s: UnifiedServer) => {
      if (s.connectionId) {
        const r = await api.closeConnectionMcp(s.connectionId);
        setConnState((m) => ({ ...m, [r.id]: r.connected }));
      } else {
        await api.closeMcpServer(s.name);
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["mcpServers"] }),
  });
  const remove = useMutation({
    mutationFn: (s: UnifiedServer) =>
      s.connectionId ? api.deleteConnection(s.connectionId) : api.deleteMcpServer(s.name),
    onSuccess: invalidate,
  });
  // Deleting is irreversible (a definition's config / a connection's secret is never
  // redisplayed), so it goes through the shared confirmation dialog.
  const [confirmRemove, setConfirmRemove] = useState<UnifiedServer | null>(null);
  // Clicking a server's name opens its settings (rename, config, secret, enable) — the same
  // "click the row to edit its registration" the models registry has.
  const [settingsFor, setSettingsFor] = useState<UnifiedServer | null>(null);

  // Filters: transport (the category) and connected/idle state over the MERGED list, plus a
  // name search. New transports (openapi/graphql/…) appear as facet values automatically.
  const [transportSel, setTransportSel] = useState<string[]>([]);
  const [statusSel, setStatusSel] = useState<string[]>([]);
  const [q, setQ] = useState("");

  const transportCounts = useMemo(() => countBy(merged, (s) => s.transport), [merged]);
  const statusCounts = useMemo(
    () => countBy(merged, (s) => (s.connected ? "connected" : "idle")),
    [merged],
  );
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return merged.filter((s) => {
      if (transportSel.length && !transportSel.includes(s.transport)) return false;
      const st = s.connected ? "connected" : "idle";
      if (statusSel.length && !statusSel.includes(st)) return false;
      if (needle && !s.name.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [merged, transportSel, statusSel, q]);

  const toggleTransport = (v: string) => setTransportSel((s) => toggle(s, v));
  const toggleStatus = (v: string) => setStatusSel((s) => toggle(s, v));

  const transportFacet = {
    label: "Transport",
    selected: transportSel,
    onToggle: toggleTransport,
    options: Object.keys(transportCounts)
      .sort()
      .map((t) => ({ value: t, label: t, tone: transportTone(t), count: transportCounts[t] })),
  };
  const statusFacet = {
    label: "Status",
    selected: statusSel,
    onToggle: toggleStatus,
    options: ["connected", "idle"]
      .filter((s) => statusCounts[s])
      .map((s) => ({
        value: s,
        label: s,
        tone: s === "connected" ? ("green" as const) : ("slate" as const),
        count: statusCounts[s],
      })),
  };

  const loading = servers.isLoading || connections.isLoading;

  return (
    <section className="space-y-3">
      <SectionHeading>Servers</SectionHeading>
      <ErrorBanner
        error={servers.error ?? connections.error ?? warm.error ?? close.error ?? remove.error}
      />
      {loading ? (
        <Spinner label="Loading MCP servers…" />
      ) : merged.length === 0 ? (
        <Empty className="border py-10">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <Server />
            </EmptyMedia>
            <EmptyTitle>No MCP servers yet</EmptyTitle>
            <EmptyDescription>
              Add one by hand, or browse the hubs to install a published server.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <>
          <FilterBar
            search={q}
            onSearch={setQ}
            searchPlaceholder="Search name…"
            facets={[transportFacet, statusFacet]}
            total={merged.length}
            shown={filtered.length}
            onClear={() => {
              setTransportSel([]);
              setStatusSel([]);
              setQ("");
            }}
          />
          {filtered.length === 0 ? (
            <Empty className="border py-10">
              <EmptyDescription>No servers match the current filters.</EmptyDescription>
            </Empty>
          ) : (
            <ItemGroup className="gap-2">
              {filtered.map((s) => (
                <ServerRow
                  key={s.key}
                  server={s}
                  transportSel={transportSel}
                  statusSel={statusSel}
                  onToggleTransport={toggleTransport}
                  onToggleStatus={toggleStatus}
                  onSettings={() => setSettingsFor(s)}
                  onWarm={() => warm.mutate(s)}
                  onClose={() => close.mutate(s)}
                  onRemove={() => setConfirmRemove(s)}
                  warming={warm.isPending && warm.variables?.key === s.key}
                  closing={close.isPending && close.variables?.key === s.key}
                  removing={remove.isPending && remove.variables?.key === s.key}
                />
              ))}
            </ItemGroup>
          )}
        </>
      )}
      {confirmRemove !== null && (
        <ConfirmDialog
          title="Delete MCP server"
          message={
            <>
              Delete <span className="font-medium text-foreground">{confirmRemove.name}</span>?{" "}
              {confirmRemove.source === "connection"
                ? "Its config and encrypted secret cannot be recovered, and agents referencing the connection will fail."
                : "Its command, args, and env config cannot be recovered."}
            </>
          }
          onConfirm={() => {
            remove.mutate(confirmRemove);
            setConfirmRemove(null);
          }}
          onCancel={() => setConfirmRemove(null)}
        />
      )}
      {settingsFor !== null && (
        <EditServerModal server={settingsFor} onClose={() => setSettingsFor(null)} />
      )}
    </section>
  );
}

function ServerRow({
  server,
  transportSel,
  statusSel,
  onToggleTransport,
  onToggleStatus,
  onSettings,
  onWarm,
  onClose,
  onRemove,
  warming,
  closing,
  removing,
}: {
  server: UnifiedServer;
  transportSel: string[];
  statusSel: string[];
  onToggleTransport: (value: string) => void;
  onToggleStatus: (value: string) => void;
  onSettings: () => void;
  onWarm: () => void;
  onClose: () => void;
  onRemove: () => void;
  warming: boolean;
  closing: boolean;
  removing: boolean;
}) {
  const qc = useQueryClient();
  const [showTools, setShowTools] = useState(false);
  const tools = useQuery({
    queryKey: server.connectionId
      ? ["mcpToolsConn", server.connectionId]
      : ["mcpTools", server.name],
    queryFn: () =>
      server.connectionId
        ? api.getConnectionMcpTools(server.connectionId)
        : api.getMcpTools(server.name),
    enabled: showTools,
    retry: false,
  });

  // The interactive authorization flow, for `auth.type === "oauth"` connections only: Connect
  // starts the flow; a pending flow opens the provider's page and polls the status (2s, capped
  // at ~5 minutes) until the browser round-trip lands the tokens.
  const isOauth = server.authType === "oauth" && Boolean(server.connectionId);
  const [polling, setPolling] = useState(false);
  const [oauthError, setOauthError] = useState<string | null>(null);
  const pollStarted = useRef(0);
  const oauthStatus = useQuery({
    queryKey: ["mcpOauth", server.connectionId],
    queryFn: () => api.getConnectionMcpOauth(server.connectionId as string),
    enabled: isOauth,
    retry: false,
    refetchInterval: polling ? 2000 : false,
  });
  useEffect(() => {
    if (!polling || !oauthStatus.data) return;
    if (oauthStatus.data.authorized) {
      setPolling(false);
      notify.success(`${server.name} authorized`);
      qc.invalidateQueries({ queryKey: ["connections"] });
    } else if (oauthStatus.data.lastError && !oauthStatus.data.pending) {
      setPolling(false);
      setOauthError(oauthStatus.data.lastError);
    } else if (Date.now() - pollStarted.current > 5 * 60_000) {
      setPolling(false);
      setOauthError("Authorization timed out — press Connect to try again.");
    }
  }, [polling, oauthStatus.data, qc, server.name]);
  const connect = useMutation({
    mutationFn: () => api.startConnectionMcpOauth(server.connectionId as string),
    onSuccess: (r) => {
      setOauthError(null);
      if (r.status === "authorized") {
        notify.success(`${server.name} authorized`);
        qc.invalidateQueries({ queryKey: ["mcpOauth", server.connectionId] });
      } else if (r.status === "pending") {
        // The provider's consent page opens in a new tab; the callback lands server-side, so
        // this tab just polls the status until the tokens exist.
        if (r.authorizationUrl) window.open(r.authorizationUrl, "_blank");
        pollStarted.current = Date.now();
        setPolling(true);
      } else {
        setOauthError(r.error ?? "authorization failed");
      }
    },
    onError: (e) => setOauthError(e instanceof Error ? e.message : String(e)),
  });

  const st = server.connected ? "connected" : "idle";
  // While any action on this row is in flight, disable the others so they can't race.
  const busy = warming || closing || removing;
  return (
    <Item variant="outline" className="bg-card">
      <ItemMedia variant="icon">
        <Server />
      </ItemMedia>
      <ItemContent>
        <ItemTitle className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={onSettings}
            title="Open settings"
            className="hover:underline"
          >
            {server.name}
          </button>
          {server.hasSecret && (
            <span title="has an encrypted secret (write-only — never shown again)">
              <Lock size={12} className="text-muted-foreground" aria-label="has secret" />
            </span>
          )}
        </ItemTitle>
        <div className="flex flex-wrap items-center gap-1.5">
          <CategoryBadge
            tone={transportTone(server.transport)}
            active={transportSel.includes(server.transport)}
            onClick={() => onToggleTransport(server.transport)}
            title={`Filter by ${server.transport}`}
          >
            {server.transport}
          </CategoryBadge>
          <Badge tone="slate">{server.source}</Badge>
          {server.origin?.registry && (
            <span title={`installed from the ${server.origin.registry} hub`}>
              <Badge tone="cyan">
                {server.origin.registry}
                {server.origin.version ? ` · v${server.origin.version}` : ""}
              </Badge>
            </span>
          )}
          {server.authType && <Badge tone="orange">{server.authType}</Badge>}
          {isOauth && (
            <Badge tone={oauthStatus.data?.authorized ? "green" : "amber"}>
              {oauthStatus.data?.authorized ? "authorized" : "needs auth"}
            </Badge>
          )}
          <CategoryBadge
            tone={server.connected ? "green" : "slate"}
            active={statusSel.includes(st)}
            onClick={() => onToggleStatus(st)}
            title={`Filter by ${st}`}
          >
            {st}
          </CategoryBadge>
        </div>
      </ItemContent>
      <ItemActions>
        {isOauth && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => connect.mutate()}
            disabled={connect.isPending || polling}
            title="Authorize with the provider in your browser"
          >
            {polling ? "Waiting…" : "Connect"}
          </Button>
        )}
        <Button
          variant="outline"
          size="sm"
          onClick={() => setShowTools((v) => !v)}
          aria-pressed={showTools}
        >
          {showTools ? "Hide tools" : "Tools"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={onWarm}
          disabled={busy}
          title="Start/connect the server"
        >
          {warming ? "Warming…" : "Warm"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={onClose}
          disabled={busy}
          title="Disconnect the server (config is kept)"
        >
          {closing ? "Closing…" : "Close"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={onSettings}
          title="Rename, reconfigure, rotate the secret, enable/disable"
        >
          Settings
        </Button>
        <Button variant="destructive" size="sm" onClick={onRemove} disabled={busy}>
          {removing ? "Deleting…" : "Delete"}
        </Button>
      </ItemActions>
      {(showTools || oauthError) && (
        <ItemFooter className="flex-col items-stretch justify-start gap-1 border-t pt-2">
          {oauthError && <ErrorBanner error={oauthError} />}
          {showTools && (
            <>
              {tools.isLoading && <Spinner label="Connecting…" />}
              {tools.error && <ErrorBanner error={tools.error} />}
              {tools.data && tools.data.length === 0 && (
                <p className="text-xs text-muted-foreground">No tools reported.</p>
              )}
              {tools.data && tools.data.length > 0 && (
                <ul className="space-y-1.5">
                  {tools.data.map((t) => (
                    <ToolRunner
                      key={t.name}
                      tool={t}
                      target={
                        server.connectionId
                          ? { connection: server.connectionId }
                          : { server: server.name }
                      }
                    />
                  ))}
                </ul>
              )}
            </>
          )}
        </ItemFooter>
      )}
    </Item>
  );
}

// ── per-tool runner (folded into a server's Tools panel) ─────────────────────────────────────────
// Run ONE of a server's tools right where it's listed. The args editor is pre-seeded from the tool's
// own inputSchema, so you don't have to know the arg names. It runs through the same throwaway
// `input → mcp_tool → output` graph the agent path uses (`buildToolGraph` + `runGraph`) — no new
// backend, no new execution path — and shows the tool's raw output.
function ToolRunner({
  tool,
  target,
}: {
  tool: McpToolDescriptor;
  target: { server?: string; connection?: string };
}) {
  const [open, setOpen] = useState(false);
  const [argsText, setArgsText] = useState(() => skeletonFromSchema(tool.inputSchema));
  const [output, setOutput] = useState<string | null>(null);

  const run = useMutation({
    mutationFn: async () => {
      let input: Record<string, unknown> = {};
      const trimmed = argsText.trim();
      if (trimmed) {
        const parsed = JSON.parse(trimmed);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("Arguments must be a JSON object of named tool args.");
        }
        input = parsed as Record<string, unknown>;
      }
      const ir = buildToolGraph({ ...target, tool: tool.name, argNames: Object.keys(input) });
      const r = await api.runGraph({ ir, input });
      if (r.error) throw new Error(r.error);
      return r.output ?? "";
    },
    onSuccess: (out) => setOutput(out),
  });

  return (
    <li className="rounded-md border border-border/60 bg-muted/30 p-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <span className="mono text-xs text-foreground">{tool.name}</span>
          {tool.description && (
            <p className="text-[11px] leading-snug text-muted-foreground">{tool.description}</p>
          )}
        </div>
        <Button
          variant="outline"
          size="sm"
          className="shrink-0"
          onClick={() => setOpen((v) => !v)}
          aria-pressed={open}
        >
          {open ? "Hide" : "Run"}
        </Button>
      </div>
      {open && (
        <div className="mt-2 space-y-2">
          <Field label="Arguments (JSON)">
            <Textarea
              rows={4}
              className="mono text-xs"
              spellCheck={false}
              value={argsText}
              onChange={(e) => setArgsText(e.target.value)}
            />
          </Field>
          <div className="flex items-center gap-2">
            <Button size="sm" onClick={() => run.mutate()} disabled={run.isPending}>
              {run.isPending ? "Running…" : "Run tool"}
            </Button>
            <span className="text-[11px] text-muted-foreground">
              Runs once through a throwaway one-node agent.
            </span>
          </div>
          <ErrorBanner error={run.error} />
          {/* Only the LATEST run's output — isSuccess resets to false the moment the next run
              starts, so a prior success never lingers beside a fresh error. */}
          {run.isSuccess && output !== null && (
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-background p-2 text-xs text-foreground">
              {formatToolOutput(output)}
            </pre>
          )}
        </div>
      )}
    </li>
  );
}

// ── server settings (opened by clicking a server's name) ─────────────────────────────────────────
// Connection-backed servers carry their full config, so they get a real edit form; name-defined
// servers expose only a summary, so they get a read-only detail view.
function EditServerModal({ server, onClose }: { server: UnifiedServer; onClose: () => void }) {
  if (server.connectionId && server.config) {
    return <ConnectionSettingsModal server={server} onClose={onClose} />;
  }
  return <DefinedServerModal server={server} onClose={onClose} />;
}

function ConnectionSettingsModal({
  server,
  onClose,
}: {
  server: UnifiedServer;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const cfg = (server.config ?? {}) as Record<string, unknown>;
  const transport = typeof cfg.transport === "string" ? cfg.transport : "stdio";
  const isStdio = transport === "stdio";
  const isRemote = transport === "http" || transport === "sse";
  const isGenerated = transport === "openapi" || transport === "graphql";
  // A pasted credential can be replaced; an OAuth grant is minted by the sign-in flow, so a
  // hand-written value would corrupt it — re-authorize with Connect instead.
  const canRotate = Boolean(server.authType && server.authType !== "oauth");
  const mapSecret = server.authType === "env" || server.authType === "headers";

  const [name, setName] = useState(server.name);
  const [enabled, setEnabled] = useState(server.enabled);
  const [command, setCommand] = useState(typeof cfg.command === "string" ? cfg.command : "");
  const [args, setArgs] = useState(Array.isArray(cfg.args) ? (cfg.args as string[]).join(" ") : "");
  const [env, setEnv] = useState(() => envToLines(cfg.env));
  const [cwd, setCwd] = useState(typeof cfg.cwd === "string" ? cfg.cwd : "");
  const [url, setUrl] = useState(typeof cfg.url === "string" ? cfg.url : "");
  const [headers, setHeaders] = useState(() => envToLines(cfg.headers));
  const [newSecret, setNewSecret] = useState("");

  const save = useMutation({
    mutationFn: () => {
      // Start from the stored config so keys we don't edit (auth, origin, a generated server's
      // derived tools) survive the write untouched.
      const nextConfig: Record<string, unknown> = { ...cfg };
      if (isStdio) {
        nextConfig.command = command.trim();
        nextConfig.args = parseArgs(args);
        nextConfig.env = parseEnv(env) ?? {};
        // undefined is dropped by JSON.stringify — clears cwd on the wire when left blank.
        nextConfig.cwd = cwd.trim() || undefined;
      } else if (isRemote) {
        nextConfig.url = url.trim();
        nextConfig.headers = parseEnv(headers) ?? {};
      }
      const body: {
        name?: string;
        config?: Record<string, unknown>;
        enabled?: boolean;
        secret?: string;
      } = { name: name.trim(), enabled };
      // Only editable transports write config back. A generated server's stored config carries the
      // full parsed spec, which the connection dump elides to a summary — sending it back would
      // overwrite the real spec and wipe the derived tools. Omitting config leaves it untouched.
      if (isStdio || isRemote) body.config = nextConfig;
      // The secret is write-only — only send it when the user actually types a replacement.
      if (newSecret.trim()) body.secret = newSecret.trim();
      return api.patchConnection(server.connectionId as string, body);
    },
    onSuccess: () => {
      notify.success(`${name.trim()} updated`);
      qc.invalidateQueries({ queryKey: ["connections"] });
      onClose();
    },
  });

  return (
    <Modal title={`${server.name} — settings`} width="max-w-2xl" onClose={onClose}>
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name">
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Transport">
            <Input value={transport} disabled className="mono" />
          </Field>
        </div>

        {isStdio && (
          <>
            <Field label="Command">
              <Input
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder="npx"
              />
            </Field>
            <Field label="Args (whitespace-separated)">
              <Input value={args} onChange={(e) => setArgs(e.target.value)} />
            </Field>
            <Field label="Env (NAME=value, one per line — non-secret)">
              <Textarea
                rows={3}
                className="mono"
                value={env}
                onChange={(e) => setEnv(e.target.value)}
              />
            </Field>
            <Field label="Working dir (optional)">
              <Input value={cwd} onChange={(e) => setCwd(e.target.value)} />
            </Field>
          </>
        )}

        {isRemote && (
          <>
            <Field label="URL">
              <Input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://host/mcp"
              />
            </Field>
            <Field label="Headers (NAME=value, one per line — non-secret)">
              <Textarea
                rows={2}
                className="mono"
                value={headers}
                onChange={(e) => setHeaders(e.target.value)}
              />
            </Field>
          </>
        )}

        {isGenerated && (
          <NoteBanner>
            Generated {transport} server — its derived tools and upstream spec aren't hand-edited
            here. You can rename it, toggle it, or rotate its credential; re-create it to change the
            spec.
          </NoteBanner>
        )}

        {canRotate && (
          <Field
            label={
              mapSecret
                ? "Rotate secret (JSON object — leave blank to keep the current one)"
                : "Rotate secret (leave blank to keep the current one)"
            }
          >
            {mapSecret ? (
              <Textarea
                value={newSecret}
                onChange={(e) => setNewSecret(e.target.value)}
                placeholder={'{ "NAME": "value" }'}
                className="mono"
                style={SECRET_TEXTAREA_STYLE}
                rows={2}
              />
            ) : (
              <Input
                type="password"
                value={newSecret}
                onChange={(e) => setNewSecret(e.target.value)}
                placeholder="write-only — never shown again"
              />
            )}
          </Field>
        )}

        <label className="flex w-fit items-center gap-2 text-sm text-foreground">
          <Switch checked={enabled} onCheckedChange={setEnabled} />
          Enabled
        </label>

        <ErrorBanner error={save.error} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={!name.trim() || save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save changes"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function DefinedServerModal({ server, onClose }: { server: UnifiedServer; onClose: () => void }) {
  return (
    <Modal title={`${server.name} — settings`} width="max-w-md" onClose={onClose}>
      <div className="space-y-3">
        <NoteBanner>
          This is a name-defined server. Its command, args, and env live in the control-plane config
          and aren't editable from the browser — re-add it to change them, or delete it from its
          row.
        </NoteBanner>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
          <dt className="text-muted-foreground">Name</dt>
          <dd className="mono text-foreground">{server.name}</dd>
          <dt className="text-muted-foreground">Transport</dt>
          <dd className="text-foreground">{server.transport}</dd>
          <dt className="text-muted-foreground">State</dt>
          <dd className="text-foreground">{server.connected ? "connected" : "idle"}</dd>
        </dl>
        <div className="flex justify-end">
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// ── small shared helpers ──────────────────────────────────────────────────────────────────────────

// Turn a JSON-Schema property into a placeholder default so the args editor seeds with the right
// shape. A `type` may be a string or an array of strings — take the first concrete one.
function schemaDefault(prop: unknown): unknown {
  if (!prop || typeof prop !== "object") return null;
  const p = prop as Record<string, unknown>;
  // A nullable schema types as e.g. ["null", "string"]; seed from the first NON-null type.
  const types = Array.isArray(p.type) ? p.type : [p.type];
  const t = types.find((x) => x !== "null") ?? types[0];
  switch (t) {
    case "string":
      return "";
    case "number":
    case "integer":
      return 0;
    case "boolean":
      return false;
    case "array":
      return [];
    case "object":
      return {};
    default:
      return null;
  }
}

// Seed the tool's args editor from its declared inputSchema (an empty object when it has none).
function skeletonFromSchema(schema: Record<string, unknown> | null | undefined): string {
  const props =
    schema && typeof schema === "object"
      ? (schema.properties as Record<string, unknown> | undefined)
      : undefined;
  if (!props || typeof props !== "object") return "{}";
  const out: Record<string, unknown> = {};
  for (const [name, def] of Object.entries(props)) out[name] = schemaDefault(def);
  return JSON.stringify(out, null, 2);
}

// Pretty-print a tool's output as JSON when it parses, else show it raw.
function formatToolOutput(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "(empty output)";
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return raw;
  }
}

// Render a config map ({ NAME: value }) back into the NAME=value lines the forms edit.
function envToLines(v: unknown): string {
  if (!v || typeof v !== "object" || Array.isArray(v)) return "";
  return Object.entries(v as Record<string, unknown>)
    .map(([k, val]) => `${k}=${String(val)}`)
    .join("\n");
}

// Parse newline/space-separated args, and KEY=value env/header lines, into the wire shape.
function parseArgs(raw: string): string[] {
  return raw.split(/\s+/).filter(Boolean);
}
function parseEnv(raw: string): Record<string, string> | undefined {
  const out: Record<string, string> = {};
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const eq = trimmed.indexOf("=");
    if (eq > 0) out[trimmed.slice(0, eq).trim()] = trimmed.slice(eq + 1).trim();
  }
  return Object.keys(out).length ? out : undefined;
}

function slugify(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
}

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(0)}k`;
  return String(n);
}

// The tabs-styled segmented switch used by the browse + add modals. Real <button>s (not
// tab-role triggers) because the panes are alternate FORMS and callers/tests address them as
// plain buttons.
function SegmentedSwitch<T extends string>({
  options,
  value,
  onChange,
}: {
  options: readonly (readonly [T, string])[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="inline-flex h-8 w-fit items-center justify-center rounded-lg bg-muted p-[3px] text-muted-foreground">
      {options.map(([k, label]) => (
        <button
          key={k}
          type="button"
          onClick={() => onChange(k)}
          className={cn(
            "inline-flex h-[calc(100%-1px)] items-center justify-center rounded-md border border-transparent px-3 py-0.5 text-sm font-medium whitespace-nowrap transition-all",
            value === k
              ? "bg-background text-foreground shadow-sm dark:border-input dark:bg-input/30"
              : "text-foreground/60 hover:text-foreground dark:text-muted-foreground dark:hover:text-foreground",
          )}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

// ── Browse hubs: install a published server from an MCP registry ─────────────────────────────────

export function BrowseHubsModal({ onClose }: { onClose: () => void }) {
  // Installs report through toasts, so the modal stays open while you install more.
  return (
    <Modal title="Browse MCP hubs" width="max-w-3xl" onClose={onClose}>
      <HubBrowsePanel />
    </Modal>
  );
}

function HubBrowsePanel() {
  const registries = useQuery({
    queryKey: ["mcpRegistries"],
    queryFn: () => api.listMcpRegistries(),
  });
  const [registry, setRegistry] = useState<string | null>(null);
  useEffect(() => {
    if (registry === null && registries.data?.length) setRegistry(registries.data[0].id);
  }, [registries.data, registry]);

  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebounced(search.trim()), 350);
    return () => clearTimeout(t);
  }, [search]);

  // Cursor pagination: "Load more" appends pages onto an accumulated list keyed by
  // registry+search; changing either resets the accumulation (and the cursor).
  const listKey = `${registry}::${debounced}`;
  const [cursor, setCursor] = useState<string | undefined>(undefined);
  // biome-ignore lint/correctness/useExhaustiveDependencies: intentionally reset paging on input change
  useEffect(() => setCursor(undefined), [listKey]);

  const page = useQuery({
    queryKey: ["mcpCatalog", registry, debounced, cursor ?? ""],
    queryFn: () =>
      api.searchMcpCatalog({
        registry: registry as string,
        search: debounced || undefined,
        limit: 30,
        cursor,
      }),
    enabled: Boolean(registry),
    // Keep the current list on screen while the next page / new search loads.
    placeholderData: keepPreviousData,
  });

  const [acc, setAcc] = useState<{ key: string; entries: McpCatalogEntry[] }>({
    key: "",
    entries: [],
  });
  useEffect(() => {
    const data = page.data;
    if (!data || page.isPlaceholderData) return;
    setAcc((prev) => {
      // First page (no cursor) or a different registry/search → replace; else append, deduped.
      if (prev.key !== listKey || !cursor) return { key: listKey, entries: data.entries };
      const seen = new Set(prev.entries.map((e) => `${e.registry}:${e.name}`));
      return {
        key: listKey,
        entries: [
          ...prev.entries,
          ...data.entries.filter((e) => !seen.has(`${e.registry}:${e.name}`)),
        ],
      };
    });
  }, [page.data, page.isPlaceholderData, listKey, cursor]);
  const entries = acc.key === listKey ? acc.entries : [];

  const [selected, setSelected] = useState<string | null>(null);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        {registries.data && registries.data.length > 0 && (
          <SegmentedSwitch
            options={registries.data.map((r) => [r.id, r.label] as const)}
            value={(registry ?? registries.data[0].id) as string}
            onChange={setRegistry}
          />
        )}
        <div className="min-w-0 flex-1">
          <Field label="Search servers">
            <Input
              value={search}
              placeholder="e.g. github, filesystem, search…"
              onChange={(e) => setSearch(e.target.value)}
            />
          </Field>
        </div>
      </div>

      <ErrorBanner error={registries.error ?? page.error} />

      {registries.isLoading || (page.isFetching && entries.length === 0) ? (
        <div className="space-y-2">
          {[0, 1, 2, 3].map((i) => (
            <div
              key={i}
              className="space-y-2 rounded-xl bg-card px-4 py-3 ring-1 ring-foreground/10"
            >
              <Skeleton className="h-3.5 w-1/3" />
              <Skeleton className="h-2.5 w-1/2" />
            </div>
          ))}
        </div>
      ) : entries.length === 0 ? (
        // A failed registry/catalog fetch already shows the banner above — an additional
        // "no matching servers" would misread as a search result.
        registries.isError || page.isError ? null : (
          <Empty className="border border-dashed">
            <EmptyDescription>No matching servers — try a different search.</EmptyDescription>
          </Empty>
        )
      ) : (
        <div className="space-y-2">
          {entries.map((entry) => (
            <HubEntryCard
              key={`${entry.registry}:${entry.name}`}
              entry={entry}
              expanded={selected === entry.name}
              onToggle={() => setSelected((s) => (s === entry.name ? null : entry.name))}
            />
          ))}
          {page.data?.nextCursor && (
            <div className="flex justify-center pt-1">
              <Button
                variant="outline"
                disabled={page.isFetching}
                onClick={() => setCursor(page.data?.nextCursor ?? undefined)}
              >
                {page.isFetching ? "Loading…" : "Load more"}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function HubEntryCard({
  entry,
  expanded,
  onToggle,
}: {
  entry: McpCatalogEntry;
  expanded: boolean;
  onToggle: () => void;
}) {
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
              <CardTitle className="truncate text-sm" title={entry.title || entry.name}>
                {entry.title || entry.name}
              </CardTitle>
              {entry.installed && <Badge tone="green">✓ installed</Badge>}
              {entry.status && entry.status !== "active" && (
                <Badge tone="amber">{entry.status}</Badge>
              )}
            </div>
            <CardDescription className="mono truncate text-[11px]" title={entry.name}>
              {entry.name}
            </CardDescription>
            {entry.description && (
              <p
                className="mt-1 line-clamp-2 text-xs text-muted-foreground"
                title={entry.description}
              >
                {entry.description}
              </p>
            )}
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
              <span className="mono">v{entry.version}</span>
              {entry.transports.map((t) => (
                <CategoryBadge key={t} tone={transportTone(t)}>
                  {t}
                </CategoryBadge>
              ))}
              {entry.packageTypes.map((p) => (
                <Badge key={p} tone="slate">
                  {p}
                </Badge>
              ))}
              {entry.updatedAt && (
                <span>
                  updated <TimeAgo iso={entry.updatedAt} />
                </span>
              )}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2 text-[11px] text-muted-foreground">
            {typeof entry.stars === "number" && (
              <span title="stars" className="inline-flex items-center gap-0.5">
                <Star size={12} className="shrink-0" /> {compact(entry.stars)}
              </span>
            )}
            <span className="text-muted-foreground/70">
              {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </span>
          </div>
        </CardHeader>
      </button>
      {entry.deprecationMessage && (
        <div className="px-4 pb-3">
          <NoteBanner>{entry.deprecationMessage}</NoteBanner>
        </div>
      )}
      {expanded && <HubEntryDetail entry={entry} />}
    </Card>
  );
}

function HubEntryDetail({ entry }: { entry: McpCatalogEntry }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["mcpCatalogEntry", entry.registry, entry.name],
    queryFn: () => api.getMcpCatalogEntry(entry.registry, entry.name),
  });
  const [installing, setInstalling] = useState<McpInstallCandidate | null>(null);

  return (
    <CardContent className="space-y-3 border-t py-3">
      <div className="flex items-center justify-between gap-3 text-[11px]">
        {entry.installed && entry.installedAs ? (
          <span className="text-emerald-700 dark:text-emerald-300">
            ✓ Installed as <span className="mono">{entry.installedAs}</span>
          </span>
        ) : (
          <span />
        )}
        <span className="flex items-center gap-3">
          {entry.repositoryUrl && (
            <a
              href={entry.repositoryUrl}
              target="_blank"
              rel="noopener noreferrer"
              className={`hover:underline ${linkClass}`}
            >
              Repository ↗
            </a>
          )}
          {entry.websiteUrl && (
            <a
              href={entry.websiteUrl}
              target="_blank"
              rel="noopener noreferrer"
              className={`hover:underline ${linkClass}`}
            >
              Website ↗
            </a>
          )}
        </span>
      </div>
      <ErrorBanner error={error} />
      {isLoading ? (
        <div className="space-y-1.5">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : !data || data.candidates.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No installable form — this entry declares no runnable package or remote endpoint.
        </p>
      ) : (
        <div className="space-y-1.5">
          {data.candidates.map((c) => (
            <Item key={c.id} variant="outline" className="flex-nowrap gap-3 px-3 py-2">
              <ItemContent className="min-w-0 flex-row flex-wrap items-center gap-2">
                <span className="mono truncate text-sm text-foreground" title={c.label}>
                  {c.label}
                </span>
                <CategoryBadge tone={transportTone(c.kind)}>{c.kind}</CategoryBadge>
                {c.supportsOauth && <Badge tone="cyan">sign-in</Badge>}
                {c.inputs.length > 0 && (
                  <span className="text-[11px] text-muted-foreground">
                    {c.inputs.length} input{c.inputs.length === 1 ? "" : "s"}
                  </span>
                )}
              </ItemContent>
              <ItemActions className="shrink-0">
                <Button size="sm" onClick={() => setInstalling(c)}>
                  Install
                </Button>
              </ItemActions>
            </Item>
          ))}
        </div>
      )}
      {installing && data && (
        <HubInstallDialog
          entry={data.entry}
          candidate={installing}
          onClose={() => setInstalling(null)}
        />
      )}
    </CardContent>
  );
}

function HubInstallDialog({
  entry,
  candidate,
  onClose,
}: {
  entry: McpCatalogEntry;
  candidate: McpInstallCandidate;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const suggested = useMemo(() => slugify(entry.name.split("/").pop() || entry.name), [entry.name]);
  const [name, setName] = useState(suggested);
  const [values, setValues] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    for (const i of candidate.inputs) if (i.default != null) init[i.name] = i.default;
    return init;
  });
  const [useOauth, setUseOauth] = useState(false);
  const setValue = (k: string, v: string) => setValues((m) => ({ ...m, [k]: v }));
  // Signing in with the provider replaces pasted credentials — the secret header inputs go
  // inert (and are not sent) while the checkbox is on.
  const inputDisabled = (i: McpInstallInput) => useOauth && i.secret && i.target === "header";
  const missingRequired = candidate.inputs.some(
    (i) => i.required && !inputDisabled(i) && !(values[i.name] ?? ""),
  );

  const install = useMutation({
    mutationFn: () => {
      const send: Record<string, string> = {};
      for (const i of candidate.inputs) {
        if (inputDisabled(i)) continue;
        const v = values[i.name];
        if (v) send[i.name] = v;
      }
      return api.installMcpCatalogEntry({
        registry: entry.registry,
        name: entry.name,
        version: entry.version,
        candidateId: candidate.id,
        connectionName: name.trim(),
        values: send,
        useOauth,
      });
    },
    onSuccess: () => {
      notify.success(
        useOauth
          ? "Installed — find it in the server list and press Connect to authorize"
          : "Installed — find it in the server list",
      );
      qc.invalidateQueries({ queryKey: ["connections"] });
      qc.invalidateQueries({ queryKey: ["mcpCatalog"] });
      qc.invalidateQueries({ queryKey: ["mcpCatalogEntry"] });
      onClose();
    },
  });

  return (
    <Modal title={`Install ${entry.title || entry.name}`} width="max-w-lg" onClose={onClose}>
      <div className="space-y-4">
        <p className="mono text-[11px] text-muted-foreground">
          {entry.name} · v{entry.version} · {candidate.label}
        </p>
        {candidate.warnings.map((w) => (
          <NoteBanner key={w}>{w}</NoteBanner>
        ))}
        {candidate.command && (
          <p
            className="mono truncate text-[11px] text-muted-foreground"
            title={`${candidate.command} ${candidate.args.join(" ")}`}
          >
            {candidate.command} {candidate.args.join(" ")}
          </p>
        )}
        {candidate.url && (
          <p className="mono truncate text-[11px] text-muted-foreground" title={candidate.url}>
            {candidate.url}
          </p>
        )}
        <Field label="Connection name (how agents reference it)">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={suggested} />
        </Field>
        {candidate.inputs.map((i) => (
          <Field key={i.name} label={`${i.name}${i.required ? " *" : ""}`}>
            {i.choices?.length ? (
              <Select
                value={values[i.name] ?? ""}
                disabled={inputDisabled(i)}
                onChange={(e) => setValue(i.name, e.target.value)}
              >
                <option value="">—</option>
                {i.choices.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </Select>
            ) : (
              <Input
                type={i.secret ? "password" : "text"}
                value={values[i.name] ?? ""}
                placeholder={i.placeholder ?? undefined}
                disabled={inputDisabled(i)}
                onChange={(e) => setValue(i.name, e.target.value)}
              />
            )}
            {i.description && (
              <span className="block text-[11px] text-muted-foreground">{i.description}</span>
            )}
          </Field>
        ))}
        {candidate.supportsOauth && (
          <label className="flex items-center gap-2 text-sm text-foreground">
            <Checkbox checked={useOauth} onCheckedChange={(v) => setUseOauth(v === true)} />
            Sign in with the provider instead (authorize in the browser after installing)
          </label>
        )}
        <ErrorBanner error={install.error} />
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!name.trim() || missingRequired || install.isPending}
            onClick={() => install.mutate()}
          >
            {install.isPending ? "Installing…" : "Install"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// ── Add server: define a server by hand (all four forms create an mcp_server connection) ─────────

const ADD_PANES = [
  ["stdio", "Stdio"],
  ["remote", "Remote"],
  ["openapi", "OpenAPI"],
  ["graphql", "GraphQL"],
] as const;
type AddPane = (typeof ADD_PANES)[number][0];

export function AddServerModal({ onClose }: { onClose: () => void }) {
  const [pane, setPane] = useState<AddPane>("stdio");
  return (
    <Modal title="Add an MCP server" width="max-w-2xl" onClose={onClose}>
      <div className="space-y-3">
        <SegmentedSwitch options={ADD_PANES} value={pane} onChange={setPane} />
        {pane === "stdio" && <StdioForm onDone={onClose} />}
        {pane === "remote" && <RemoteForm onDone={onClose} />}
        {pane === "openapi" && <OpenApiForm onDone={onClose} />}
        {pane === "graphql" && <GraphqlForm onDone={onClose} />}
      </div>
    </Modal>
  );
}

// Reveal-as-dots for the secret env textarea — a textarea has no type="password".
const SECRET_TEXTAREA_STYLE = { WebkitTextSecurity: "disc" } as CSSProperties;

function useConnectionCreated(onDone: () => void) {
  const qc = useQueryClient();
  return (name: string) => {
    notify.success(`Added ${name} — find it in the server list`);
    qc.invalidateQueries({ queryKey: ["connections"] });
    onDone();
  };
}

function StdioForm({ onDone }: { onDone: () => void }) {
  const created = useConnectionCreated(onDone);
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [env, setEnv] = useState("");
  const [secretEnv, setSecretEnv] = useState("");
  const [cwd, setCwd] = useState("");

  const create = useMutation({
    mutationFn: () => {
      const config: Record<string, unknown> = {
        transport: "stdio",
        command: command.trim(),
        args: parseArgs(args),
        env: parseEnv(env) ?? {},
      };
      if (cwd.trim()) config.cwd = cwd.trim();
      const body: CreateConnectionBody = { name: name.trim(), kind: "mcp_server", config };
      const secretMap = parseEnv(secretEnv);
      if (secretMap) {
        // Secret env vars ride the write-only `secret` field as one JSON map behind a single
        // rotatable ref; they enter the subprocess env at step time, never the config row.
        config.auth = { type: "env" };
        body.secret = JSON.stringify(secretMap);
      }
      return api.createConnection(body);
    },
    onSuccess: (c) => created(c.name),
  });

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        A local subprocess in your trust domain. Secret env values are stored encrypted and injected
        at launch — they never appear in the config again.
      </p>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name">
          <Input value={name} placeholder="filesystem" onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Command">
          <Input value={command} placeholder="npx" onChange={(e) => setCommand(e.target.value)} />
        </Field>
      </div>
      <Field label="Args (whitespace-separated)">
        <Input
          value={args}
          placeholder="-y @modelcontextprotocol/server-filesystem /tmp"
          onChange={(e) => setArgs(e.target.value)}
        />
      </Field>
      <Field label="Env (NAME=value, one per line — non-secret)">
        <Textarea
          rows={3}
          className="mono"
          value={env}
          placeholder={"MODEL_PATH=…\nLOG_LEVEL=info"}
          onChange={(e) => setEnv(e.target.value)}
        />
      </Field>
      <Field label="Secret env (NAME=value, one per line — stored encrypted)">
        <Textarea
          rows={3}
          className="mono"
          style={SECRET_TEXTAREA_STYLE}
          value={secretEnv}
          placeholder={"API_KEY=…"}
          onChange={(e) => setSecretEnv(e.target.value)}
        />
      </Field>
      <Field label="Working dir (optional)">
        <Input value={cwd} placeholder="/path/to/cwd" onChange={(e) => setCwd(e.target.value)} />
      </Field>
      <ErrorBanner error={create.error} />
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button
          disabled={!name.trim() || !command.trim() || create.isPending}
          onClick={() => create.mutate()}
        >
          {create.isPending ? "Creating…" : "Create"}
        </Button>
      </div>
    </div>
  );
}

// ── upstream auth (shared by the Remote / OpenAPI / GraphQL forms) ───────────────────────────────

type AuthChoice =
  | "none"
  | "bearer"
  | "api_key"
  | "basic"
  | "headers"
  | "oauth2_client_credentials"
  | "oauth";

const AUTH_LABEL: Record<AuthChoice, string> = {
  none: "None",
  bearer: "Bearer token",
  api_key: "API key header",
  basic: "Basic (username + password)",
  headers: "Header map",
  oauth2_client_credentials: "OAuth2 client credentials",
  oauth: "OAuth sign-in (authorize in the browser)",
};

/** The one auth vocabulary for a remote/generated server's upstream calls. Returns the select +
 * per-type fields as JSX and a `build()` that yields the `auth` config block plus the write-only
 * `secret`. `allowOauth` adds the interactive sign-in option (remote http only). */
function useUpstreamAuth({ allowOauth = false }: { allowOauth?: boolean } = {}) {
  const [choice, setChoice] = useState<AuthChoice>("none");
  const [header, setHeader] = useState("X-API-Key");
  const [username, setUsername] = useState("");
  const [tokenUrl, setTokenUrl] = useState("");
  const [clientId, setClientId] = useState("");
  const [scope, setScope] = useState("");
  const [secret, setSecret] = useState("");
  const [secretLines, setSecretLines] = useState("");

  // If the interactive option disappears (e.g. the transport radio moved off http), fall back.
  useEffect(() => {
    if (!allowOauth && choice === "oauth") setChoice("none");
  }, [allowOauth, choice]);

  const build = (): { auth?: Record<string, unknown>; secret?: string } => {
    switch (choice) {
      case "none":
        return {};
      case "bearer":
        return { auth: { type: "bearer" }, ...(secret ? { secret } : {}) };
      case "api_key":
        return {
          auth: { type: "api_key", header: header.trim() || "X-API-Key" },
          ...(secret ? { secret } : {}),
        };
      case "basic":
        return {
          auth: { type: "basic", username: username.trim() },
          ...(secret ? { secret } : {}),
        };
      case "headers": {
        const map = parseEnv(secretLines);
        return { auth: { type: "headers" }, ...(map ? { secret: JSON.stringify(map) } : {}) };
      }
      case "oauth2_client_credentials":
        return {
          auth: {
            type: "oauth2_client_credentials",
            tokenUrl: tokenUrl.trim(),
            clientId: clientId.trim(),
            ...(scope.trim() ? { scope: scope.trim() } : {}),
          },
          ...(secret ? { secret } : {}),
        };
      case "oauth":
        return { auth: { type: "oauth" } };
    }
  };

  const options: AuthChoice[] = [
    "none",
    "bearer",
    "api_key",
    "basic",
    "headers",
    "oauth2_client_credentials",
    ...(allowOauth ? (["oauth"] as const) : []),
  ];

  const fields = (
    <>
      <Field label="Auth">
        <Select value={choice} onChange={(e) => setChoice(e.target.value as AuthChoice)}>
          {options.map((o) => (
            <option key={o} value={o}>
              {AUTH_LABEL[o]}
            </option>
          ))}
        </Select>
      </Field>
      {choice === "bearer" && (
        <Field label="Token (stored encrypted)">
          <Input
            type="password"
            value={secret}
            placeholder="paste the token…"
            onChange={(e) => setSecret(e.target.value)}
          />
        </Field>
      )}
      {choice === "api_key" && (
        <>
          <Field label="Header name">
            <Input value={header} onChange={(e) => setHeader(e.target.value)} />
          </Field>
          <Field label="Key (stored encrypted)">
            <Input
              type="password"
              value={secret}
              placeholder="paste the key…"
              onChange={(e) => setSecret(e.target.value)}
            />
          </Field>
        </>
      )}
      {choice === "basic" && (
        <>
          <Field label="Username">
            <Input value={username} onChange={(e) => setUsername(e.target.value)} />
          </Field>
          <Field label="Password (stored encrypted)">
            <Input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} />
          </Field>
        </>
      )}
      {choice === "headers" && (
        <Field label="Headers (NAME=value, one per line — stored encrypted)">
          <Textarea
            rows={3}
            className="mono"
            style={SECRET_TEXTAREA_STYLE}
            value={secretLines}
            placeholder={"Authorization=Bearer …\nX-Org-Id=…"}
            onChange={(e) => setSecretLines(e.target.value)}
          />
        </Field>
      )}
      {choice === "oauth2_client_credentials" && (
        <>
          <Field label="Token URL">
            <Input
              value={tokenUrl}
              placeholder="https://auth.example.com/oauth/token"
              onChange={(e) => setTokenUrl(e.target.value)}
            />
          </Field>
          <Field label="Client id">
            <Input value={clientId} onChange={(e) => setClientId(e.target.value)} />
          </Field>
          <Field label="Scope (optional)">
            <Input value={scope} onChange={(e) => setScope(e.target.value)} />
          </Field>
          <Field label="Client secret (stored encrypted)">
            <Input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} />
          </Field>
        </>
      )}
      {choice === "oauth" && (
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          Nothing to paste — create the server, then press{" "}
          <span className="text-foreground">Connect</span> on its row to sign in with the provider.
        </p>
      )}
    </>
  );

  return { choice, fields, build };
}

function RemoteForm({ onDone }: { onDone: () => void }) {
  const created = useConnectionCreated(onDone);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [transport, setTransport] = useState<"http" | "sse">("http");
  const [headers, setHeaders] = useState("");
  const auth = useUpstreamAuth({ allowOauth: transport === "http" });

  const create = useMutation({
    mutationFn: () => {
      const { auth: authBlock, secret } = auth.build();
      const config: Record<string, unknown> = {
        transport,
        url: url.trim(),
        headers: parseEnv(headers) ?? {},
        ...(authBlock ? { auth: authBlock } : {}),
      };
      return api.createConnection({
        name: name.trim(),
        kind: "mcp_server",
        config,
        ...(secret ? { secret } : {}),
      });
    },
    onSuccess: (c) => created(c.name),
  });

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        A remote MCP server reached over HTTP. The auth credential is stored encrypted and turned
        into request headers server-side — it never appears in the config.
      </p>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name">
          <Input value={name} placeholder="github" onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="URL">
          <Input
            value={url}
            placeholder="https://host/mcp"
            onChange={(e) => setUrl(e.target.value)}
          />
        </Field>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          Transport
        </span>
        {(
          [
            ["http", "HTTP"],
            ["sse", "SSE"],
          ] as const
        ).map(([k, label]) => (
          <button
            key={k}
            type="button"
            aria-pressed={transport === k}
            onClick={() => setTransport(k)}
            className={cn(
              "rounded-md border px-2 py-1 text-xs",
              transport === k
                ? "border-blue-500 bg-blue-500/10 text-blue-700 dark:text-blue-200"
                : "border-border text-muted-foreground hover:text-foreground",
            )}
          >
            {label}
          </button>
        ))}
      </div>
      <Field label="Headers (NAME=value, one per line — non-secret)">
        <Textarea
          rows={2}
          className="mono"
          value={headers}
          placeholder={"X-Client=theygent"}
          onChange={(e) => setHeaders(e.target.value)}
        />
      </Field>
      {auth.fields}
      <ErrorBanner error={create.error} />
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button
          disabled={!name.trim() || !url.trim() || create.isPending}
          onClick={() => create.mutate()}
        >
          {create.isPending ? "Creating…" : "Create"}
        </Button>
      </div>
    </div>
  );
}

/** The derived-tools list a generated-server preview returns — rendered identically for the
 * OpenAPI and GraphQL forms so "what will my agent see" reads the same both places. */
function PreviewToolList({ preview }: { preview: GeneratedPreview }) {
  return (
    <div className="space-y-1 rounded-md border p-3">
      <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {preview.tools.length} tool{preview.tools.length === 1 ? "" : "s"} derived
        {preview.url ? (
          <>
            {" "}
            · upstream <span className="mono normal-case">{preview.url}</span>
          </>
        ) : null}
      </p>
      {preview.tools.length === 0 ? (
        <p className="text-xs text-muted-foreground">No tools derived — check the spec.</p>
      ) : (
        <ul className="max-h-40 space-y-1 overflow-auto">
          {preview.tools.map((t) => (
            <li key={t.name} className="text-xs">
              <span className="mono text-foreground">{t.name}</span>
              {t.description && <span className="text-muted-foreground"> — {t.description}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function OpenApiForm({ onDone }: { onDone: () => void }) {
  const created = useConnectionCreated(onDone);
  const [name, setName] = useState("");
  const [specUrl, setSpecUrl] = useState("");
  const [specText, setSpecText] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [preview, setPreview] = useState<GeneratedPreview | null>(null);
  const auth = useUpstreamAuth();

  // A pasted spec must be a JSON object (YAML paste is not supported — the server can fetch
  // YAML from a URL, but the connection stores the parsed document). Returns null when the
  // textarea is empty (the URL path applies instead).
  function pastedSpec(): Record<string, unknown> | null {
    const text = specText.trim();
    if (!text) return null;
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      throw new Error(
        "The pasted spec isn't valid JSON — YAML paste isn't supported; give the spec URL instead.",
      );
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("The pasted spec must be a JSON object.");
    }
    return parsed as Record<string, unknown>;
  }

  const previewMut = useMutation({
    mutationFn: () => {
      const spec = pastedSpec();
      if (!spec && !specUrl.trim()) {
        throw new Error("Give a spec URL or paste the spec JSON first.");
      }
      return api.previewGenerated({
        kind: "openapi",
        ...(spec ? { spec } : { specUrl: specUrl.trim() }),
        ...(baseUrl.trim() ? { url: baseUrl.trim() } : {}),
      });
    },
    onSuccess: (p) => setPreview(p),
  });

  const create = useMutation({
    mutationFn: async () => {
      let spec = pastedSpec();
      if (!spec) {
        // The connection stores the spec INLINE (responses elide it to a small summary), so a
        // URL-only spec is fetched from the browser here. A CORS-blocked or YAML document
        // can't be — the honest fallback is asking for a paste.
        try {
          spec = await api.fetchJsonDocument(specUrl.trim());
        } catch {
          throw new Error(
            "Couldn't fetch the spec from the browser (CORS, or not JSON) — paste the spec JSON instead.",
          );
        }
      }
      const { auth: authBlock, secret } = auth.build();
      const config: Record<string, unknown> = {
        transport: "openapi",
        spec,
        url: baseUrl.trim() || preview?.url || "",
        ...(authBlock ? { auth: authBlock } : {}),
      };
      return api.createConnection({
        name: name.trim(),
        kind: "mcp_server",
        config,
        ...(secret ? { secret } : {}),
      });
    },
    onSuccess: (c) => created(c.name),
  });

  // Any spec/base change invalidates the preview — Create is gated on a CURRENT one.
  const editSpec = (fn: () => void) => {
    fn();
    setPreview(null);
  };

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        Turn a REST API into MCP tools: every operation in the OpenAPI spec becomes a callable tool.
        Preview first — Create is enabled once the spec derives cleanly.
      </p>
      <Field label="Name">
        <Input value={name} placeholder="petstore" onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="Spec URL">
        <Input
          value={specUrl}
          placeholder="https://api.example.com/openapi.json"
          onChange={(e) => editSpec(() => setSpecUrl(e.target.value))}
        />
      </Field>
      <Field label="…or paste the spec (JSON)">
        <Textarea
          rows={4}
          className="mono"
          value={specText}
          placeholder='{ "openapi": "3.1.0", "paths": { … } }'
          onChange={(e) => editSpec(() => setSpecText(e.target.value))}
        />
      </Field>
      <Field label="Base URL (optional — derived from the spec's servers when omitted)">
        <Input
          value={baseUrl}
          placeholder="https://api.example.com"
          onChange={(e) => editSpec(() => setBaseUrl(e.target.value))}
        />
      </Field>
      {auth.fields}
      <ErrorBanner error={previewMut.error ?? create.error} />
      {preview && <PreviewToolList preview={preview} />}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button
          variant="outline"
          disabled={(!specUrl.trim() && !specText.trim()) || previewMut.isPending}
          onClick={() => previewMut.mutate()}
        >
          {previewMut.isPending ? "Previewing…" : "Preview"}
        </Button>
        <Button
          disabled={!name.trim() || !preview || create.isPending}
          onClick={() => create.mutate()}
        >
          {create.isPending ? "Creating…" : "Create"}
        </Button>
      </div>
    </div>
  );
}

function GraphqlForm({ onDone }: { onDone: () => void }) {
  const created = useConnectionCreated(onDone);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [allowMutations, setAllowMutations] = useState(false);
  const [preview, setPreview] = useState<GeneratedPreview | null>(null);
  const auth = useUpstreamAuth();

  const previewMut = useMutation({
    mutationFn: () => api.previewGenerated({ kind: "graphql", url: url.trim(), allowMutations }),
    onSuccess: (p) => setPreview(p),
  });

  const create = useMutation({
    mutationFn: () => {
      const { auth: authBlock, secret } = auth.build();
      const config: Record<string, unknown> = {
        transport: "graphql",
        url: url.trim(),
        allowMutations,
        ...(authBlock ? { auth: authBlock } : {}),
      };
      return api.createConnection({
        name: name.trim(),
        kind: "mcp_server",
        config,
        ...(secret ? { secret } : {}),
      });
    },
    onSuccess: (c) => created(c.name),
  });

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        Turn a GraphQL endpoint into MCP tools by introspection: each query (and, if allowed, each
        mutation) becomes a callable tool.
      </p>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name">
          <Input value={name} placeholder="countries" onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Endpoint URL">
          <Input
            value={url}
            placeholder="https://api.example.com/graphql"
            onChange={(e) => {
              setUrl(e.target.value);
              setPreview(null);
            }}
          />
        </Field>
      </div>
      <label className="flex items-center gap-2 text-sm text-foreground">
        <Switch
          checked={allowMutations}
          onCheckedChange={(v) => {
            setAllowMutations(v === true);
            setPreview(null);
          }}
        />
        Allow mutations (write operations become tools too)
      </label>
      {auth.fields}
      <ErrorBanner error={previewMut.error ?? create.error} />
      {preview && <PreviewToolList preview={preview} />}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button
          variant="outline"
          disabled={!url.trim() || previewMut.isPending}
          onClick={() => previewMut.mutate()}
        >
          {previewMut.isPending ? "Previewing…" : "Preview"}
        </Button>
        <Button
          disabled={!name.trim() || !url.trim() || create.isPending}
          onClick={() => create.mutate()}
        >
          {create.isPending ? "Creating…" : "Create"}
        </Button>
      </div>
    </div>
  );
}
