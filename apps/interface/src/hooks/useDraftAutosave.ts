// Draft autosave — the editor's background persistence loop. The current IRDocument (including
// its `view`: layout is worth preserving in a draft even though it never affects publishing) is
// debounce-saved to the /drafts resource whenever it diverges from the last persisted snapshot.
// The first divergence CREATES a draft (opening a graph and only looking at it never does); every
// later one updates it. Publishing hands off to the registry and deletes the draft — the draft is
// the bridge between editing sessions, never the published artifact.
//
// Saves are compared/scheduled on a JSON snapshot of the document: a false-positive (equal content,
// different serialization) only costs one redundant PUT, while a deep-equality pass on every
// keystroke would cost more than the save it avoids.

import { useQueryClient } from "@tanstack/react-query";
import type { IRDocument } from "@theygent/ir-types";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, flushDraftOnUnload } from "../lib/api";
import { keys } from "../queries";

/** Idle time after the last edit before a save fires. */
const DEBOUNCE_MS = 1500;
/** Ceiling on how long continuous editing can defer a save — the debounce resets on every edit,
 * so without this a long uninterrupted drag/typing session would never persist. */
const MAX_DEFER_MS = 8000;

export type DraftSaveStatus =
  | "clean" // everything persisted (or nothing diverged from the loaded document yet)
  | "pending" // edits waiting out the debounce
  | "saving"
  | "error";

export interface DraftSeed {
  /** Identity of the seeded document (one per load); changing it resets the autosave state. */
  key: string;
  /** The document as loaded — the "nothing to save yet" reference. */
  baseline: IRDocument;
  /** When the session opened an existing draft: its id (updates go to it from the first edit). */
  draftId: string | null;
  /** The registry agent this session edits (stamped onto a created draft); null for a new graph. */
  agentId: string | null;
  /** The opened draft's server `updated_at`, so the status chip starts truthful. */
  savedAt: string | null;
}

export interface DraftAutosave {
  status: DraftSaveStatus;
  /** The live draft id — set once the first save creates one (or from the opened draft). */
  draftId: string | null;
  lastSavedAt: number | null;
  error: string | null;
  /** True when leaving now would lose edits (pending/saving/failed). */
  hasUnsaved: boolean;
  /** Save immediately (Cmd+S, leave-guard). Resolves true when everything is persisted. */
  flush: () => Promise<boolean>;
  /** Publishing succeeded: the registry owns the content now — drop the draft, reset the baseline. */
  markPublished: (publishedIr: IRDocument) => Promise<void>;
}

export function useDraftAutosave(seed: DraftSeed | null, ir: IRDocument | null): DraftAutosave {
  const qc = useQueryClient();
  const [status, setStatus] = useState<DraftSaveStatus>("clean");
  const [draftId, setDraftId] = useState<string | null>(null);
  const [lastSavedAt, setLastSavedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Mutable machinery, deliberately outside React state: the debounce timer, the persisted
  // snapshot, and a generation counter that invalidates in-flight saves when the seed changes
  // (an await can't be cancelled, but a stale one must not write refs for the next document).
  const genRef = useRef(0);
  const draftIdRef = useRef<string | null>(null);
  const agentIdRef = useRef<string | null>(null);
  const persistedRef = useRef<string | null>(null);
  const currentRef = useRef<{ ir: IRDocument; snapshot: string } | null>(null);
  const timerRef = useRef<number | null>(null);
  const firstDirtyAtRef = useRef<number | null>(null);
  const savingRef = useRef(false);

  const clearTimer = useCallback(() => {
    if (timerRef.current != null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  // Reset on a new seeded document (load / new / reopen). The baseline snapshot is what "no
  // changes yet" means — matching it never creates a draft.
  useEffect(() => {
    genRef.current += 1;
    clearTimer();
    firstDirtyAtRef.current = null;
    draftIdRef.current = seed?.draftId ?? null;
    agentIdRef.current = seed?.agentId ?? null;
    persistedRef.current = seed ? JSON.stringify(seed.baseline) : null;
    currentRef.current = null;
    setDraftId(seed?.draftId ?? null);
    setLastSavedAt(seed?.savedAt ? Date.parse(seed.savedAt) : null);
    setStatus("clean");
    setError(null);
  }, [seed, clearTimer]);

  const save = useCallback(async () => {
    const gen = genRef.current;
    if (savingRef.current) return; // the running save reschedules if the doc moved again
    const snap = currentRef.current;
    if (!snap || snap.snapshot === persistedRef.current) return;
    savingRef.current = true;
    setStatus("saving");
    try {
      // Every await below can outlive a seed change or a publish — each post-await step
      // re-checks the generation BEFORE touching refs or the server, so a stale save can
      // neither hijack the next document's draft id nor mint a row for a dead session.
      if (draftIdRef.current) {
        try {
          const rec = await api.updateDraft(draftIdRef.current, { ir: snap.ir });
          if (gen !== genRef.current) return;
          // Keep the detail cache in lockstep so reopening ?draft=<id> never seeds a stale
          // document (the baseline of a stale seed would then overwrite newer server state).
          qc.setQueryData(keys.draft(rec.id), rec);
        } catch (e) {
          if (gen !== genRef.current) return;
          // The draft was discarded elsewhere (another tab, the Agents page) — recover by
          // minting a fresh one rather than failing every subsequent autosave.
          if (e instanceof ApiError && e.status === 404) {
            draftIdRef.current = null;
          } else {
            throw e;
          }
        }
      }
      if (!draftIdRef.current) {
        if (gen !== genRef.current) return;
        const rec = await api.createDraft({ ir: snap.ir, agent_id: agentIdRef.current });
        if (gen !== genRef.current) {
          // The document changed identity (or was published) mid-create: the row belongs to a
          // dead session — remove it rather than leave a phantom in the drafts list.
          void api.deleteDraft(rec.id).catch(() => {});
          return;
        }
        draftIdRef.current = rec.id;
        setDraftId(rec.id);
        qc.setQueryData(keys.draft(rec.id), rec);
        qc.invalidateQueries({ queryKey: keys.drafts() });
      }
      if (gen !== genRef.current) return;
      persistedRef.current = snap.snapshot;
      firstDirtyAtRef.current = null;
      setLastSavedAt(Date.now());
      setError(null);
      // Edits landed while the request was in flight — go around again after the usual idle gap.
      if (currentRef.current && currentRef.current.snapshot !== persistedRef.current) {
        setStatus("pending");
        clearTimer();
        timerRef.current = window.setTimeout(() => void save(), DEBOUNCE_MS);
      } else {
        setStatus("clean");
      }
    } catch (e) {
      if (gen !== genRef.current) return;
      setStatus("error");
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      savingRef.current = false;
    }
  }, [qc, clearTimer]);

  // Watch the document: diverged from the persisted snapshot → schedule a save.
  useEffect(() => {
    if (!seed || !ir) return;
    const snapshot = JSON.stringify(ir);
    currentRef.current = { ir, snapshot };
    if (snapshot === persistedRef.current) {
      // e.g. an undo walked back to exactly the saved state — nothing pending anymore.
      if (!savingRef.current) {
        clearTimer();
        firstDirtyAtRef.current = null;
        setStatus((s) => (s === "error" ? s : "clean"));
      }
      return;
    }
    if (firstDirtyAtRef.current == null) firstDirtyAtRef.current = Date.now();
    if (!savingRef.current) setStatus((s) => (s === "error" ? s : "pending"));
    clearTimer();
    const deferredFor = Date.now() - firstDirtyAtRef.current;
    const delay = Math.max(0, Math.min(DEBOUNCE_MS, MAX_DEFER_MS - deferredFor));
    timerRef.current = window.setTimeout(() => void save(), delay);
  }, [ir, seed, save, clearTimer]);

  useEffect(() => clearTimer, [clearTimer]);

  // Last-resort flush when the page is going away mid-debounce: a keepalive PUT outlives the
  // document. Only an existing draft — creating one at unload would race the mint unobserved.
  // Known, accepted race: if a debounced save is in flight when the page hides, this keepalive
  // (carrying the NEWEST snapshot) can land before the older in-flight PUT, which then wins —
  // one edit-burst reverts until the next autosave. Fixing it needs a server-side sequence
  // guard on the draft row (a deliberate contract extension for later); firing the newest
  // snapshot beats guaranteed loss when the tab actually closes.
  useEffect(() => {
    const flushIfHiding = () => {
      const snap = currentRef.current;
      if (draftIdRef.current && snap && snap.snapshot !== persistedRef.current) {
        flushDraftOnUnload(draftIdRef.current, snap.ir);
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") flushIfHiding();
    };
    window.addEventListener("pagehide", flushIfHiding);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("pagehide", flushIfHiding);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  const flush = useCallback(async (): Promise<boolean> => {
    clearTimer();
    // A save already in flight: wait it out, then settle whatever it left behind.
    while (savingRef.current) await new Promise((r) => window.setTimeout(r, 50));
    if (!currentRef.current || currentRef.current.snapshot === persistedRef.current) return true;
    await save();
    return currentRef.current?.snapshot === persistedRef.current;
  }, [save, clearTimer]);

  const markPublished = useCallback(
    async (publishedIr: IRDocument) => {
      genRef.current += 1; // strand any in-flight autosave — its draft is about to be deleted
      clearTimer();
      firstDirtyAtRef.current = null;
      const id = draftIdRef.current;
      draftIdRef.current = null;
      persistedRef.current = JSON.stringify(publishedIr);
      currentRef.current = { ir: publishedIr, snapshot: persistedRef.current };
      setDraftId(null);
      setStatus("clean");
      setError(null);
      if (id) {
        try {
          await api.deleteDraft(id);
        } catch {
          // Already gone (or unreachable) — the stale draft is visible on the Agents page and
          // discardable there; never fail the publish over it.
        }
      }
      // Drop cached drafts lists outright (then refetch the active ones): a merely-stale cache
      // would flash the just-deleted draft back into the resume banner / Agents strip.
      qc.removeQueries({ queryKey: keys.drafts() });
      qc.invalidateQueries({ queryKey: keys.drafts() });
    },
    [qc, clearTimer],
  );

  return {
    status,
    draftId,
    lastSavedAt,
    error,
    hasUnsaved: status === "pending" || status === "saving" || status === "error",
    flush,
    markPublished,
  };
}
