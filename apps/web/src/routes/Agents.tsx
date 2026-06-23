// The Agents view (M11 §5) — a thin, additive read over the new /agents/* endpoints. It does NOT
// reshape any backend contract (M8 §0): it lists saved agents + versions and invokes one by
// reference through the existing run path. The IR is still authored as JSON in Compose ("Save as
// agent" lives there); this view runs and inspects what was saved. No graph canvas, no design work.

import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useState } from "react";
import {
  Button,
  Card,
  Empty,
  ErrorBanner,
  Field,
  Spinner,
  Table,
  Td,
  Textarea,
  Th,
} from "../components/ui";
import { ApiError } from "../lib/api";
import { relativeTime, shortId } from "../lib/format";
import { startLiveRun } from "../lib/live";
import { useAgent, useAgents } from "../queries";

export function AgentsList() {
  const { data: agents, isLoading, error } = useAgents();

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-slate-100">Agents</h1>
        <p className="text-xs text-slate-500">
          Saved, versioned agents · invoke by reference (no IR paste). Save one from{" "}
          <Link to="/compose" className="text-indigo-400">
            Compose
          </Link>{" "}
          (graph mode).
        </p>
      </div>

      <ErrorBanner error={error} />

      {isLoading ? (
        <Spinner />
      ) : !agents || agents.length === 0 ? (
        <Empty>
          No saved agents yet.{" "}
          <Link to="/compose" className="text-indigo-400">
            Compose a graph → Save as agent →
          </Link>
        </Empty>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Name</Th>
              <Th>Agent id</Th>
              <Th>Latest version</Th>
              <Th>Content hash</Th>
              <Th>Versions</Th>
              <Th>Created</Th>
            </tr>
          </thead>
          <tbody>
            {agents.map((a) => (
              <tr key={a.id} className="hover:bg-slate-800/30">
                <Td>
                  <Link
                    to="/agents/$agentId"
                    params={{ agentId: a.id }}
                    className="text-indigo-400 hover:text-indigo-300"
                  >
                    {a.name}
                  </Link>
                </Td>
                <Td className="mono text-slate-400">{shortId(a.id, 14)}</Td>
                <Td className="mono text-slate-300">{a.latest_version ?? "—"}</Td>
                <Td className="mono text-slate-500">
                  {a.latest_content_hash ? shortId(a.latest_content_hash, 17) : "—"}
                </Td>
                <Td className="text-slate-400">{a.version_count}</Td>
                <Td className="text-slate-400">{relativeTime(a.created_at)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </div>
  );
}

export function AgentDetail() {
  const { agentId } = useParams({ from: "/agents/$agentId" });
  const { data: agent, isLoading, error } = useAgent(agentId);
  const navigate = useNavigate();

  const [input, setInput] = useState("");
  const [version, setVersion] = useState(""); // "" = latest
  const [submitting, setSubmitting] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  async function run() {
    setRunError(null);
    setSubmitting(true);
    try {
      // The input may be a plain string OR a JSON object (a multi-input agent drills $in.in.<field>).
      // Try to parse it as JSON; fall back to the raw string — so both agent shapes Just Work.
      let parsed: unknown = input;
      try {
        parsed = JSON.parse(input);
      } catch {
        parsed = input;
      }
      const body: Record<string, unknown> = { input: parsed, stream: true };
      if (version) body.version = version;
      const runId = await startLiveRun(`/agents/${agentId}/runs`, body);
      navigate({ to: "/runs/$runId", params: { runId } });
    } catch (e) {
      setRunError(e instanceof ApiError ? `${e.code}: ${e.message}` : String((e as Error).message));
    } finally {
      setSubmitting(false);
    }
  }

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBanner error={error} />;
  if (!agent) return <Empty>Agent not found.</Empty>;

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">{agent.name}</h1>
          <p className="mono text-xs text-slate-500">{agent.id}</p>
        </div>
        <Link to="/agents" className="text-sm text-slate-400 hover:text-slate-200">
          ← all agents
        </Link>
      </div>

      <Card className="space-y-4 p-4">
        <h2 className="text-sm font-semibold text-slate-200">Run this agent</h2>
        <ErrorBanner error={runError} />
        <Field label="Input (plain text, or JSON for a multi-input agent)">
          <Textarea
            rows={3}
            value={input}
            placeholder='e.g. "name three EU capitals" or {"path": "...", "question": "..."}'
            onChange={(e) => setInput(e.target.value)}
          />
        </Field>
        <Field label="Version">
          <select
            className="w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-indigo-500"
            value={version}
            onChange={(e) => setVersion(e.target.value)}
          >
            <option value="">latest ({agent.versions[0]?.version ?? "none"})</option>
            {agent.versions.map((v) => (
              <option key={v.version} value={v.version}>
                {v.version} · {shortId(v.content_hash, 14)}
              </option>
            ))}
          </select>
        </Field>
        <Button
          variant="primary"
          disabled={submitting || agent.versions.length === 0}
          onClick={run}
        >
          {submitting ? "Starting…" : "Run & stream"}
        </Button>
      </Card>

      <Card className="p-4">
        <h2 className="mb-2 text-sm font-semibold text-slate-200">Versions (newest first)</h2>
        <Table>
          <thead>
            <tr>
              <Th>Version</Th>
              <Th>Seq</Th>
              <Th>Content hash</Th>
              <Th>Created</Th>
            </tr>
          </thead>
          <tbody>
            {agent.versions.map((v) => (
              <tr key={v.version}>
                <Td className="mono text-slate-200">{v.version}</Td>
                <Td className="text-slate-400">{v.seq}</Td>
                <Td className="mono text-slate-500">{shortId(v.content_hash, 24)}</Td>
                <Td className="text-slate-400">{relativeTime(v.created_at)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>
    </div>
  );
}
