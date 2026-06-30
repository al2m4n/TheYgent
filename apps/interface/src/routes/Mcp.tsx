// The MCP page — define and manage MCP servers (the user's tools), and test them. MCP servers are
// stdio subprocesses in the USER's trust domain (M7 §3.2 / §10): the `env` you give here is passed
// into the spawned process and never logged with values, never resolved in theygent cloud. Below the
// registry sits the tool tester (M18 §2.6): run a single tool through a throwaway one-node graph and
// see its detections drawn through the shared overlay (the same one a grounding VLM uses).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ToolTester } from "../bench/ToolTester";
import { CategoryBadge, FilterBar } from "../components/Filters";
import { Button, Card, Empty, ErrorBanner, Field, Input, Page, Spinner } from "../components/ui";
import { type McpServerConfig, type McpServerSummary, api } from "../lib/api";
import { countBy, toggle, transportTone } from "../lib/categories";

export function Mcp() {
  return (
    <Page className="space-y-8">
      <div>
        <h1 className="text-lg font-semibold text-slate-100">MCP servers</h1>
        <p className="text-xs text-slate-500">
          Define the external tools your agents can call. Servers run locally in your trust domain.
        </p>
      </div>
      <ServerList />
      <ToolTester />
    </Page>
  );
}

function ServerList() {
  const qc = useQueryClient();
  const servers = useQuery({ queryKey: ["mcpServers"], queryFn: () => api.listMcpServers() });
  const invalidate = () => qc.invalidateQueries({ queryKey: ["mcpServers"] });
  const warm = useMutation({ mutationFn: api.warmMcpServer, onSuccess: invalidate });
  const close = useMutation({ mutationFn: api.closeMcpServer, onSuccess: invalidate });
  const remove = useMutation({ mutationFn: api.deleteMcpServer, onSuccess: invalidate });
  const [adding, setAdding] = useState(false);

  // Filters: by transport (the server's category) and connected/idle state, plus a name search.
  const [transportSel, setTransportSel] = useState<string[]>([]);
  const [statusSel, setStatusSel] = useState<string[]>([]);
  const [q, setQ] = useState("");

  const list = servers.data ?? [];
  const transportCounts = useMemo(() => countBy(list, (s) => s.transport), [list]);
  const statusCounts = useMemo(
    () => countBy(list, (s) => (s.connected ? "connected" : "idle")),
    [list],
  );
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return list.filter((s) => {
      if (transportSel.length && !transportSel.includes(s.transport)) return false;
      const st = s.connected ? "connected" : "idle";
      if (statusSel.length && !statusSel.includes(st)) return false;
      if (needle && !s.name.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [list, transportSel, statusSel, q]);

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

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">Registered</h2>
        <Button variant="primary" onClick={() => setAdding((a) => !a)}>
          {adding ? "Close" : "＋ Define server"}
        </Button>
      </div>

      {adding && (
        <RegisterForm
          onDone={() => {
            setAdding(false);
            invalidate();
          }}
        />
      )}

      <ErrorBanner error={servers.error ?? warm.error ?? close.error ?? remove.error} />
      {servers.isLoading ? (
        <Spinner label="Loading MCP servers…" />
      ) : list.length === 0 ? (
        <Empty>
          No MCP servers yet. Define one above (e.g. a filesystem or YOLO/SAM CV server).
        </Empty>
      ) : (
        <>
          <FilterBar
            search={q}
            onSearch={setQ}
            searchPlaceholder="Search name…"
            facets={[transportFacet, statusFacet]}
            total={list.length}
            shown={filtered.length}
            onClear={() => {
              setTransportSel([]);
              setStatusSel([]);
              setQ("");
            }}
          />
          {filtered.length === 0 ? (
            <Empty>No servers match the current filters.</Empty>
          ) : (
            <div className="space-y-2">
              {filtered.map((s) => (
                <ServerRow
                  key={s.name}
                  server={s}
                  transportSel={transportSel}
                  statusSel={statusSel}
                  onToggleTransport={toggleTransport}
                  onToggleStatus={toggleStatus}
                  onWarm={() => warm.mutate(s.name)}
                  onClose={() => close.mutate(s.name)}
                  onRemove={() => remove.mutate(s.name)}
                />
              ))}
            </div>
          )}
        </>
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
  onWarm,
  onClose,
  onRemove,
}: {
  server: McpServerSummary;
  transportSel: string[];
  statusSel: string[];
  onToggleTransport: (value: string) => void;
  onToggleStatus: (value: string) => void;
  onWarm: () => void;
  onClose: () => void;
  onRemove: () => void;
}) {
  const [showTools, setShowTools] = useState(false);
  const tools = useQuery({
    queryKey: ["mcpTools", server.name],
    queryFn: () => api.getMcpTools(server.name),
    enabled: showTools,
    retry: false,
  });
  const st = server.connected ? "connected" : "idle";
  return (
    <Card className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-slate-100">{server.name}</span>
        <CategoryBadge
          tone={transportTone(server.transport)}
          active={transportSel.includes(server.transport)}
          onClick={() => onToggleTransport(server.transport)}
          title={`Filter by ${server.transport}`}
        >
          {server.transport}
        </CategoryBadge>
        <CategoryBadge
          tone={server.connected ? "green" : "slate"}
          active={statusSel.includes(st)}
          onClick={() => onToggleStatus(st)}
          title={`Filter by ${st}`}
        >
          {st}
        </CategoryBadge>
        <div className="ml-auto flex items-center gap-1">
          <Button onClick={() => setShowTools((v) => !v)}>
            {showTools ? "hide tools" : "tools"}
          </Button>
          <Button onClick={onWarm}>warm</Button>
          <Button onClick={onClose}>close</Button>
          <Button variant="danger" onClick={onRemove}>
            delete
          </Button>
        </div>
      </div>
      {showTools && (
        <div className="border-t border-slate-800 pt-2">
          {tools.isLoading && <Spinner label="Connecting…" />}
          {tools.error && <ErrorBanner error={tools.error} />}
          {tools.data && tools.data.length === 0 && (
            <p className="text-xs text-slate-500">No tools reported.</p>
          )}
          <ul className="space-y-1">
            {tools.data?.map((t) => (
              <li key={t.name} className="text-xs">
                <span className="font-mono text-slate-200">{t.name}</span>
                {t.description && <span className="text-slate-500"> — {t.description}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

// Parse newline/space-separated args, and KEY=value env lines, into the wire shape.
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

function RegisterForm({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [env, setEnv] = useState("");
  const [cwd, setCwd] = useState("");
  const save = useMutation({
    mutationFn: (cfg: { name: string; config: McpServerConfig }) =>
      api.putMcpServer(cfg.name, cfg.config),
    onSuccess: onDone,
  });

  return (
    <Card className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="filesystem" />
        </Field>
        <Field label="Command">
          <Input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="npx" />
        </Field>
      </div>
      <Field label="Args (whitespace-separated)">
        <Input
          value={args}
          onChange={(e) => setArgs(e.target.value)}
          placeholder="-y @modelcontextprotocol/server-filesystem /tmp"
        />
      </Field>
      <Field label="Env (KEY=value per line, stays local)">
        <textarea
          value={env}
          onChange={(e) => setEnv(e.target.value)}
          rows={2}
          placeholder={"API_KEY=…\nMODEL_PATH=…"}
          className="w-full rounded-md border border-slate-700 bg-[var(--c-surface)] px-2.5 py-1.5 font-mono text-sm text-slate-100 outline-none focus:border-blue-500"
        />
      </Field>
      <Field label="Working dir (optional)">
        <Input value={cwd} onChange={(e) => setCwd(e.target.value)} placeholder="/path/to/cwd" />
      </Field>
      <ErrorBanner error={save.error} />
      <div className="flex items-center gap-2">
        <Button
          variant="primary"
          disabled={!name.trim() || !command.trim() || save.isPending}
          onClick={() =>
            save.mutate({
              name: name.trim(),
              config: {
                transport: "stdio",
                command: command.trim(),
                args: parseArgs(args),
                env: parseEnv(env),
                cwd: cwd.trim() || null,
              },
            })
          }
        >
          {save.isPending ? "Saving…" : "Save server"}
        </Button>
      </div>
    </Card>
  );
}
