// API access — the per-agent dialog opened from an Agents card/row. Everything a caller outside
// the interface needs to run THIS published agent over HTTP: the endpoints (interactive run,
// unattended invoke, durable run), what credential opens each one, a copy-able curl for each, and
// any triggers already deployed for the agent. The dialog only READS — it assembles URLs from the
// agent id and `controlPlaneUrl()` client-side; there is no backend surface behind it.
//
// The example request body is derived from the published IR: a graph that drills `$in.<port>.<field>`
// on a node fed directly by the input boundary takes an OBJECT input with those fields (the
// port-first `$in` grammar makes this unambiguous); anything else gets a plain-string example.

import { useQuery } from "@tanstack/react-query";
import type { IRDocument } from "@theygent/ir-types";
import { Check, Copy } from "lucide-react";
import { useEffect, useState } from "react";
import { type AgentSummary, type TriggerRecord, api, controlPlaneUrl } from "../lib/api";
import { isDurableOnly } from "../lib/durable";
import { Modal, Spinner } from "./ui";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";

/** Best-effort example input for a published agent, from its IR. Object-shaped when any node fed
 * directly by the input boundary drills `$in.<fed port>.<field>` (those fields ARE the run-input
 * fields); a plain string otherwise. Purely advisory — the server never sees this.
 *
 * Two token positions exist and their field grammars differ, so two regexes scan the stringified
 * config: a whole-string ref (`"$in.in.user-id"`) allows any dot-free field name and runs to the
 * next path dot or the closing quote, while an inline template token embedded in prose is
 * word-chars only — exactly the walker's template tokenizer. The lookbehind keeps the inline
 * pattern off whole-string refs, where it would truncate `user-id` to a field the agent never
 * reads. */
export function deriveExampleInput(ir: IRDocument): string | Record<string, string> {
  const nodes = ir.nodes ?? [];
  const inputNode = nodes.find((n) => n.type === "input");
  if (!inputNode) return "Hello!";
  const fields = new Set<string>();
  for (const edge of ir.edges ?? []) {
    if (edge.source !== inputNode.id) continue;
    const target = nodes.find((n) => n.id === edge.target);
    if (!target) continue;
    const port = (edge.targetHandle ?? "in").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const config = JSON.stringify(target.config ?? {});
    const wholeString = new RegExp(`"\\$in\\.${port}\\.([^".\\s\\\\]+)`, "g");
    const inline = new RegExp(`(?<!")\\$in\\.${port}\\.([A-Za-z0-9_]+)`, "g");
    for (const match of config.matchAll(wholeString)) fields.add(match[1]);
    for (const match of config.matchAll(inline)) fields.add(match[1]);
  }
  if (fields.size === 0) return "Hello!";
  return Object.fromEntries([...fields].map((f) => [f, `<${f}>`]));
}

function runBody(input: string | Record<string, string>): string {
  return JSON.stringify({ input, stream: false });
}

function runCurl(base: string, path: string, input: string | Record<string, string>): string {
  return [
    `curl -X POST '${base}${path}' \\`,
    `  -H 'Authorization: Bearer tyk_YOUR_API_KEY' \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -d '${runBody(input)}'`,
  ].join("\n");
}

function webhookCurl(base: string, triggerId: string): string {
  return [
    `SECRET='your-signing-secret'  # config.secret set when the trigger was created`,
    `BODY='{"text": "hello"}'`,
    `SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')`,
    "",
    `curl -X POST '${base}/hooks/${triggerId}' \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -H "X-Theygent-Signature: sha256=$SIG" \\`,
    `  -d "$BODY"`,
  ].join("\n");
}

// A copy-able code block: the snippet in a scrollable <pre>, a Copy button that flips to a check
// for a moment. The clipboard call is optional-chained — jsdom and non-secure contexts lack it.
function Snippet({ label, code }: { label: string; code: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1400);
    return () => clearTimeout(t);
  }, [copied]);
  return (
    <div className="relative rounded-md border bg-muted/50">
      <pre className="mono overflow-x-auto p-2.5 pr-10 text-[11px] leading-relaxed">{code}</pre>
      <Button
        size="icon-sm"
        variant="ghost"
        className="absolute top-1 right-1"
        aria-label={`Copy ${label}`}
        title={`Copy ${label}`}
        onClick={() => {
          void navigator.clipboard?.writeText(code).then(() => setCopied(true));
        }}
      >
        {copied ? <Check /> : <Copy />}
      </Button>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h3 className="text-xs font-semibold text-foreground">{title}</h3>
      {children}
    </section>
  );
}

const NOTE = "text-xs text-muted-foreground";

export function ApiAccessModal({ agent, onClose }: { agent: AgentSummary; onClose: () => void }) {
  const base = controlPlaneUrl();
  const hasVersion = Boolean(agent.latest_version);

  // Same key as the card's graph preview, so the IR is usually already cached. The IR decides
  // WHICH run sections render (a durable-only agent must not be shown run/invoke curls the server
  // rejects with 400 durable_required), so the run sections wait for the fetch to settle instead
  // of painting the interactive branch and flipping. A failed fetch falls back to the interactive
  // branch — the most common shape — behind a visible caveat, never silently.
  const stored = useQuery({
    queryKey: ["agentVersion", agent.id, agent.latest_version],
    queryFn: () => api.getAgentVersion(agent.id, agent.latest_version as string),
    enabled: hasVersion,
    staleTime: 5 * 60 * 1000,
  });
  const irPending = hasVersion && stored.isPending;
  const ir = stored.data?.ir as IRDocument | undefined;
  const example = ir ? deriveExampleInput(ir) : "Hello!";
  const durableOnly = ir ? isDurableOnly(ir) : false;
  const hasHuman = (ir?.nodes ?? []).some((n) => n.type === "human");

  // Listing triggers needs an editor-or-above bearer; a viewer's 403 just hides the section —
  // the rest of the dialog is useful to every role.
  const triggers = useQuery({
    queryKey: ["triggers"],
    queryFn: api.listTriggers,
    retry: false,
  });
  const agentTriggers = (triggers.data ?? []).filter((t) => t.agent_id === agent.id);

  const idPath = encodeURIComponent(agent.id);

  return (
    <Modal title={`API access · ${agent.name}`} width="max-w-2xl" onClose={onClose}>
      <div className="space-y-4">
        <p className={NOTE}>
          The run and poll calls below authenticate with{" "}
          <span className="mono">Authorization: Bearer &lt;token&gt;</span> — mint an API key (
          <span className="mono">tyk_…</span>) from <strong>Profile → API keys</strong>; running an
          agent needs no particular role. Webhook deliveries are the exception: they carry a
          per-trigger HMAC signature instead of a bearer. Base URL:{" "}
          <span className="mono">{base}</span>
        </p>

        {stored.isError && (
          <p className={NOTE}>
            The published graph could not be loaded, so the examples use a generic input and assume
            the interactive run path — the server re-checks everything on the first call.
          </p>
        )}

        {irPending ? (
          <Spinner />
        ) : durableOnly ? (
          <Section title="Run (durable only)">
            <p className={NOTE}>
              This agent contains a <span className="mono">human / subgraph / loop / map</span>{" "}
              node, so it runs only on the durable runtime — the interactive and invoke endpoints
              reject it with <span className="mono">400 durable_required</span>. The server must run
              with <span className="mono">THEYGENT_DURABLE=1</span>.
            </p>
            <Snippet
              label="durable run request"
              code={[
                `curl -X POST '${base}/agents/${idPath}/durable-runs' \\`,
                `  -H 'Authorization: Bearer tyk_YOUR_API_KEY' \\`,
                `  -H 'Content-Type: application/json' \\`,
                `  -d '${JSON.stringify({ input: example })}'`,
              ].join("\n")}
            />
            <p className={NOTE}>
              Returns <span className="mono">202 {'{"run_id": "…"}'}</span> immediately — poll the
              run below for the result.
            </p>
            {hasHuman && (
              <p className={NOTE}>
                A run paused at the human node reports{" "}
                <span className="mono">"status": "waiting"</span>; deliver the awaited input with{" "}
                <span className="mono">POST /runs/{"{run_id}"}/resume</span> and a{" "}
                <span className="mono">{'{"input": …}'}</span> body.
              </p>
            )}
          </Section>
        ) : (
          <>
            <Section title="Run (interactive)">
              <p className={NOTE}>
                Accepts your session token or an API key. Streams Server-Sent Events by default;
                pass <span className="mono">"stream": false</span> for a single JSON response.
              </p>
              <Snippet
                label="run request"
                code={runCurl(base, `/agents/${idPath}/runs`, example)}
              />
              <p className={NOTE}>
                Response:{" "}
                <span className="mono">
                  {'{"runId": "…", "status": "completed", "output": "…"}'}
                </span>
              </p>
            </Section>

            <Section title="Invoke (unattended)">
              <p className={NOTE}>
                The endpoint for scripts, services and other frontends. Accepts an API key or the
                deploy-wide <span className="mono">THEYGENT_INVOKE_TOKEN</span> — session tokens are
                deliberately rejected, and with neither credential every call is a 401.{" "}
                <span className="mono">stream</span> defaults to false.
              </p>
              <Snippet
                label="invoke request"
                code={[
                  `curl -X POST '${base}/agents/${idPath}/invoke' \\`,
                  `  -H 'Authorization: Bearer tyk_YOUR_API_KEY' \\`,
                  `  -H 'Content-Type: application/json' \\`,
                  `  -d '${JSON.stringify({ input: example })}'`,
                ].join("\n")}
              />
            </Section>
          </>
        )}

        <Section title="Poll a run">
          <Snippet
            label="poll request"
            code={[
              `curl '${base}/runs/RUN_ID' \\`,
              `  -H 'Authorization: Bearer tyk_YOUR_API_KEY'`,
            ].join("\n")}
          />
          <p className={NOTE}>
            The run, invoke and durable-run bodies also accept{" "}
            <span className="mono">"version"</span>
            {agent.latest_version && (
              <>
                {" "}
                (latest: <span className="mono">{agent.latest_version}</span>)
              </>
            )}{" "}
            or <span className="mono">"content_hash"</span> to pin an exact published version —
            omitted means latest. A trigger's pin is fixed on the trigger itself.
          </p>
        </Section>

        {triggers.isSuccess && (
          <Section title="Triggers">
            {agentTriggers.length === 0 ? (
              <p className={NOTE}>
                No triggers are deployed for this agent. Create a webhook or cron schedule with{" "}
                <span className="mono">POST /triggers</span> to give it an unattended entry point
                with its own per-trigger credential.
              </p>
            ) : (
              <div className="space-y-2">
                {agentTriggers.map((t) => (
                  <TriggerBlock key={t.id} trigger={t} base={base} durableOnly={durableOnly} />
                ))}
              </div>
            )}
          </Section>
        )}
      </div>
    </Modal>
  );
}

function TriggerBlock({
  trigger,
  base,
  durableOnly,
}: {
  trigger: TriggerRecord;
  base: string;
  durableOnly: boolean;
}) {
  const pin = trigger.version ? `v${trigger.version}` : (trigger.content_hash ?? "");
  return (
    <div className="space-y-1.5 rounded-md border border-border px-3 py-2">
      <div className="flex items-center gap-2 text-xs">
        <Badge variant="secondary" className="mono text-[11px]">
          {trigger.kind}
        </Badge>
        <span className="mono truncate text-muted-foreground" title={trigger.id}>
          {trigger.id}
        </span>
        <span className="mono text-muted-foreground/70">{pin}</span>
        {!trigger.enabled && (
          <Badge
            variant="secondary"
            className="bg-amber-500/15 text-[11px] text-amber-700 dark:text-amber-300"
          >
            disabled
          </Badge>
        )}
      </div>
      {trigger.kind === "webhook" && (
        <>
          <p className={NOTE}>
            Fire it by POSTing JSON to the URL below, signed with HMAC-SHA256 of the raw body in the{" "}
            <span className="mono">X-Theygent-Signature</span> header. The parsed body becomes the
            run input.
          </p>
          <Snippet label="webhook request" code={webhookCurl(base, trigger.id)} />
        </>
      )}
      {trigger.kind === "schedule" && (
        <p className={NOTE}>
          Fires on cron <span className="mono">{String(trigger.config?.cron ?? "")}</span> (UTC) —
          no public URL.
        </p>
      )}
      {trigger.kind === "http" && (
        <p className={NOTE}>
          {durableOnly
            ? "Token-invoke registration — this agent runs only durably, so use the durable-run endpoint above (invoke rejects it)."
            : "Token-invoke registration — call the invoke endpoint above."}
        </p>
      )}
    </div>
  );
}
