// The one central place for messages, progress and status (replaces the per-page / per-modal
// download trays). A single <NotificationCenter/> mounts at the app root (outside the routed view),
// so anything reported here is visible from every page, bottom-right, and survives navigation.
//
//  • notify.success/error/info/…  — one-off transient messages, from anywhere.
//  • trackDownload(job)           — a persistent, self-updating progress card for a model install.
//
// Downloads run in the inference plane and keep going across a page reload; the center re-attaches
// to any still-active job on mount (see resumeDownloads), so progress is never lost.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Badge, ProgressBar } from "../components/ui";
import { Toaster } from "../components/ui/sonner";
import { type DownloadJob, api } from "./api";
import { formatBytes, formatDuration } from "./format";

// Thin re-export so call sites read `notify.success(...)` / `notify.error(...)`. Anything sonner's
// `toast` exposes (loading, promise, dismiss, …) is available here too.
export const notify = toast;

const downloadToastId = (jobId: string) => `download:${jobId}`;

// Spawn (or update) the persistent progress card for one install. Idempotent: the toast id is keyed
// on the job, so calling it twice for the same job updates the existing card instead of stacking a
// duplicate (matters for resume-on-mount + a fresh install racing).
export function trackDownload(job: Pick<DownloadJob, "id"> & Partial<DownloadJob>): void {
  toast.custom(
    (t) => <DownloadToast jobId={job.id} initial={job} dismiss={() => toast.dismiss(t)} />,
    {
      id: downloadToastId(job.id),
      duration: Number.POSITIVE_INFINITY, // a download isn't transient — it stays until dismissed
      unstyled: true, // the inner card draws its own chrome; no Toaster styling to double up on
      // The card owns its lifecycle: cancel (while active) / ✕ (once settled) call toast.dismiss
      // directly. dismissible:false disables sonner's swipe gesture so a live download can't be
      // flicked off-screen and lost (it would only reappear on a full reload).
      dismissible: false,
    },
  );
}

// One live download row — polls the plane until the job settles, then offers to dismiss. Lifted out
// of the Registries page so it lives in the global toaster and survives navigation.
function DownloadToast({
  jobId,
  initial,
  dismiss,
}: {
  jobId: string;
  initial?: Partial<DownloadJob>;
  dismiss: () => void;
}) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["download", jobId],
    queryFn: () => api.getDownload(jobId),
    initialData: initial?.status ? (initial as DownloadJob) : undefined,
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "done" || s === "error" || s === "cancelled" ? false : 750;
    },
  });
  const cancel = useMutation({
    mutationFn: () => api.cancelDownload(jobId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["download", jobId] }),
  });

  // Live download rate from successive byte samples → speed + ETA.
  const sample = useRef<{ bytes: number; t: number } | null>(null);
  const [rate, setRate] = useState<number | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: sample only on a byte change
  useEffect(() => {
    if (!data) return;
    const now = Date.now();
    const prev = sample.current;
    if (prev && data.doneBytes > prev.bytes && now > prev.t) {
      setRate((data.doneBytes - prev.bytes) / ((now - prev.t) / 1000));
    }
    sample.current = { bytes: data.doneBytes, t: now };
  }, [data?.doneBytes]);

  // When an install completes, the new model appears under Installed → refresh those queries once.
  useEffect(() => {
    if (data?.status === "done") {
      qc.invalidateQueries({ queryKey: ["models"] });
      qc.invalidateQueries({ queryKey: ["engines"] });
    }
  }, [data?.status, qc]);

  if (!data) return <span />;
  const done = data.status === "done";
  const errored = data.status === "error";
  const cancelled = data.status === "cancelled";
  const active = !done && !errored && !cancelled;
  const eta =
    active && rate && rate > 0 && data.totalBytes
      ? (data.totalBytes - data.doneBytes) / rate
      : null;

  let footer = `${formatBytes(data.doneBytes)} / ${formatBytes(data.totalBytes)}`;
  if (active && rate && rate > 0) {
    footer += ` · ${formatBytes(rate)}/s`;
    if (eta && eta > 0) footer += ` · ~${formatDuration(eta)} left`;
  }

  return (
    <div className="w-[360px] space-y-1.5 rounded-lg border border-slate-700 bg-[var(--c-surface-2)] p-3 shadow-xl">
      <div className="flex items-center justify-between gap-2 text-xs">
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-slate-500">⬇</span>
          <span className="mono truncate text-slate-200">{data.logicalId}</span>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {/* Cancelled is user-initiated, not a failure — it gets the neutral slate, not alarm red. */}
          <Badge tone={errored ? "red" : cancelled ? "slate" : done ? "green" : "blue"}>
            {data.status}
          </Badge>
          {active ? (
            <button
              type="button"
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending}
              aria-label="Cancel download"
              className="text-slate-500 hover:text-rose-600 dark:hover:text-rose-300"
            >
              Cancel
            </button>
          ) : (
            <button
              type="button"
              onClick={dismiss}
              aria-label="Dismiss"
              className="rounded px-1.5 text-slate-500 hover:text-slate-300"
            >
              ✕
            </button>
          )}
        </div>
      </div>
      {errored ? (
        <p className="text-[11px] text-rose-600 dark:text-rose-400">{data.error}</p>
      ) : cancelled ? (
        <p className="text-[11px] text-slate-500">Cancelled.</p>
      ) : (
        <>
          <ProgressBar
            value={data.doneBytes}
            max={data.totalBytes}
            indeterminate={!data.totalBytes && !done}
          />
          <p className="text-right text-[11px] text-slate-500">{done ? "Installed ✓" : footer}</p>
        </>
      )}
    </div>
  );
}

// The mountable center. Renders the themed Toaster (bottom-right, follows the app theme) and, once
// on mount, re-attaches a progress card to any install still running in the plane.
export function NotificationCenter() {
  useEffect(() => {
    let active = true;
    api
      .listDownloads()
      .then((jobs) => {
        if (!active) return;
        for (const j of jobs ?? []) {
          if (j.status === "downloading" || j.status === "registering") trackDownload(j);
        }
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);

  return <Toaster position="bottom-right" expand richColors closeButton visibleToasts={6} />;
}
