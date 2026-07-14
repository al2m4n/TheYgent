// Bulk-add MCP tools: pick a server (or an mcp_server connection), tick the tools it exposes, and
// land one configured `mcp_tool` node per tick — wired straight into the target llm's tools port
// when opened from an llm. One dialog replaces the per-tool create → configure → drag cycle.

import { useQuery } from "@tanstack/react-query";
import type { IRDocument } from "@theygent/ir-types";
import { useMemo, useState } from "react";
import { addMcpToolNodes } from "../adapter";
import { api } from "../lib/api";
import { Badge, Button, Field, Input, Modal, Select } from "./ui";
import { Checkbox } from "./ui/checkbox";

// Beyond this many tools, surface the cost note — every wired tool rides along in the prompt on
// each call, so a very large set spends tokens and dilutes the model's tool choice.
const MANY_TOOLS = 12;

interface Props {
  ir: IRDocument;
  onChange: (ir: IRDocument) => void;
  onClose: () => void;
  /** An llm node id — every added tool is wired into its tool-role in-port. */
  attachTo?: string;
  /** Position anchor for the spawned grid (defaults to `attachTo`). */
  near?: string;
  initialServer?: string;
  initialConnection?: string;
  /** Fix the source to the initial server/connection (opened from an mcp_tool node). */
  lockSource?: boolean;
}

export function AddMcpToolsDialog({
  ir,
  onChange,
  onClose,
  attachTo,
  near,
  initialServer,
  initialConnection,
  lockSource = false,
}: Props) {
  const [server, setServer] = useState(initialServer);
  const [connection, setConnection] = useState(initialConnection);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");

  const servers = useQuery({
    queryKey: ["mcpServers"],
    queryFn: api.listMcpServers,
    enabled: !lockSource,
    retry: false,
    staleTime: 30_000,
  });
  const connections = useQuery({
    queryKey: ["connections"],
    queryFn: api.listConnections,
    enabled: !lockSource,
    retry: false,
    staleTime: 30_000,
  });
  const source = connection || server;
  const tools = useQuery({
    queryKey: connection ? ["mcpToolsConn", connection] : ["mcpTools", server],
    queryFn: () =>
      connection ? api.getConnectionMcpTools(connection) : api.getMcpTools(server as string),
    enabled: Boolean(source),
    retry: false,
    staleTime: 30_000,
  });

  // Tools that already have a node for THIS source (tool name → node id) — re-adding must reuse,
  // never duplicate. `wiredToTarget` narrows "done" to the target llm: an existing node feeding a
  // DIFFERENT llm stays selectable, and adding it just wires the existing node here too.
  const existingByTool = useMemo(() => {
    const m = new Map<string, string>();
    for (const n of ir.nodes ?? []) {
      if (n.type !== "mcp_tool") continue;
      const cfg = (n.config ?? {}) as Record<string, unknown>;
      const match = server
        ? cfg.server === server
        : connection
          ? cfg.connection === connection
          : false;
      if (match && typeof cfg.tool === "string") m.set(cfg.tool, n.id);
    }
    return m;
  }, [ir, server, connection]);
  const wiredToTarget = useMemo(() => {
    if (!attachTo) return new Set<string>();
    return new Set(
      (ir.edges ?? [])
        .filter((e) => (e.channel ?? "data") === "tool" && e.target === attachTo)
        .map((e) => e.source),
    );
  }, [ir, attachTo]);

  const q = search.trim().toLowerCase();
  const rows = (tools.data ?? [])
    .filter(
      (t) =>
        !q || t.name.toLowerCase().includes(q) || (t.description ?? "").toLowerCase().includes(q),
    )
    .map((t) => {
      const nodeId = existingByTool.get(t.name);
      const done = nodeId ? (attachTo ? wiredToTarget.has(nodeId) : true) : false;
      return { name: t.name, description: t.description ?? null, onCanvas: Boolean(nodeId), done };
    });
  const selectable = rows.filter((r) => !r.done);
  const allSelected = selectable.length > 0 && selectable.every((r) => selected.has(r.name));

  const toggle = (name: string, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(name);
      else next.delete(name);
      return next;
    });
  const toggleAll = (on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      for (const r of selectable) {
        if (on) next.add(r.name);
        else next.delete(r.name);
      }
      return next;
    });
  const pickSource = (next: { server?: string; connection?: string }) => {
    setServer(next.server);
    setConnection(next.connection);
    setSelected(new Set());
  };

  // Preserve the server's tool order in the batch (node ids and layout follow it).
  const picked = (tools.data ?? []).map((t) => t.name).filter((n) => selected.has(n));
  const add = () => {
    if (picked.length === 0) return;
    const { ir: next } = addMcpToolNodes(ir, {
      server: connection ? undefined : server,
      connection: connection || undefined,
      tools: picked,
      attachTo,
      near,
    });
    onChange(next);
    onClose();
  };

  const mcpConns = (connections.data ?? []).filter((c) => c.kind === "mcp_server");

  return (
    <Modal title="Add MCP tools" onClose={onClose} width="max-w-lg">
      <div className="space-y-3">
        {lockSource ? (
          <p className="text-xs text-muted-foreground">
            Tools exposed by <span className="mono text-foreground">{server ?? connection}</span>
          </p>
        ) : (
          <div className="grid grid-cols-2 gap-2">
            <Field label="server">
              <Select
                value={server ?? ""}
                onChange={(e) => pickSource({ server: e.target.value || undefined })}
              >
                <option value="">— pick a server —</option>
                {(servers.data ?? []).map((s) => (
                  <option key={s.name} value={s.name}>
                    {s.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="or a connection (mcp server)">
              <Select
                value={connection ?? ""}
                onChange={(e) => pickSource({ connection: e.target.value || undefined })}
              >
                <option value="">—</option>
                {mcpConns.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
        )}

        {!source ? (
          <p className="text-xs text-muted-foreground">Pick a server to list its tools.</p>
        ) : tools.isLoading ? (
          <p className="text-xs text-muted-foreground">loading tools…</p>
        ) : tools.isError ? (
          <p className="text-xs text-muted-foreground">
            Couldn't list this server's tools — it may be unreachable. You can still add a single
            tool by name from the node inspector.
          </p>
        ) : rows.length === 0 && !q ? (
          <p className="text-xs text-muted-foreground">This server reports no tools.</p>
        ) : (
          <>
            <div className="flex items-center gap-3">
              <Input
                className="h-7 text-xs"
                placeholder="Search tools…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <label className="flex shrink-0 cursor-pointer items-center gap-1.5 text-[11px] text-muted-foreground">
                <Checkbox
                  aria-label="Select all"
                  checked={allSelected}
                  disabled={selectable.length === 0}
                  onCheckedChange={(v) => toggleAll(v === true)}
                />
                Select all ({selectable.length})
              </label>
            </div>

            <div className="max-h-72 overflow-y-auto rounded-md border border-border">
              {rows.length === 0 ? (
                <p className="px-2.5 py-2 text-xs text-muted-foreground">No tools match.</p>
              ) : (
                rows.map((r) => (
                  <label
                    key={r.name}
                    className={`flex items-start gap-2 border-b border-border/60 px-2.5 py-1.5 last:border-b-0 ${
                      r.done ? "opacity-60" : "cursor-pointer hover:bg-muted/50"
                    }`}
                  >
                    <Checkbox
                      className="mt-0.5"
                      aria-label={r.name}
                      checked={r.done || selected.has(r.name)}
                      disabled={r.done}
                      onCheckedChange={(v) => toggle(r.name, v === true)}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="mono block truncate text-xs">{r.name}</span>
                      {r.description && (
                        <span className="block truncate text-[11px] text-muted-foreground">
                          {r.description}
                        </span>
                      )}
                    </span>
                    {r.done ? (
                      <Badge>{attachTo ? "wired" : "on canvas"}</Badge>
                    ) : r.onCanvas ? (
                      <Badge>on canvas</Badge>
                    ) : null}
                  </label>
                ))
              )}
            </div>

            {picked.length > MANY_TOOLS && (
              <p className="text-[11px] leading-relaxed text-amber-700 dark:text-amber-300">
                {picked.length} tools is a lot — every wired tool is described to the model on each
                call, which costs tokens and can dilute its tool choice. Consider curating.
              </p>
            )}

            <div className="flex items-center justify-end gap-2 pt-1">
              <Button onClick={onClose}>Cancel</Button>
              <Button variant="primary" disabled={picked.length === 0} onClick={add}>
                Add {picked.length || ""} tool{picked.length === 1 ? "" : "s"}
              </Button>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}
