// Settings → Samples: install the shipped example agents. Each sample is a complete, runnable
// graph that lands as an ORDINARY registry agent (edit, duplicate, delete like any other) — the
// only per-install choice is which models fill its slots, because logical model ids are
// environment bindings the catalog can never ship. Model options come straight from the
// inference plane's registry (the browser reaches it directly; unreachable degrades to a hint).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { FlaskConical } from "lucide-react";
import { useState } from "react";
import { type SampleView, api } from "../../lib/api";
import { notify } from "../../lib/notify";
import { Badge, Button, ErrorBanner, Field, Select, Spinner, linkClass } from "../ui";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemGroup,
  ItemMedia,
  ItemTitle,
} from "../ui/item";

/** Slot → the chosen logical model id ("" = unfilled). Bindings are derived from the registry. */
type SlotChoices = Record<string, string>;

// The pickers are shared across all samples, so their copy is slot-generic; each sample card
// lists its OWN slot hints (e.g. "pick a model with tool-calling support") next to its badges.
const SLOT_COPY: Record<string, { label: string; description: string }> = {
  local: { label: "Local model", description: "Fills every sample's local-model slot." },
  remote: { label: "API model", description: "Fills every sample's API-model slot." },
  vision: { label: "Vision model", description: "For samples that look at images." },
  stt: { label: "Speech-to-text model", description: "For samples that hear audio." },
  tts: { label: "Text-to-speech model", description: "For samples that talk back." },
  image: { label: "Image model", description: "For samples that generate images." },
  embedding: { label: "Embedding model", description: "Indexes and searches RAG sources." },
};

//: Stable picker order: the chat pair first, then the special-modality slots.
const SLOT_ORDER = ["local", "remote", "vision", "stt", "tts", "image", "embedding"];

export function SamplesTab() {
  const qc = useQueryClient();
  const samplesQuery = useQuery({ queryKey: ["samples"], queryFn: () => api.listSamples() });
  const modelsQuery = useQuery({
    queryKey: ["models"],
    queryFn: () => api.listModels(),
    retry: false,
  });
  // Shares the Settings page's cache entry — no second fetch; used only for the durable hint.
  const settingsQuery = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.getSettings(),
    retry: false,
  });
  const [choices, setChoices] = useState<SlotChoices>({});

  const samples = samplesQuery.data?.samples ?? [];
  const models = modelsQuery.data ?? [];
  // A slot with a declared modality (vision / audio.* / images.generation / embeddings) filters
  // the registry by that modality, whatever the binding. Chat slots keep the local/remote
  // split: "local" = served by a local engine; "remote" = an openai-compatible registration.
  // One registry feeds all of it — the binding/modality axes narrow, never the source.
  const slotModality = (slot: string): string | null =>
    samples.map((s) => s.model_slots[slot]?.modality).find((m) => m != null) ?? null;
  const chatModels = models.filter((m) => !m.binding.modality || m.binding.modality === "chat");
  const optionsFor = (slot: string) => {
    const modality = slotModality(slot);
    if (modality && modality !== "chat")
      return models.filter((m) => m.binding.modality === modality);
    return slot === "remote"
      ? chatModels.filter((m) => m.binding.binding === "openai-compatible")
      : chatModels.filter((m) => m.binding.binding !== "openai-compatible");
  };

  const durableOff =
    settingsQuery.data?.boot.some((b) => b.key === "durable" && b.value === false) ?? false;

  const slots = [...new Set(samples.flatMap((s) => Object.keys(s.model_slots)))].sort(
    (a, b) =>
      (SLOT_ORDER.indexOf(a) + 1 || SLOT_ORDER.length + 1) -
      (SLOT_ORDER.indexOf(b) + 1 || SLOT_ORDER.length + 1),
  );

  // THE one definition of a filled slot: the choice must resolve in the CURRENT model list —
  // the same lookup the install payload uses, so the button can never enable with a choice the
  // mutation would silently drop (e.g. a model unregistered after being picked).
  const chosenModel = (slot: string) =>
    models.find((m) => m.logicalId === (choices[slot] ?? "")) ?? null;

  const install = useMutation({
    mutationFn: (sample: SampleView) => {
      const chosen: Record<string, { logical_id: string; binding: string }> = {};
      for (const slot of Object.keys(sample.model_slots)) {
        const model = chosenModel(slot);
        if (model) chosen[slot] = { logical_id: model.logicalId, binding: model.binding.binding };
      }
      return api.installSamples({ ids: [sample.id], models: chosen });
    },
    onSuccess: (result, sample) => {
      const entry = result.report[0];
      if (entry?.status === "error") {
        notify.error(`Install failed: ${entry.message ?? entry.code ?? "unknown error"}`, {
          id: `sample-${sample.id}`,
        });
      } else if (entry?.status === "already_installed") {
        notify.warning(`${sample.title} is already installed`, { id: `sample-${sample.id}` });
      } else {
        notify.success(`Installed ${sample.agent_name}`, { id: `sample-${sample.id}` });
        (entry?.warnings ?? []).forEach((warning, i) => {
          // Unique ids: same-id toasts REPLACE each other, and every warning must survive.
          notify.warning(warning, { id: `sample-${sample.id}-warn-${i}` });
        });
      }
      qc.invalidateQueries({ queryKey: ["samples"] });
      qc.invalidateQueries({ queryKey: ["agents"] });
      qc.invalidateQueries({ queryKey: ["connections"] });
    },
    onError: (err, sample) => {
      notify.error(`Install failed: ${err instanceof Error ? err.message : String(err)}`, {
        id: `sample-${sample.id}`,
      });
    },
  });

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>Sample agents</CardTitle>
        <CardDescription>
          Ready-made agents that show what TheYgent can do — two-model runs, private SQL over a
          local database, human approval, MCP tools from REST and GraphQL. Installing creates a
          normal agent named <span className="font-mono">sample-…</span> that you can open in the
          editor, change, or delete.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 p-4">
        {samplesQuery.isLoading && <Spinner />}
        <ErrorBanner error={samplesQuery.error} />

        {slots.length > 0 && (
          <div className="grid gap-3 sm:grid-cols-2">
            {slots.map((slot) => {
              const meta =
                SLOT_COPY[slot] ??
                samples.map((s) => s.model_slots[slot]).find((m) => m !== undefined);
              const options = optionsFor(slot);
              return (
                <Field key={slot} label={meta?.label ?? slot}>
                  <Select
                    aria-label={meta?.label ?? slot}
                    value={choices[slot] ?? ""}
                    onChange={(e) => setChoices((prev) => ({ ...prev, [slot]: e.target.value }))}
                  >
                    <option value="">
                      {options.length === 0
                        ? modelsQuery.isError
                          ? "inference plane unreachable"
                          : slotModality(slot)
                            ? `no ${slotModality(slot)} model registered`
                            : slot === "remote"
                              ? "no openai-compatible model registered"
                              : "no local model registered"
                        : "Choose a model…"}
                    </option>
                    {options.map((m) => (
                      <option key={m.logicalId} value={m.logicalId}>
                        {m.logicalId} ({m.binding.binding})
                      </option>
                    ))}
                  </Select>
                  {meta && (
                    <span className="block text-[11px] text-muted-foreground">
                      {meta.description}
                    </span>
                  )}
                </Field>
              );
            })}
          </div>
        )}

        <ItemGroup className="gap-2">
          {samples.map((sample) => {
            const missing = Object.keys(sample.model_slots).filter((slot) => !chosenModel(slot));
            return (
              <Item key={sample.id} variant="outline" className="bg-card">
                <ItemMedia variant="icon">
                  <FlaskConical />
                </ItemMedia>
                <ItemContent>
                  <ItemTitle>
                    {sample.title}
                    <span className="ml-1 font-mono text-[11px] text-muted-foreground">
                      {sample.agent_name}
                    </span>
                  </ItemTitle>
                  <ItemDescription>{sample.description}</ItemDescription>
                  {!sample.installed && Object.keys(sample.model_slots).length > 0 && (
                    <div className="text-[11px] text-muted-foreground">
                      {Object.values(sample.model_slots)
                        .map((m) => `${m.label} — ${m.description}`)
                        .join(" · ")}
                    </div>
                  )}
                  <div className="flex flex-wrap gap-1 pt-1">
                    {sample.capabilities.map((cap) => (
                      <Badge key={cap} tone="blue">
                        {cap}
                      </Badge>
                    ))}
                    {sample.connections.map((c) => (
                      <Badge key={c.key} tone="slate">
                        creates {c.name} ({c.transport})
                      </Badge>
                    ))}
                    {sample.rag_sources.map((r) => (
                      <Badge key={r.key} tone="slate">
                        creates {r.name} (RAG)
                      </Badge>
                    ))}
                    {sample.agent_names.length > 1 && (
                      <Badge tone="slate">installs {sample.agent_names.length} agents</Badge>
                    )}
                    {sample.trigger && !sample.trigger.enabled && (
                      <Badge tone="slate">ships a disabled {sample.trigger.kind} trigger</Badge>
                    )}
                    {sample.durable && (
                      <Badge tone="amber">
                        {durableOff
                          ? "needs the durable runtime (THEYGENT_DURABLE=1) — currently off"
                          : "runs on the durable runtime"}
                      </Badge>
                    )}
                  </div>
                </ItemContent>
                <ItemActions>
                  {sample.installed ? (
                    <>
                      <Badge tone="green">Installed</Badge>
                      <Link
                        className={`text-xs ${linkClass}`}
                        to="/editor"
                        search={{ agent: sample.agent_id }}
                      >
                        Open in editor
                      </Link>
                    </>
                  ) : (
                    <Button
                      variant="primary"
                      disabled={missing.length > 0 || install.isPending}
                      title={
                        missing.length > 0 ? `Choose a model for: ${missing.join(", ")}` : undefined
                      }
                      onClick={() => install.mutate(sample)}
                    >
                      Install
                    </Button>
                  )}
                </ItemActions>
              </Item>
            );
          })}
        </ItemGroup>
      </CardContent>
    </Card>
  );
}
