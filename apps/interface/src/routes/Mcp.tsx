// The MCP page — define and manage MCP servers (the user's tools), and test them. MCP servers are
// stdio subprocesses in the user's trust domain: the `env` you give here is passed into the spawned
// process and never logged with values, never resolved in theygent cloud. Below the registry sits
// the tool tester: run a single tool through a throwaway one-node graph and see its detections drawn
// through the shared overlay (the same one a grounding VLM uses).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Server } from "lucide-react";
import { useMemo, useState } from "react";
import { ToolTester } from "../bench/ToolTester";
import { CategoryBadge, FilterBar } from "../components/Filters";
import { ConfirmDialog, ErrorBanner, Page, SectionHeading, Spinner } from "../components/ui";
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
import { Textarea } from "../components/ui/textarea";
import { type McpServerConfig, type McpServerSummary, api } from "../lib/api";
import { countBy, toggle, transportTone } from "../lib/categories";

export function Mcp() {
  return (
    <Page className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-foreground">MCP servers</h1>
        <p className="text-xs text-muted-foreground">
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
  // Deleting a server is irreversible (its command/args/env config is never redisplayed), so it
  // goes through the shared confirmation dialog instead of firing on the row button.
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null);

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
        <SectionHeading>Registered</SectionHeading>
        <Button onClick={() => setAdding((a) => !a)}>
          {adding ? (
            "Close"
          ) : (
            <>
              <Plus size={14} /> Define server
            </>
          )}
        </Button>
      </div>

      {adding && (
        <RegisterForm
          onDone={() => {
            setAdding(false);
            invalidate();
          }}
          onCancel={() => setAdding(false)}
        />
      )}

      <ErrorBanner error={servers.error ?? warm.error ?? close.error ?? remove.error} />
      {servers.isLoading ? (
        <Spinner label="Loading MCP servers…" />
      ) : list.length === 0 ? (
        <Empty className="border py-10">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <Server />
            </EmptyMedia>
            <EmptyTitle>No MCP servers yet</EmptyTitle>
            <EmptyDescription>
              Define one above (e.g. a filesystem or YOLO/SAM CV server).
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
            total={list.length}
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
                  key={s.name}
                  server={s}
                  transportSel={transportSel}
                  statusSel={statusSel}
                  onToggleTransport={toggleTransport}
                  onToggleStatus={toggleStatus}
                  onWarm={() => warm.mutate(s.name)}
                  onClose={() => close.mutate(s.name)}
                  onRemove={() => setConfirmRemove(s.name)}
                  warming={warm.isPending && warm.variables === s.name}
                  closing={close.isPending && close.variables === s.name}
                  removing={remove.isPending && remove.variables === s.name}
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
              Delete <span className="font-medium text-foreground">{confirmRemove}</span>? Its
              command, args, and env config cannot be recovered.
            </>
          }
          onConfirm={() => {
            remove.mutate(confirmRemove);
            setConfirmRemove(null);
          }}
          onCancel={() => setConfirmRemove(null)}
        />
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
  warming,
  closing,
  removing,
}: {
  server: McpServerSummary;
  transportSel: string[];
  statusSel: string[];
  onToggleTransport: (value: string) => void;
  onToggleStatus: (value: string) => void;
  onWarm: () => void;
  onClose: () => void;
  onRemove: () => void;
  warming: boolean;
  closing: boolean;
  removing: boolean;
}) {
  const [showTools, setShowTools] = useState(false);
  const tools = useQuery({
    queryKey: ["mcpTools", server.name],
    queryFn: () => api.getMcpTools(server.name),
    enabled: showTools,
    retry: false,
  });
  const st = server.connected ? "connected" : "idle";
  // While any action on this row is in flight, disable all three so warm/close/delete can't race.
  const busy = warming || closing || removing;
  return (
    <Item variant="outline" className="bg-card">
      <ItemMedia variant="icon">
        <Server />
      </ItemMedia>
      <ItemContent>
        <ItemTitle>{server.name}</ItemTitle>
        <div className="flex flex-wrap items-center gap-1.5">
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
        </div>
      </ItemContent>
      <ItemActions>
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
          title="Start the server process and connect"
        >
          {warming ? "Warming…" : "Warm"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={onClose}
          disabled={busy}
          title="Stop the server process (config is kept)"
        >
          {closing ? "Closing…" : "Close"}
        </Button>
        <Button variant="destructive" size="sm" onClick={onRemove} disabled={busy}>
          {removing ? "Deleting…" : "Delete"}
        </Button>
      </ItemActions>
      {showTools && (
        <ItemFooter className="flex-col items-stretch justify-start gap-1 border-t pt-2">
          {tools.isLoading && <Spinner label="Connecting…" />}
          {tools.error && <ErrorBanner error={tools.error} />}
          {tools.data && tools.data.length === 0 && (
            <p className="text-xs text-muted-foreground">No tools reported.</p>
          )}
          <ul className="space-y-1">
            {tools.data?.map((t) => (
              <li key={t.name} className="text-xs">
                <span className="mono text-foreground">{t.name}</span>
                {t.description && <span className="text-muted-foreground"> — {t.description}</span>}
              </li>
            ))}
          </ul>
        </ItemFooter>
      )}
    </Item>
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

function RegisterForm({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
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
    <Card size="sm">
      <CardHeader className="border-b">
        <CardTitle>Define server</CardTitle>
        <CardDescription>
          The server runs as a local subprocess; its config stays on this machine.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <FieldGroup className="gap-4">
          <div className="grid grid-cols-2 gap-4">
            <Field>
              <FieldLabel htmlFor="mcp-name">Name</FieldLabel>
              <Input
                id="mcp-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="filesystem"
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="mcp-command">Command</FieldLabel>
              <Input
                id="mcp-command"
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder="npx"
              />
            </Field>
          </div>
          <Field>
            <FieldLabel htmlFor="mcp-args">Args</FieldLabel>
            <Input
              id="mcp-args"
              value={args}
              onChange={(e) => setArgs(e.target.value)}
              placeholder="-y @modelcontextprotocol/server-filesystem /tmp"
            />
            <FieldDescription>Whitespace-separated.</FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor="mcp-env">Env</FieldLabel>
            <Textarea
              id="mcp-env"
              value={env}
              onChange={(e) => setEnv(e.target.value)}
              placeholder={"API_KEY=…\nMODEL_PATH=…"}
              className="mono"
            />
            <FieldDescription>
              One KEY=value per line — passed into the spawned process, values never logged.
            </FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor="mcp-cwd">Working dir (optional)</FieldLabel>
            <Input
              id="mcp-cwd"
              value={cwd}
              onChange={(e) => setCwd(e.target.value)}
              placeholder="/path/to/cwd"
            />
          </Field>
        </FieldGroup>
        <ErrorBanner error={save.error} />
      </CardContent>
      <CardFooter className="justify-end gap-2">
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button
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
      </CardFooter>
    </Card>
  );
}
