// Settings → RAG: chunking, crawl concurrency, upload cap (edited in MiB, stored in bytes),
// retrieval defaults, and the default embedding model for NEW sources (each source's own binding
// stays immutable after creation — this only seeds the create form).

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { type SettingEntry, api } from "../../lib/api";
import { formatBytes } from "../../lib/format";
import { SearchableSelect } from "../SearchableSelect";
import { Card, ErrorBanner, Input, SectionHeading, Spinner } from "../ui";
import { SaveBar } from "./SaveBar";
import { SettingRow } from "./SettingField";
import { type PlatformSettingsForm, displayValue } from "./useSettingsForm";

const MIB = 1024 * 1024;

export function RagTab({ form }: { form: PlatformSettingsForm }) {
  // Gate on data, not isLoading — see BootConfig: retries against an unreachable plane must not
  // flash an empty success layout.
  if (form.loadError && !form.data) return <ErrorBanner error={form.loadError} />;
  if (!form.data) return <Spinner label="Loading settings…" />;

  const row = (key: string) => {
    const entry = form.entry(key);
    if (!entry) return null;
    if (key === "rag.max_upload_bytes")
      return (
        <SettingRow
          key={key}
          entry={entry}
          form={form}
          label="max upload size"
          control={<MibControl entry={entry} form={form} />}
        />
      );
    if (key === "rag.default_embedding_model")
      return (
        <SettingRow
          key={key}
          entry={entry}
          form={form}
          control={<EmbeddingModelPicker entry={entry} form={form} />}
        />
      );
    return <SettingRow key={key} entry={entry} form={form} />;
  };

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <SectionHeading>Chunking</SectionHeading>
        <div className="mt-2">
          {row("rag.chunk_max_tokens")}
          {row("rag.chunk_overlap_tokens")}
        </div>
      </Card>

      <Card className="p-4">
        <SectionHeading>Crawling</SectionHeading>
        <div className="mt-2">
          {row("rag.crawl_desired_concurrency")}
          {row("rag.crawl_max_concurrency")}
        </div>
      </Card>

      <Card className="p-4">
        <SectionHeading>Uploads &amp; retrieval</SectionHeading>
        <div className="mt-2">
          {row("rag.max_upload_bytes")}
          {row("rag.default_top_k")}
          {row("rag.default_embedding_model")}
        </div>
      </Card>

      <SaveBar form={form} group="rag" />
    </div>
  );
}

// The byte-typed cap edited in MiB — humans size uploads in MiB, the wire carries bytes.
function MibControl({ entry, form }: { entry: SettingEntry; form: PlatformSettingsForm }) {
  const bytes = displayValue(form, entry);
  const shownMib = typeof bytes === "number" ? bytes / MIB : Number.NaN;
  const [text, setText] = useState(Number.isNaN(shownMib) ? "" : String(round2(shownMib)));

  // Re-sync when the underlying value changes from OUTSIDE the input (reset staged, save landed)
  // — otherwise the text keeps showing a stale MiB number next to a byte label that moved on.
  // biome-ignore lint/correctness/useExhaustiveDependencies: sync from the derived display value only
  useEffect(() => {
    const parsed = Math.round(Number.parseFloat(text) * MIB);
    if (typeof bytes === "number" ? parsed !== bytes : text !== "")
      setText(typeof bytes === "number" ? String(round2(bytes / MIB)) : "");
  }, [bytes]);

  // The invalid text this blocker guards lives only in local state — an unmount (tab switch)
  // discards the text, so the blocker must go with it.
  useEffect(() => () => form.setBlocker(entry.key, null), [form.setBlocker, entry.key]);

  const onChange = (raw: string) => {
    setText(raw);
    const mib = Number.parseFloat(raw);
    if (raw.trim() === "" || Number.isNaN(mib)) {
      form.setBlocker(entry.key, "Enter a size in MiB.");
      return;
    }
    const next = Math.round(mib * MIB);
    const { min, max } = entry.constraints ?? {};
    if (min != null && next < min) {
      form.setBlocker(entry.key, `Must be at least ${formatBytes(min)}.`);
      return;
    }
    if (max != null && next > max) {
      form.setBlocker(entry.key, `Must be at most ${formatBytes(max)}.`);
      return;
    }
    form.setBlocker(entry.key, null);
    form.setEdit(entry.key, next);
  };

  return (
    <div className="flex items-center gap-2">
      <Input
        aria-label={entry.key}
        type="number"
        inputMode="decimal"
        className="max-w-40"
        value={text}
        disabled={entry.env_pinned}
        onChange={(e) => onChange(e.target.value)}
      />
      <span className="text-xs text-muted-foreground">
        MiB{typeof bytes === "number" ? ` = ${bytes.toLocaleString()} bytes` : ""}
      </span>
    </div>
  );
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

// The default embedding model for NEW retrieval sources, picked from the inference plane's
// registered models (embeddings-capable first; everything as a fallback if none are tagged).
function EmbeddingModelPicker({
  entry,
  form,
}: {
  entry: SettingEntry;
  form: PlatformSettingsForm;
}) {
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.listModels() });
  const all = models.data ?? [];
  const embedding = all.filter((m) => m.binding.modality === "embeddings");
  const options = (embedding.length > 0 ? embedding : all).map((m) => ({
    value: m.logicalId,
    label: m.logicalId,
  }));
  const value = displayValue(form, entry);

  return (
    <div className="space-y-1">
      <SearchableSelect
        options={options}
        value={typeof value === "string" ? value : ""}
        onChange={(v) => form.setEdit(entry.key, v)}
        placeholder="No default — pick per source"
        ariaLabel={entry.key}
        disabled={entry.env_pinned}
        className="w-full max-w-md"
      />
      <p className="text-[11px] text-muted-foreground/80">
        Used as the DEFAULT for new sources only — an existing source keeps the embedding model it
        was created with.
      </p>
    </div>
  );
}
