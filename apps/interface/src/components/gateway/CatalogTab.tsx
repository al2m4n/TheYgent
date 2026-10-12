// The AI Gateway page's Catalog (admins): every kind of side request the gateways have seen, and
// what routing may do with it. A kind is identified by a fingerprint of its shape; its review
// status is a rule in the gateway config, so reviewing one saves a new config version — and a
// kind nobody reviewed passes through untouched.
//
// Kinds recognised by their shape get a suggestion (a startup quota check: never route; a side
// request riding the main conversation's cache: never route; a session title: a local model could
// answer it). A suggestion is only that: the status is the admin's decision.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  ApiError,
  type FingerprintStatus,
  type GatewayFingerprintEntry,
  type GatewayKnownKind,
  api,
} from "../../lib/api";
import { isToken } from "../../lib/gatewayConfig";
import { notify } from "../../lib/notify";
import { TimeAgo } from "../TimeAgo";
import { Badge, ErrorBanner, Field, Input, Modal } from "../ui";
import { Button } from "../ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "../ui/empty";
import { Item, ItemActions, ItemContent, ItemDescription, ItemTitle } from "../ui/item";
import { NativeSelect, NativeSelectOption } from "../ui/native-select";
import { Skeleton } from "../ui/skeleton";

export const STATUSES: Record<FingerprintStatus, { label: string; detail: string; tone: string }> =
  {
    unreviewed: {
      label: "unreviewed",
      detail: "Passes through untouched until someone decides.",
      tone: "slate",
    },
    observe_only: {
      label: "observe only",
      detail: "Recorded and reported, never routed.",
      tone: "blue",
    },
    allow_cheaper: {
      label: "cheaper model",
      detail: "May go to the cheaper Claude model (the config's subagent target).",
      tone: "violet",
    },
    allow_local: {
      label: "local model",
      detail: "May be answered by the local model (the config's local target).",
      tone: "green",
    },
    never_route: {
      label: "never route",
      detail: "Always stays on the model Claude Code asked for.",
      tone: "amber",
    },
  };

const KINDS: Record<GatewayKnownKind, string> = {
  quota_check: "Claude Code's startup quota check",
  main_prefix_side_request: "a side request riding the main conversation's cache",
  session_title: "the session title",
};

function count(n: number, noun: string, none = `0 ${noun}s`): string {
  if (n === 0) return none;
  return `${n.toLocaleString()} ${noun}${n === 1 ? "" : "s"}`;
}

function shapeSummary(shape: Record<string, unknown> | null): string {
  if (!shape) return "shape not seen yet";
  const parts: string[] = [];
  if (typeof shape.tool_count === "number") parts.push(count(shape.tool_count, "tool", "no tools"));
  if (shape.has_output_schema) parts.push("structured output");
  if (typeof shape.max_tokens === "number") parts.push(`max ${count(shape.max_tokens, "token")}`);
  if (typeof shape.thinking === "string") parts.push(`thinking ${shape.thinking}`);
  return parts.join(" · ") || "no tools";
}

export function CatalogTab() {
  const qc = useQueryClient();
  const catalog = useQuery({
    queryKey: ["gatewayFingerprints"],
    queryFn: () => api.listGatewayFingerprints(),
  });
  const [editing, setEditing] = useState<GatewayFingerprintEntry | null>(null);
  const review = useMutation({
    mutationFn: (rule: { fingerprint: string; status: FingerprintStatus; label: string | null }) =>
      api.setGatewayFingerprintRule(
        rule.fingerprint,
        { status: rule.status, label: rule.label },
        catalog.data?.config_version,
      ),
    onSuccess: (saved) => {
      notify.success(`Saved as gateway config v${saved.config_version}`);
      setEditing(null);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.code === "config_version_conflict") {
        notify.error("The gateway config changed since this page loaded — reloaded; review again.");
      } else {
        notify.error(e instanceof Error ? e.message : String(e));
      }
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["gatewayFingerprints"] });
      qc.invalidateQueries({ queryKey: ["gatewayConfig"] });
    },
  });

  if (catalog.isPending) return <Skeleton className="h-32 rounded-lg" />;
  const entries = catalog.data?.fingerprints ?? [];
  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        Kinds of side requests Claude Code sends (titles, suggestions, checks), told apart by a
        fingerprint of their shape. An unreviewed kind passes through; reviewing one saves a new
        gateway config version.
      </p>
      <ErrorBanner error={catalog.error} />
      {entries.length === 0 && !catalog.error ? (
        <Empty className="rounded-lg border border-dashed">
          <EmptyHeader>
            <EmptyTitle>No side-request kinds seen yet</EmptyTitle>
            <EmptyDescription>
              They appear here as gateways in observer mode record interactive Claude Code sessions.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <ul className="flex flex-col gap-2" aria-label="Side-request kinds">
          {entries.map((entry) => (
            <KindRow
              key={entry.fingerprint}
              entry={entry}
              busy={review.isPending}
              onEdit={() => setEditing(entry)}
              onApply={(status) =>
                review.mutate({ fingerprint: entry.fingerprint, status, label: entry.label })
              }
            />
          ))}
        </ul>
      )}
      {editing && (
        <ReviewDialog
          entry={editing}
          busy={review.isPending}
          onClose={() => setEditing(null)}
          onSave={(status, label) =>
            review.mutate({ fingerprint: editing.fingerprint, status, label })
          }
        />
      )}
    </div>
  );
}

function KindRow({
  entry,
  busy,
  onEdit,
  onApply,
}: {
  entry: GatewayFingerprintEntry;
  busy: boolean;
  onEdit: () => void;
  onApply: (status: FingerprintStatus) => void;
}) {
  const status = STATUSES[entry.status];
  const suggestion =
    entry.known_kind && entry.suggested_status && entry.suggested_status !== entry.status
      ? entry.suggested_status
      : null;
  return (
    <Item variant="outline" size="sm" asChild>
      <li data-testid={`kind-${entry.fingerprint}`}>
        <ItemContent>
          <ItemTitle className="flex flex-wrap items-center gap-2">
            <span className="font-mono">{entry.fingerprint}</span>
            {entry.label && <span>{entry.label}</span>}
            <Badge tone={status.tone}>{status.label}</Badge>
          </ItemTitle>
          <ItemDescription className="flex flex-wrap gap-x-4 gap-y-1">
            {entry.known_kind && <span>Looks like {KINDS[entry.known_kind]}</span>}
            <span>{shapeSummary(entry.shape)}</span>
            <span>{entry.request_count.toLocaleString()} seen</span>
            {entry.last_seen ? (
              <span>
                last <TimeAgo iso={entry.last_seen} />
              </span>
            ) : (
              <span>not seen yet</span>
            )}
            {entry.client_versions.length > 0 && (
              <span>Claude Code {entry.client_versions.join(", ")}</span>
            )}
          </ItemDescription>
        </ItemContent>
        <ItemActions>
          {suggestion && (
            <Button
              size="sm"
              disabled={busy}
              onClick={() => onApply(suggestion)}
              title={STATUSES[suggestion].detail}
            >
              Apply suggestion: {STATUSES[suggestion].label}
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            onClick={onEdit}
            aria-label={`Review ${entry.fingerprint}`}
          >
            Review
          </Button>
        </ItemActions>
      </li>
    </Item>
  );
}

function ReviewDialog({
  entry,
  busy,
  onClose,
  onSave,
}: {
  entry: GatewayFingerprintEntry;
  busy: boolean;
  onClose: () => void;
  onSave: (status: FingerprintStatus, label: string | null) => void;
}) {
  const [status, setStatus] = useState<FingerprintStatus>(entry.status);
  const [label, setLabel] = useState(entry.label ?? "");
  const cleanLabel = label.trim();
  const labelValid = cleanLabel === "" || isToken(cleanLabel);
  return (
    <Modal title={`Review ${entry.fingerprint}`} onClose={onClose} width="max-w-lg">
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (labelValid) onSave(status, cleanLabel || null);
        }}
      >
        <Field label="What routing may do">
          <NativeSelect
            aria-label="Status"
            value={status}
            onChange={(e) => setStatus(e.target.value as FingerprintStatus)}
          >
            {(Object.keys(STATUSES) as FingerprintStatus[]).map((s) => (
              <NativeSelectOption key={s} value={s}>
                {STATUSES[s].label}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </Field>
        <p className="text-xs text-muted-foreground">{STATUSES[status].detail}</p>
        <Field label="Label — a short name, no spaces (optional)">
          <Input
            aria-label="Label"
            value={label}
            placeholder="session-title"
            onChange={(e) => setLabel(e.target.value)}
          />
        </Field>
        {!labelValid && (
          <p className="text-xs text-destructive">
            No spaces: letters, digits and . _ : - only, starting with a letter or digit.
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !labelValid}>
            Save
          </Button>
        </div>
      </form>
    </Modal>
  );
}
