import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorBanner,
  Field,
  Input,
  Select,
  Spinner,
  Table,
  Td,
  Th,
} from "../components/ui";
import { api } from "../lib/api";
import type { McpServerSummary, McpServerView } from "../lib/types";
import {
  keys,
  useEngines,
  useMcpMutations,
  useMcpServers,
  useMcpTools,
  useModelMutations,
  useModels,
} from "../queries";

export function Registries() {
  const [tab, setTab] = useState<"models" | "mcp">("models");
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-slate-100">Registries</h1>
        <div className="flex rounded-md border border-slate-700 p-0.5">
          {(["models", "mcp"] as const).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setTab(t)}
              className={`rounded px-3 py-1 text-sm uppercase ${
                tab === t ? "bg-slate-700 text-slate-100" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {t === "mcp" ? "MCP servers" : "Models"}
            </button>
          ))}
        </div>
      </div>
      {tab === "models" ? <ModelsPanel /> : <McpPanel />}
    </div>
  );
}

// ── Models (inference plane /admin/models + /admin/engines) ─────────────────

function ModelsPanel() {
  const { data: models, isLoading, error } = useModels();
  const { data: engines } = useEngines();
  const m = useModelMutations();

  const residentCount = engines
    ? Array.isArray(engines.resident)
      ? engines.resident.length
      : Object.keys(engines.resident ?? {}).length
    : 0;

  return (
    <div className="space-y-5">
      {engines && (
        <p className="text-xs text-slate-400">
          Resident engines: <span className="text-slate-200">{residentCount}</span> /{" "}
          {engines.maxResident}
        </p>
      )}
      <ErrorBanner error={error ?? m.register.error ?? m.warm.error ?? m.evict.error} />

      {isLoading ? (
        <Spinner />
      ) : !models || models.length === 0 ? (
        <Empty>No models registered. Register one below.</Empty>
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
            {models.map((model) => (
              <tr key={model.logicalId} className="align-top hover:bg-slate-800/30">
                <Td className="mono text-slate-100">{model.logicalId}</Td>
                <Td>
                  <Badge>{model.binding.binding}</Badge>
                </Td>
                <Td className="mono text-slate-300">{model.binding.model ?? "—"}</Td>
                <Td>
                  {model.state.resident ? (
                    <Badge tone="green">resident{model.state.draining ? " · draining" : ""}</Badge>
                  ) : (
                    <Badge>cold</Badge>
                  )}
                </Td>
                <Td>
                  <CapabilitiesCell logicalId={model.logicalId} />
                </Td>
                <Td>
                  <div className="flex flex-wrap gap-1">
                    <Button onClick={() => m.warm.mutate(model.logicalId)}>warm</Button>
                    <Button onClick={() => m.evict.mutate(model.logicalId)}>evict</Button>
                    <Button variant="danger" onClick={() => m.remove.mutate(model.logicalId)}>
                      delete
                    </Button>
                  </div>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}

      <ModelRegisterForm onSubmit={(logicalId, body) => m.register.mutate({ logicalId, body })} />
    </div>
  );
}

function CapabilitiesCell({ logicalId }: { logicalId: string }) {
  const [show, setShow] = useState(false);
  const { data, isFetching, error } = useQuery({
    queryKey: [...keys.models(), "caps", logicalId],
    queryFn: () => api.getModelCapabilities(logicalId),
    enabled: show,
    retry: false,
  });

  if (!show) {
    return (
      <Button onClick={() => setShow(true)} className="text-xs">
        probe
      </Button>
    );
  }
  if (isFetching) return <span className="text-xs text-slate-500">probing…</span>;
  if (error) return <span className="text-xs text-rose-400">{(error as Error).message}</span>;
  if (!data) return <span className="text-xs text-slate-500">—</span>;
  return (
    <div className="space-y-0.5 text-xs text-slate-300">
      <div>maxContext: {data.maxContext ?? "?"}</div>
      <div className="flex gap-1">
        {data.approximate && <Badge tone="red">approximate</Badge>}
        {data.tools && <Badge tone="green">tools</Badge>}
        {data.structuredOutput && <Badge tone="green">structured</Badge>}
        {data.vision && <Badge tone="green">vision</Badge>}
      </div>
    </div>
  );
}

function ModelRegisterForm({
  onSubmit,
}: {
  onSubmit: (logicalId: string, body: unknown) => void;
}) {
  const [logicalId, setLogicalId] = useState("");
  const [binding, setBinding] = useState("mlx");
  const [source, setSource] = useState("hf");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [credentialRef, setCredentialRef] = useState("");

  const reachable = binding === "openai-compatible";

  function submit() {
    if (!logicalId || !model) return;
    const body = reachable
      ? {
          binding,
          model,
          baseUrl,
          ...(credentialRef ? { credentialRef } : {}),
        }
      : { binding, source, model };
    onSubmit(logicalId, body);
  }

  return (
    <Card className="space-y-3 p-4">
      <h2 className="text-sm font-semibold text-slate-200">Register a model</h2>
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
            <option value="mlx">mlx</option>
            <option value="vllm">vllm</option>
            <option value="llamacpp">llamacpp</option>
            <option value="openai-compatible">openai-compatible</option>
          </Select>
        </Field>
        <Field label="Model (weights / upstream id)">
          <Input
            value={model}
            placeholder="mlx-community/Qwen2.5-0.5B-4bit"
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
            <Field label="Credential ref (resolved locally only)">
              <Input
                value={credentialRef}
                placeholder="env:OPENAI_API_KEY"
                onChange={(e) => setCredentialRef(e.target.value)}
              />
            </Field>
          </>
        ) : (
          <Field label="Source">
            <Select value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="hf">hf</option>
              <option value="local-path">local-path</option>
              <option value="url">url</option>
            </Select>
          </Field>
        )}
      </div>
      <Button variant="primary" onClick={submit}>
        Register
      </Button>
    </Card>
  );
}

// ── MCP servers (control-plane /admin/mcp/*) ────────────────────────────────

function McpPanel() {
  const { data: servers, isLoading, error } = useMcpServers();
  const m = useMcpMutations();

  // GET /admin/mcp/servers returns a LIST of {name, transport, connected} summaries.
  const list = servers ?? [];

  return (
    <div className="space-y-5">
      <ErrorBanner error={error ?? m.register.error ?? m.warm.error ?? m.close.error} />
      {isLoading ? (
        <Spinner />
      ) : list.length === 0 ? (
        <Empty>No MCP servers registered. Register a stdio server below.</Empty>
      ) : (
        <div className="space-y-3">
          {list.map((summary) => (
            <McpServerRow key={summary.name} summary={summary} />
          ))}
        </div>
      )}
      <McpRegisterForm onSubmit={(name, body) => m.register.mutate({ name, body })} />
    </div>
  );
}

function McpServerRow({ summary }: { summary: McpServerSummary }) {
  const name = summary.name;
  const m = useMcpMutations();
  // The summary already carries name/transport/connected; fetch the detail for command/args/env.
  const { data: server } = useQuery<McpServerView>({
    queryKey: keys.mcpServer(name),
    queryFn: () => api.getMcpServer(name),
  });
  const [showTools, setShowTools] = useState(false);
  const tools = useMcpTools(name, showTools);

  return (
    <Card className="space-y-2 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="mono font-semibold text-slate-100">{name}</span>
        <Badge>{summary.transport}</Badge>
        {summary.connected ? <Badge tone="green">connected</Badge> : <Badge>idle</Badge>}
        <div className="ml-auto flex gap-1">
          <Button onClick={() => setShowTools((s) => !s)}>
            {showTools ? "hide tools" : "tools"}
          </Button>
          <Button onClick={() => m.warm.mutate(name)}>warm</Button>
          <Button onClick={() => m.close.mutate(name)}>close</Button>
          <Button variant="danger" onClick={() => m.remove.mutate(name)}>
            delete
          </Button>
        </div>
      </div>
      {server && (
        <div className="mono text-xs text-slate-400">
          {server.command} {server.args.join(" ")}
          {server.envKeys.length > 0 && (
            <span className="ml-2 text-slate-500">env: {server.envKeys.join(", ")}</span>
          )}
        </div>
      )}
      {showTools && (
        <div className="rounded border border-slate-800 p-2 text-xs">
          {tools.isFetching ? (
            <span className="text-slate-500">probing…</span>
          ) : tools.error ? (
            <span className="text-rose-400">{(tools.error as Error).message}</span>
          ) : tools.data && tools.data.length > 0 ? (
            <ul className="space-y-1">
              {tools.data.map((t) => (
                <li key={t.name}>
                  <span className="mono text-slate-200">{t.name}</span>
                  {t.description && <span className="text-slate-500"> — {t.description}</span>}
                </li>
              ))}
            </ul>
          ) : (
            <span className="text-slate-500">no tools reported</span>
          )}
        </div>
      )}
    </Card>
  );
}

function McpRegisterForm({ onSubmit }: { onSubmit: (name: string, body: unknown) => void }) {
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [envKeys, setEnvKeys] = useState("");

  function submit() {
    if (!name || !command) return;
    // Sovereignty (§10): we send env KEYS only with blank values — the user fills real values
    // in their own trust domain. The form never transmits secret values.
    const env: Record<string, string> = {};
    for (const k of envKeys
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean))
      env[k] = "";
    onSubmit(name, {
      transport: "stdio",
      command,
      args: args
        .split(" ")
        .map((s) => s.trim())
        .filter(Boolean),
      ...(Object.keys(env).length ? { env } : {}),
    });
  }

  return (
    <Card className="space-y-3 p-4">
      <h2 className="text-sm font-semibold text-slate-200">Register a stdio MCP server</h2>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Name">
          <Input value={name} placeholder="filesystem" onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Command">
          <Input value={command} placeholder="npx" onChange={(e) => setCommand(e.target.value)} />
        </Field>
        <Field label="Args (space-separated)">
          <Input
            value={args}
            placeholder="-y @modelcontextprotocol/server-filesystem /tmp"
            onChange={(e) => setArgs(e.target.value)}
          />
        </Field>
        <Field label="Env keys (comma-separated, values blank)">
          <Input
            value={envKeys}
            placeholder="GITHUB_TOKEN"
            onChange={(e) => setEnvKeys(e.target.value)}
          />
        </Field>
      </div>
      <Button variant="primary" onClick={submit}>
        Register
      </Button>
    </Card>
  );
}
