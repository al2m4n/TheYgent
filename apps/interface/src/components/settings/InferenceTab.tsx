// Settings → Inference: the inference plane's OWN settings surface — a separate, user-controlled
// trust domain reached directly (never proxied through the control plane). Settable knobs come
// from its settings resource; read-only environment facts from its diagnostics resource; the HF
// token is a reserved name in its machine-local, write-only credential store (gated model
// downloads) and the browser PUTs it straight to that plane.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { type InferenceSettings, api, residentEngines } from "../../lib/api";
import { notify } from "../../lib/notify";
import { Badge, Button, Card, ErrorBanner, Field, Input, SectionHeading, Spinner } from "../ui";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../ui/table";

const HF_TOKEN = "HF_TOKEN";

export function InferenceTab() {
  return (
    <div className="space-y-4">
      <MaxResidentCard />
      <ResidentEnginesCard />
      <DiagnosticsCard />
      <HfTokenCard />
    </div>
  );
}

// ── the resident-engine ceiling ───────────────────────────────────────────────

function MaxResidentCard() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["inference-settings"],
    queryFn: () => api.getInferenceSettings(),
  });
  const entry = q.data?.maxResident;
  const pinned = entry?.source === "env";
  const [text, setText] = useState<string | null>(null); // null = untouched, show server value

  const patch = useMutation({
    mutationFn: (value: number | null) => api.patchInferenceSettings({ maxResident: value }),
    onSuccess: (res) => {
      qc.setQueryData<InferenceSettings>(["inference-settings"], res);
      qc.invalidateQueries({ queryKey: ["engines"] });
      setText(null);
      notify.success("Resident-engine ceiling updated");
    },
  });

  const shown = text ?? (entry ? String(entry.value) : "");
  const parsed = Number.parseInt(shown, 10);
  const valid = !Number.isNaN(parsed) && parsed >= 1;
  const dirty = entry != null && text !== null && (!valid || parsed !== entry.value);

  return (
    <Card className="p-4">
      <SectionHeading>Resident engines</SectionHeading>
      <p className="mt-1 text-xs text-muted-foreground">
        How many engines may hold a model in memory at once. Applies immediately — engines over the
        new ceiling are evicted when idle; busy ones finish their in-flight work and drain.
      </p>
      {q.isLoading ? (
        <Spinner label="Loading…" />
      ) : q.error ? (
        <div className="mt-2">
          <ErrorBanner error={q.error} />
        </div>
      ) : entry ? (
        <div className="mt-3 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium text-foreground">max resident</span>
            {pinned && (
              <Badge tone="slate">
                <span
                  title={`Set by the ${entry.envVar} environment variable — unset it to edit here.`}
                >
                  pinned by {entry.envVar}
                </span>
              </Badge>
            )}
            {entry.source === "default" && <Badge tone="blue">default</Badge>}
          </div>
          <div className="flex items-end gap-2">
            <div className="w-32">
              <Field label="Max resident">
                <Input
                  aria-label="maxResident"
                  type="number"
                  min={1}
                  value={shown}
                  disabled={pinned}
                  onChange={(e) => setText(e.target.value)}
                />
              </Field>
            </div>
            <Button
              variant="primary"
              disabled={pinned || !dirty || !valid || patch.isPending}
              onClick={() => patch.mutate(parsed)}
            >
              {patch.isPending ? "Saving…" : "Save"}
            </Button>
            {entry.source === "stored" && !pinned && (
              <Button
                variant="ghost"
                disabled={patch.isPending}
                title={`Reset to default (${entry.default})`}
                onClick={() => patch.mutate(null)}
              >
                Reset to default
              </Button>
            )}
          </div>
          {text !== null && !valid && (
            <p className="text-xs text-destructive">Enter a whole number of at least 1.</p>
          )}
          <ErrorBanner error={patch.error} />
        </div>
      ) : null}
    </Card>
  );
}

// ── what's warm right now ─────────────────────────────────────────────────────

function ResidentEnginesCard() {
  const q = useQuery({ queryKey: ["engines"], queryFn: () => api.getEngines() });
  const resident = residentEngines(q.data);

  return (
    <Card className="p-4">
      <SectionHeading>Warm engines</SectionHeading>
      {q.isLoading ? (
        <Spinner label="Loading…" />
      ) : q.error ? (
        <div className="mt-2">
          <ErrorBanner error={q.error} />
        </div>
      ) : resident.length === 0 ? (
        <p className="mt-2 text-xs text-muted-foreground">
          Nothing resident right now — an engine spawns on first use.
        </p>
      ) : (
        <div className="mt-3 overflow-hidden rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Model</TableHead>
                <TableHead>Engine</TableHead>
                <TableHead>In-flight</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {resident.map((e) => (
                <TableRow key={e.logicalId}>
                  <TableCell className="mono text-xs">{e.logicalId}</TableCell>
                  <TableCell className="text-xs">{e.engine}</TableCell>
                  <TableCell className="text-xs">
                    {e.inflight}
                    {e.draining && <span className="ml-1 text-muted-foreground">(draining)</span>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </Card>
  );
}

// ── environment diagnostics (read-only) ───────────────────────────────────────

function DiagnosticsCard() {
  const q = useQuery({
    queryKey: ["inference-diagnostics"],
    queryFn: () => api.getInferenceDiagnostics(),
  });
  const d = q.data;

  return (
    <Card className="p-4">
      <SectionHeading>Diagnostics</SectionHeading>
      {q.isLoading ? (
        <Spinner label="Loading…" />
      ) : q.error ? (
        <div className="mt-2">
          <ErrorBanner error={q.error} />
        </div>
      ) : d ? (
        <div className="mt-3 space-y-3">
          <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1.5 text-xs">
            <dt className="text-muted-foreground">State dir</dt>
            <dd className="mono text-foreground">{d.stateDir ?? "— (in-memory)"}</dd>
            <dt className="text-muted-foreground">Model dir</dt>
            <dd className="mono text-foreground">{d.modelDir ?? "—"}</dd>
            <dt className="text-muted-foreground">HF home</dt>
            <dd className="mono text-foreground">{d.hfHome}</dd>
            <dt className="text-muted-foreground">Persistent</dt>
            <dd className="text-foreground">
              {d.persistent ? "yes" : "no — registrations reset on restart"}
            </dd>
          </dl>
          <div className="space-y-1 border-t border-border/60 pt-3">
            <SectionHeading>Engine binaries</SectionHeading>
            {d.binaries.length === 0 ? (
              <p className="text-xs text-muted-foreground">No engine binaries reported.</p>
            ) : (
              <ul className="space-y-1">
                {d.binaries.map((b) => (
                  <li key={`${b.engine}:${b.modality}`} className="flex items-center gap-2 text-xs">
                    <span className="font-medium text-foreground">{b.engine}</span>
                    <span className="text-muted-foreground">{b.modality}</span>
                    {b.status === "resolved" ? (
                      <Badge tone="green">resolved</Badge>
                    ) : (
                      <Badge tone="amber">missing</Badge>
                    )}
                    {b.path && (
                      <span className="mono truncate text-muted-foreground/70" title={b.path}>
                        {b.path}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      ) : null}
    </Card>
  );
}

// ── the HF token (a reserved credential name) ─────────────────────────────────

function HfTokenCard() {
  const qc = useQueryClient();
  const creds = useQuery({ queryKey: ["credentials"], queryFn: () => api.listCredentials() });
  const existing = creds.data?.find((c) => c.name === HF_TOKEN);
  const [value, setValue] = useState("");
  const [confirming, setConfirming] = useState(false);

  const put = useMutation({
    mutationFn: () => api.putCredential(HF_TOKEN, value),
    onSuccess: () => {
      setValue("");
      qc.invalidateQueries({ queryKey: ["credentials"] });
      notify.success("Hugging Face token saved");
    },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteCredential(HF_TOKEN),
    onSuccess: () => {
      setConfirming(false);
      qc.invalidateQueries({ queryKey: ["credentials"] });
      notify.success("Hugging Face token removed");
    },
  });

  return (
    <Card className="p-4">
      <SectionHeading>Hugging Face token</SectionHeading>
      <p className="mt-1 text-xs text-muted-foreground">
        Needed only to download gated models. Write-only: it is stored in the inference plane's
        machine-local credential store (the machine running your models) and never returns over the
        wire — and it never touches theygent servers.
      </p>
      <div className="mt-3 space-y-2">
        {existing?.hasValue && (
          <div className="flex items-center gap-2 text-xs">
            <span className="mono text-foreground">{HF_TOKEN}</span>
            <span className="text-muted-foreground">•••••• set</span>
            <button
              type="button"
              className="text-[11px] text-rose-600 hover:underline disabled:opacity-50 dark:text-rose-400"
              disabled={remove.isPending}
              onClick={() => {
                if (confirming) remove.mutate();
                else setConfirming(true);
              }}
            >
              {confirming ? "Confirm remove?" : "Remove"}
            </button>
          </div>
        )}
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <Field label={existing?.hasValue ? "Replace token (write-only)" : "Token (write-only)"}>
              <Input
                type="password"
                value={value}
                placeholder="hf_…"
                onChange={(e) => setValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && value.trim() && !put.isPending) put.mutate();
                }}
              />
            </Field>
          </div>
          <Button
            variant="primary"
            disabled={!value.trim() || put.isPending}
            onClick={() => put.mutate()}
          >
            {put.isPending ? "Saving…" : "Save token"}
          </Button>
        </div>
        <ErrorBanner error={put.error ?? remove.error ?? creds.error} />
      </div>
    </Card>
  );
}
