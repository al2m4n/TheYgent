// The editor: render an agent's IR on a React Flow canvas, edit structure + node config, and take
// it live. Three columns — palette · canvas · inspector — over one IR held as the single source of
// truth, with a test console docked below. Two persistence tiers with distinct verbs:
//   · SAVE  = the draft (automatic): edits debounce-save to the mutable /drafts resource, so a
//     half-built — even structurally invalid — graph survives a reload or a week away. Cmd+S
//     flushes it immediately.
//   · PUBLISH = the registry (deliberate): an immutable, content-addressed version everyone can
//     see and run. The server owns the contentHash; the draft is deleted on success.
// Testing never leaves the canvas: the test panel runs the CURRENT document through the inline
// graph-run path and the trace stream lights each node as it executes.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getRouteApi, useBlocker, useNavigate } from "@tanstack/react-router";
import type { IRDocument } from "@theygent/ir-types";
import { Check, ChevronLeft, ChevronRight, Play, TriangleAlert, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  type Selection,
  addNode,
  defaultAddPosition,
  relayout,
  withDerivedTools,
} from "../adapter";
import { GraphCanvas } from "../components/GraphCanvas";
import { IRCodeEditor } from "../components/IRCodeEditor";
import { Inspector } from "../components/Inspector";
import { Palette } from "../components/Palette";
import { ResizeHandle } from "../components/ResizeHandle";
import { type RunStateMap, TestPanel } from "../components/TestPanel";
import { TimeAgo } from "../components/TimeAgo";
import { Badge, Button, ErrorBanner, Input, Modal, Spinner } from "../components/ui";
import { ToggleGroup, ToggleGroupItem } from "../components/ui/toggle-group";
import { type DraftAutosave, type DraftSeed, useDraftAutosave } from "../hooks/useDraftAutosave";
import { blankGraph, fromDraft, fromStoredVersion } from "../lib/agent";
import { ApiError, api } from "../lib/api";
import { sameHashedContent } from "../lib/canonical";
import { notify } from "../lib/notify";
import { latestHash, saveAgent } from "../lib/save";
import { type ValidationIssue, validateGraph } from "../lib/validate";
import { keys } from "../queries";

const routeApi = getRouteApi("/editor");

// Undo/redo history for the edited IR. `present` is the live document; `commit` pushes the prior
// present onto `past` and clears the redo stack; `reset` (load / new) starts a fresh, empty history.
// Bounded so a long editing session can't grow it without limit. Layout-only edits (drags) are
// commits too, so undo restores positions — exactly what an undo button is expected to do.
interface IrHistory {
  past: IRDocument[];
  present: IRDocument | null;
  future: IRDocument[];
}
type IrHistAction =
  | { type: "reset"; ir: IRDocument }
  | { type: "commit"; ir: IRDocument }
  | { type: "undo" }
  | { type: "redo" };

const HISTORY_LIMIT = 100;

function irHistoryReducer(s: IrHistory, a: IrHistAction): IrHistory {
  switch (a.type) {
    case "reset":
      return { past: [], present: a.ir, future: [] };
    case "commit":
      if (!s.present || s.present === a.ir) return { ...s, present: a.ir };
      return { past: [...s.past, s.present].slice(-HISTORY_LIMIT), present: a.ir, future: [] };
    case "undo": {
      if (!s.past.length || !s.present) return s;
      return {
        past: s.past.slice(0, -1),
        present: s.past[s.past.length - 1],
        future: [s.present, ...s.future],
      };
    }
    case "redo": {
      if (!s.future.length || !s.present) return s;
      return {
        past: [...s.past, s.present],
        present: s.future[0],
        future: s.future.slice(1),
      };
    }
  }
}

export function Editor() {
  const { agent: agentId, version, draft: draftParam } = routeApi.useSearch();
  const navigate = useNavigate();
  // A ?draft=<id> wins over ?agent/?version — the draft record knows the agent it edits.
  const loadingDraft = Boolean(draftParam);
  const loadingExisting = Boolean(!draftParam && agentId && version);
  // A URL naming an agent but no version (shared / hand-edited) still means "open that agent" —
  // resolve its latest version from the registry and complete the URL rather than silently
  // discarding the agent param and opening a blank graph.
  const resolvingLatest = Boolean(!draftParam && agentId && !version);
  const qc = useQueryClient();

  const { data: agentDetail, error: latestError } = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => api.getAgent(agentId as string),
    enabled: resolvingLatest,
  });
  useEffect(() => {
    if (!resolvingLatest || !agentDetail) return;
    const latest = agentDetail.versions[0]?.version; // versions come newest first
    if (latest) {
      navigate({
        to: "/editor",
        search: { agent: agentId, version: latest },
        replace: true,
      });
    }
  }, [resolvingLatest, agentDetail, agentId, navigate]);

  // The loaded registry version (when opening an existing agent).
  const {
    data: stored,
    isLoading,
    error: loadError,
  } = useQuery({
    queryKey: ["agentVersion", agentId, version],
    queryFn: () => api.getAgentVersion(agentId as string, version as string),
    enabled: loadingExisting,
  });

  // The loaded draft (when opening ?draft=<id> — a reload mid-edit, or "Open draft" anywhere).
  const { data: draftRec, error: draftError } = useQuery({
    queryKey: keys.draft(draftParam ?? ""),
    queryFn: () => api.getDraft(draftParam as string),
    enabled: loadingDraft,
  });

  const [hist, dispatch] = useReducer(irHistoryReducer, {
    past: [],
    present: null,
    future: [],
  });
  const ir = hist.present;
  const canUndo = hist.past.length > 0;
  const canRedo = hist.future.length > 0;
  // Undo/redo can revert position-only edits, which don't change the canvas's structural signature —
  // so bump a resync counter to force the canvas to re-seed the reverted positions (without refit).
  const [resyncKey, setResyncKey] = useState(0);
  const undo = useCallback(() => {
    dispatch({ type: "undo" });
    setResyncKey((k) => k + 1);
  }, []);
  const redo = useCallback(() => {
    dispatch({ type: "redo" });
    setResyncKey((k) => k + 1);
  }, []);
  const [selection, setSelection] = useState<Selection>(null);
  // The last PUBLISHED content (when known) — drives Revert and the modified-since-publish badge.
  // Null for a draft-only session (nothing published to revert to).
  const [savedSnapshot, setSavedSnapshot] = useState<IRDocument | null>(null);
  const [savedHash, setSavedHash] = useState<string | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [confirmPublish, setConfirmPublish] = useState(false);
  // Bumped by "Tidy" to force the canvas to re-seed positions (a layout-only change is otherwise
  // invisible to the structural re-seed).
  const [reseedKey, setReseedKey] = useState(0);
  const [showIssues, setShowIssues] = useState(false);
  // Side-panel widths (px) — drag the splitters to resize, double-click to reset. Pure UI layout
  // state (kept in memory, not the IR or localStorage — the drafts/registry APIs are the only
  // persistence).
  const [paletteWidth, setPaletteWidth] = useState(280);
  const [inspectorWidth, setInspectorWidth] = useState(450);
  // Either side panel can be collapsed to a thin rail to give the canvas more room. UI-only state.
  const [paletteCollapsed, setPaletteCollapsed] = useState(false);
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  // Visual canvas vs. raw-IR JSON editor — two views over the one IRDocument. `codeValid` gates
  // Publish so a half-typed (unparseable) IR in the code view can't be published.
  const [mode, setMode] = useState<"visual" | "code">("visual");
  const [codeValid, setCodeValid] = useState(true);
  // The node/edge to flash on the canvas while hovering its issue (display only — not selection).
  const [highlight, setHighlight] = useState<Selection>(null);
  // The docked test console + the live per-node execution state it feeds onto the canvas.
  const [testOpen, setTestOpen] = useState(false);
  const [runState, setRunState] = useState<RunStateMap>({});
  // Whether the open agent already exists in the registry (drives create vs add-version).
  const existsRef = useRef(false);
  // Which graph identity is currently seeded into `ir`, so we seed exactly ONCE per identity and
  // never re-create the IR on subsequent renders (re-seeding every render wipes the canvas — it
  // would render for a frame, then the next seed replaces it). A new blank graph keys as "new".
  const seededRef = useRef<string | null>(null);
  // What the autosave loop measures against: the seeded identity + its baseline document. Set only
  // when seeding, so the canvas key and the autosave state survive a URL param swap (adopting a
  // freshly minted draft id into the URL must not remount or re-baseline anything).
  const [seed, setSeed] = useState<DraftSeed | null>(null);

  // Seed the IR: a loaded draft, a loaded registry version, or a fresh blank graph for "new".
  useEffect(() => {
    if (loadingDraft) {
      if (!draftRec) return; // query still loading — keep the spinner
      const key = `draft:${draftRec.id}`;
      if (seededRef.current === key) return;
      seededRef.current = key;
      const loaded = fromDraft(draftRec);
      dispatch({ type: "reset", ir: loaded });
      // The draft doesn't carry the published baseline — Revert/modified track the registry only
      // when a published version was loaded directly.
      setSavedSnapshot(null);
      setSavedHash(null);
      setSelection(null);
      existsRef.current = Boolean(draftRec.agent_id);
      setSeed({
        key,
        baseline: loaded,
        draftId: draftRec.id,
        agentId: draftRec.agent_id,
        savedAt: draftRec.updated_at,
      });
      return;
    }
    if (resolvingLatest) return; // the URL is still being completed with the latest version — don't seed a blank graph
    if (loadingExisting) {
      if (!stored) return; // query still loading — keep the spinner, don't seed yet
      const key = `${stored.agent_id}@${stored.version}`;
      if (seededRef.current === key) return; // already seeded this exact version
      seededRef.current = key;
      const loaded = fromStoredVersion(stored);
      dispatch({ type: "reset", ir: loaded });
      setSavedSnapshot(loaded);
      setSavedHash(stored.content_hash);
      setSelection(null);
      existsRef.current = true;
      setSeed({
        key,
        baseline: loaded,
        draftId: null,
        agentId: stored.agent_id,
        savedAt: null,
      });
    } else {
      if (seededRef.current === "new") return; // already seeded the blank graph
      seededRef.current = "new";
      const blank = blankGraph("agent.untitled", "Untitled agent");
      dispatch({ type: "reset", ir: blank });
      setSavedSnapshot(blank);
      setSavedHash(null);
      setSelection(null);
      existsRef.current = false;
      setSeed({ key: "new", baseline: blank, draftId: null, agentId: null, savedAt: null });
    }
  }, [stored, loadingExisting, resolvingLatest, loadingDraft, draftRec]);

  // The draft autosave loop — every divergence from the seeded document debounce-saves.
  const autosave = useDraftAutosave(seed, ir);

  // Suppresses the leave-guard for our OWN programmatic navigations (post-publish, the draft-id
  // URL adoption) — they fire before the guarded state recomputes.
  const savingNavRef = useRef(false);

  // The first autosave of a session MINTS a draft — adopt its id into the URL (replace, no history
  // entry) so a reload reopens this exact session. Pre-mark the identity as seeded: the param swap
  // must not re-seed the document or remount the canvas mid-edit.
  useEffect(() => {
    if (!autosave.draftId || draftParam === autosave.draftId) return;
    seededRef.current = `draft:${autosave.draftId}`;
    savingNavRef.current = true;
    navigate({ to: "/editor", search: { draft: autosave.draftId }, replace: true });
    window.setTimeout(() => {
      savingNavRef.current = false;
    }, 0);
  }, [autosave.draftId, draftParam, navigate]);

  // "Modified" = the would-be-hashed content diverges from the last PUBLISHED snapshot (a pure
  // layout change is NOT modified — layout lives in the unhashed `view` block). Drives Revert.
  const dirty = useMemo(
    () => (ir && savedSnapshot ? !sameHashedContent(ir, savedSnapshot) : false),
    [ir, savedSnapshot],
  );

  // Unparseable JSON in the code view lives ONLY in that component's local buffer — the last
  // valid parse is what `ir` (and thus the draft) holds. Leaving while diverged would silently
  // discard the typed text, so it guards the exits alongside unsaved draft edits.
  const codeDiverged = mode === "code" && !codeValid;

  // With autosave, leaving is only unsafe while edits haven't reached the draft yet (debouncing,
  // in flight, or failed) — or while the code view holds unparsed text. Both the in-app guard and
  // the native beforeunload key off that; the modal below offers a save-and-leave.
  const blocker = useBlocker({
    shouldBlockFn: () => (autosave.hasUnsaved || codeDiverged) && !savingNavRef.current,
    enableBeforeUnload: () => (autosave.hasUnsaved || codeDiverged) && !savingNavRef.current,
    withResolver: true,
  });

  // An older draft of the agent being viewed (from a previous session, maybe another tab) — offer
  // to resume it rather than silently editing beside it. Our own live draft is excluded.
  const resumeQuery = useQuery({
    queryKey: [...keys.drafts(), { agent: agentId ?? "" }],
    queryFn: () => api.listDrafts({ agent_id: agentId as string }),
    enabled: Boolean(agentId && !draftParam),
  });
  const [resumeDismissed, setResumeDismissed] = useState<string | null>(null);
  const resumable = (resumeQuery.data ?? [])
    .filter((d) => d.id !== autosave.draftId && d.id !== resumeDismissed)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0];

  // Selecting a node or edge always reveals the inspector — that's where the selection's config is
  // edited, so a collapsed inspector would hide the very panel the click was meant to open.
  useEffect(() => {
    if (selection) setInspectorCollapsed(false);
  }, [selection]);

  // A different document arrived (open draft, publish re-point, new): the previous test run's
  // node lighting belongs to the old graph — clear it. The test panel itself is keyed by the
  // same identity below, so its streams abort on the remount.
  const seedKey = seed?.key;
  // biome-ignore lint/correctness/useExhaustiveDependencies: clear only when the graph identity changes
  useEffect(() => {
    setRunState({});
  }, [seedKey]);

  const onRevert = () => {
    if (savedSnapshot) {
      dispatch({ type: "commit", ir: savedSnapshot });
      setResyncKey((k) => k + 1); // revert may restore positions — re-seed them onto the canvas
      setSelection(null);
      notify.dismiss("editor-publish-error"); // clear a stale publish error — the edits causing it are gone
    }
  };

  // Live, in-editor validation — the FAST mirror of the backend's `validate_graph` (lib/validate).
  // Surfaces structural problems inline so they're caught before Publish, not as a 400. Drafts
  // save regardless — a work in progress is allowed to be broken.
  const issues = useMemo<ValidationIssue[]>(() => (ir ? validateGraph(ir) : []), [ir]);
  const errorCount = issues.filter((i) => i.severity === "error").length;

  // Keyboard shortcuts (Cmd/Ctrl+S saves the draft now, Esc deselect). Registered once; reads the
  // latest handlers through a ref so the listener never needs re-binding.
  const actionsRef = useRef<{
    save: () => void;
    deselect: () => void;
    undo: () => void;
    redo: () => void;
  }>({
    save: () => {},
    deselect: () => {},
    undo: () => {},
    redo: () => {},
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      const key = e.key.toLowerCase();
      if (mod && key === "s") {
        e.preventDefault();
        actionsRef.current.save();
      } else if (mod && (key === "z" || key === "y")) {
        // Let native text-undo win inside form fields / the code editor — only the canvas graph
        // gets the IR undo/redo.
        const t = e.target as HTMLElement | null;
        if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
        e.preventDefault();
        if (key === "y" || e.shiftKey) actionsRef.current.redo();
        else actionsRef.current.undo();
      } else if (e.key === "Escape") {
        actionsRef.current.deselect();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Every structural/config edit re-derives `ir.tools` (the global tool registry) from the
  // wired tool nodes, so it stays in sync as you build — like `ir.models` fills in when you bind a
  // model. Load/revert/relayout use the raw setter (no derive needed; load is already in sync).
  // Declared BEFORE the early returns below so the hook order is identical on the loading and the
  // loaded renders (Rules of Hooks — an early `return` must never sit between two hook calls).
  const applyIr = useCallback(
    (next: IRDocument) => dispatch({ type: "commit", ir: withDerivedTools(next) }),
    [],
  );

  if (loadError || latestError || draftError) {
    return (
      <Centered>
        <ErrorBanner error={loadError ?? latestError ?? draftError} />
      </Centered>
    );
  }
  if (resolvingLatest && agentDetail && agentDetail.versions.length === 0) {
    return (
      <Centered>
        <ErrorBanner error={`Agent "${agentId}" has no saved versions to open.`} />
      </Centered>
    );
  }
  // The draft branch gates on its own record being absent (not a query flag): an in-editor
  // transition to ?draft=<id> keeps the previous document in `ir`, and rendering IT while the
  // draft fetches would show the wrong graph for a beat.
  if (!ir || (loadingExisting && isLoading) || (loadingDraft && !draftRec) || resolvingLatest) {
    return (
      <Centered>
        <Spinner label="Loading agent…" />
      </Centered>
    );
  }

  const patchEnvelope = (patch: Partial<IRDocument>) =>
    dispatch({ type: "commit", ir: { ...ir, ...patch } });

  const onPublish = async () => {
    // The confirm modal STAYS OPEN (buttons disabled) while the POST is in flight: its overlay
    // blocks canvas/input edits, so nothing can change the document mid-publish and be silently
    // clobbered by the post-publish re-seed.
    setPublishing(true);
    try {
      const detail = await saveAgent(ir, existsRef.current);
      existsRef.current = true;
      const hash = latestHash(detail);
      if (hash) setSavedHash(hash.contentHash);
      setSavedSnapshot(ir);
      // The registry owns this content now — drop the bridging draft, reset the autosave baseline.
      await autosave.markPublished(ir);
      notify.dismiss("editor-publish-error"); // a prior failure (e.g. version_conflict) is now resolved
      // Refresh the registry caches so a freshly-published version is visible everywhere it's
      // consumed — notably the per-agent bench, which pins a version from this list. Without this,
      // publishing a new version (e.g. a model swap) leaves the bench running the STALE latest.
      qc.invalidateQueries({ queryKey: ["agents"] });
      qc.invalidateQueries({ queryKey: ["agent", ir.id] });
      qc.invalidateQueries({ queryKey: ["agentVersion", ir.id] });
      setConfirmPublish(false);
      // Re-point the URL at the now-published version so a reload re-opens it (replace, so Back
      // never lands on the now-deleted ?draft= entry). Suppress the leave-guard for this nav.
      savingNavRef.current = true;
      navigate({ to: "/editor", search: { agent: ir.id, version: ir.version }, replace: true });
      window.setTimeout(() => {
        savingNavRef.current = false;
      }, 0);
    } catch (e) {
      setConfirmPublish(false);
      const msg =
        e instanceof ApiError
          ? `${e.message} (${e.code})`
          : ((e as Error).message ?? "publish failed");
      // One place for errors: the global toast. A stable id replaces a prior publish error rather
      // than stacking, and lets a later success/revert dismiss it (see onPublish success + onRevert).
      notify.error(msg, { id: "editor-publish-error" });
    } finally {
      setPublishing(false);
    }
  };

  const onTidy = () => {
    dispatch({ type: "commit", ir: relayout(ir) });
    setReseedKey((k) => k + 1);
  };

  // Click-to-add from the palette (drag stays the precise-placement path). The spot is a cascade
  // off the last node, derived from the IR alone — the editor never reaches into the canvas
  // viewport, so React Flow stays behind the adapter.
  const onPaletteAdd = (type: string) => applyIr(addNode(ir, type, defaultAddPosition(ir)));

  // Cmd/Ctrl+S flushes the draft save immediately — the draft has no validation gate (saving a
  // broken work-in-progress is the feature). Publish stays a deliberate button click.
  actionsRef.current = {
    save: () => {
      void autosave.flush();
    },
    deselect: () => setSelection(null),
    undo,
    redo,
  };

  const publishDisabled = publishing || errorCount > 0 || codeDiverged;

  return (
    <div className="relative flex h-full flex-col">
      {/* toolbar */}
      <div className="flex items-center gap-3 border-b border-slate-800 bg-[var(--c-surface)] px-3 py-2">
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-2 text-[11px] text-slate-500">
            id
            <Input
              className="!w-44 mono h-7 text-xs"
              value={ir.id}
              disabled={existsRef.current}
              onChange={(e) => patchEnvelope({ id: e.target.value })}
            />
          </label>
          <label className="flex items-center gap-2 text-[11px] text-slate-500">
            name
            <Input
              className="!w-44 h-7 text-xs"
              value={ir.name}
              onChange={(e) => patchEnvelope({ name: e.target.value })}
            />
          </label>
          <label className="flex items-center gap-2 text-[11px] text-slate-500">
            version
            <Input
              className="!w-24 mono h-7 text-xs"
              value={ir.version}
              onChange={(e) => patchEnvelope({ version: e.target.value })}
            />
          </label>
        </div>

        <div className="ml-auto flex items-center gap-3">
          {/* Visual ⇄ Code: two views over the one IR */}
          <ToggleGroup
            type="single"
            size="sm"
            variant="outline"
            value={mode}
            onValueChange={(next) => {
              // Radix reports "" when the active item is re-clicked — the editor always has a mode.
              if (next) setMode(next as "visual" | "code");
            }}
            aria-label="Editor view"
          >
            {(["visual", "code"] as const).map((m) => {
              // Switching away from an unparseable code edit would silently destroy the typed
              // text (the code view only commits valid JSON upward) — block it until it parses.
              const blockedByInvalidJson = m === "visual" && mode === "code" && !codeValid;
              return (
                <ToggleGroupItem
                  key={m}
                  value={m}
                  disabled={blockedByInvalidJson}
                  className="capitalize data-[state=on]:bg-primary data-[state=on]:text-primary-foreground"
                  title={
                    blockedByInvalidJson
                      ? "Fix the invalid JSON before switching back to Visual"
                      : m === "visual"
                        ? "Edit on the canvas"
                        : "Edit the raw IR as JSON"
                  }
                >
                  {m}
                </ToggleGroupItem>
              );
            })}
          </ToggleGroup>
          {savedHash && (
            <span
              className="mono max-w-[260px] truncate text-[11px] text-slate-500"
              title={savedHash}
            >
              {savedHash}
            </span>
          )}
          {/* validation indicator — toggles the issues panel */}
          <button
            type="button"
            onClick={() => setShowIssues((s) => !s)}
            title="Show validation issues"
            className="rounded px-1.5 py-0.5 text-[11px] font-medium hover:bg-[var(--c-hover)]"
          >
            {errorCount > 0 ? (
              <span className="inline-flex items-center gap-1 text-red-700 dark:text-red-300">
                <TriangleAlert size={12} /> {errorCount} issue{errorCount === 1 ? "" : "s"}
              </span>
            ) : issues.length > 0 ? (
              <span className="inline-flex items-center gap-1 text-amber-700 dark:text-amber-300">
                <TriangleAlert size={12} /> {issues.length} warning{issues.length === 1 ? "" : "s"}
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 text-emerald-700 dark:text-emerald-400">
                <Check size={12} /> valid
              </span>
            )}
          </button>
          <SaveStatus autosave={autosave} savedHash={savedHash} dirty={dirty} />
          {savedSnapshot && savedHash && (
            <Button
              onClick={onRevert}
              disabled={!dirty}
              title="Discard changes since the last published version"
            >
              Revert
            </Button>
          )}
          <Button
            onClick={() => {
              setTestOpen((o) => {
                if (o) setRunState({}); // closing clears the canvas's run lighting
                return !o;
              });
            }}
            aria-pressed={testOpen}
            title="Test-run the current graph without leaving the canvas"
          >
            <span className="inline-flex items-center gap-1.5">
              <Play size={13} aria-hidden /> Test
            </span>
          </Button>
          <Button
            variant="primary"
            onClick={() => setConfirmPublish(true)}
            disabled={publishDisabled}
            title={
              mode === "code" && !codeValid
                ? "Fix the invalid JSON before publishing"
                : errorCount > 0
                  ? `Fix ${errorCount} validation error${errorCount === 1 ? "" : "s"} before publishing`
                  : "Publish an immutable version everyone can see and run"
            }
          >
            {publishing ? "Publishing…" : "Publish"}
          </Button>
        </div>
      </div>

      {/* an older draft of this agent exists — offer to resume instead of editing beside it */}
      {resumable && (
        <div className="flex items-center gap-3 border-b border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-700 dark:text-amber-300">
          <TriangleAlert size={13} aria-hidden className="shrink-0" />
          <span>
            A draft of this agent has unpublished changes (saved{" "}
            <TimeAgo iso={resumable.updated_at} />
            ).
          </span>
          <Button
            className="h-6 px-2 text-xs"
            onClick={() => navigate({ to: "/editor", search: { draft: resumable.id } })}
          >
            Open draft
          </Button>
          <Button
            variant="ghost"
            className="h-6 px-2 text-xs"
            onClick={async () => {
              try {
                await api.deleteDraft(resumable.id);
              } catch {
                /* already gone */
              }
              qc.invalidateQueries({ queryKey: keys.drafts() });
            }}
          >
            Discard it
          </Button>
          <button
            type="button"
            onClick={() => setResumeDismissed(resumable.id)}
            title="Dismiss"
            aria-label="Dismiss draft notice"
            className="ml-auto text-amber-700/70 hover:text-amber-700 dark:text-amber-300/70 dark:hover:text-amber-300"
          >
            <X size={13} />
          </button>
        </div>
      )}

      {/* Issues panel — FLOATS over the canvas (absolute) so toggling it never reflows the editor.
          Hovering an item flashes the node/edge it points at; clicking selects it. */}
      {showIssues && (
        <div className="absolute top-[46px] right-3 z-30 max-h-[60vh] w-[380px] overflow-hidden rounded-lg border border-slate-700 bg-[var(--c-surface)] shadow-2xl">
          <div className="flex items-center justify-between border-b border-slate-800 px-3 py-1.5">
            <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
              Validation
            </span>
            <button
              type="button"
              onClick={() => setShowIssues(false)}
              title="Close"
              aria-label="Close validation panel"
              className="text-slate-500 hover:text-slate-300"
            >
              <X size={14} />
            </button>
          </div>
          <div className="max-h-[calc(60vh-34px)] overflow-y-auto p-2">
            {issues.length === 0 ? (
              <p className="px-1 py-1 text-xs text-emerald-700 dark:text-emerald-400">
                No issues — the graph is structurally valid.
              </p>
            ) : (
              <ul className="space-y-1">
                {issues.map((issue, i) => {
                  const target: Selection = issue.nodeId
                    ? { kind: "node", id: issue.nodeId }
                    : issue.edgeId
                      ? { kind: "edge", id: issue.edgeId }
                      : null;
                  return (
                    <li key={`${issue.nodeId ?? issue.edgeId ?? "g"}:${i}`}>
                      <button
                        type="button"
                        className="flex w-full items-start gap-2 rounded px-1.5 py-1 text-left text-xs hover:bg-[var(--c-hover)]"
                        onMouseEnter={() => setHighlight(target)}
                        onMouseLeave={() => setHighlight(null)}
                        onClick={() => {
                          if (target) setSelection(target);
                          setHighlight(null);
                        }}
                      >
                        <span
                          className={`mt-0.5 shrink-0 ${
                            issue.severity === "error"
                              ? "text-red-700 dark:text-red-400"
                              : "text-amber-700 dark:text-amber-400"
                          }`}
                        >
                          {issue.severity === "error" ? (
                            <X size={12} />
                          ) : (
                            <TriangleAlert size={12} />
                          )}
                        </span>
                        <span className="text-slate-300">
                          {(issue.nodeId || issue.edgeId) && (
                            <span className="mono text-slate-500">
                              {issue.nodeId ?? issue.edgeId}:{" "}
                            </span>
                          )}
                          {issue.message}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>
      )}

      {/* body — Visual: three resizable columns (palette · canvas · inspector); Code: the raw IR.
          The test console docks below either view. */}
      <div className="flex min-h-0 flex-1 flex-col">
        {mode === "code" ? (
          <div className="min-h-0 flex-1">
            <IRCodeEditor ir={ir} onChange={applyIr} onValidityChange={setCodeValid} />
          </div>
        ) : (
          <div className="flex min-h-0 flex-1">
            {paletteCollapsed ? (
              <CollapsedRail
                side="left"
                label="Nodes"
                title="Show node palette"
                onExpand={() => setPaletteCollapsed(false)}
              />
            ) : (
              <>
                <aside
                  className="relative min-h-0 shrink-0 overflow-hidden border-r border-slate-800 bg-[var(--c-bg)]"
                  style={{ width: paletteWidth }}
                >
                  <CollapseButton side="left" onClick={() => setPaletteCollapsed(true)} />
                  <Palette onAdd={onPaletteAdd} />
                </aside>
                <ResizeHandle
                  width={paletteWidth}
                  onResize={setPaletteWidth}
                  side="left"
                  min={150}
                  max={420}
                  defaultWidth={280}
                  label="node palette"
                />
              </>
            )}
            <section className="min-h-0 min-w-0 flex-1">
              {/* key by the seeded identity so the canvas remounts (and re-fits) when a different
                  document is opened, but NOT on edits — nor when a freshly minted draft id is
                  adopted into the URL mid-session. */}
              <GraphCanvas
                key={seed?.key ?? "new"}
                ir={ir}
                onChange={applyIr}
                selection={selection}
                onSelect={setSelection}
                reseedKey={reseedKey}
                resyncKey={resyncKey}
                highlight={highlight}
                runState={runState}
                onUndo={undo}
                onRedo={redo}
                canUndo={canUndo}
                canRedo={canRedo}
                onTidy={onTidy}
              />
            </section>
            {inspectorCollapsed ? (
              <CollapsedRail
                side="right"
                label="Inspector"
                title="Show inspector"
                onExpand={() => setInspectorCollapsed(false)}
              />
            ) : (
              <>
                <ResizeHandle
                  width={inspectorWidth}
                  onResize={setInspectorWidth}
                  side="right"
                  min={260}
                  max={620}
                  defaultWidth={450}
                  label="inspector"
                />
                <aside
                  className="relative min-h-0 shrink-0 overflow-hidden border-l border-slate-800 bg-[var(--c-bg)]"
                  style={{ width: inspectorWidth }}
                >
                  <CollapseButton side="right" onClick={() => setInspectorCollapsed(true)} />
                  <Inspector
                    ir={ir}
                    selection={selection}
                    onChange={applyIr}
                    onSelect={setSelection}
                  />
                </aside>
              </>
            )}
          </div>
        )}
        {testOpen && (
          <TestPanel
            // Keyed by document identity: switching documents remounts the panel, whose unmount
            // cleanup aborts both streams — a run can never keep lighting the wrong graph.
            key={seed?.key ?? "new"}
            ir={ir}
            errorCount={errorCount}
            codeInvalid={codeDiverged}
            onClose={() => {
              setTestOpen(false);
              setRunState({});
            }}
            onRunState={setRunState}
            onHoverNode={(id) => setHighlight(id ? { kind: "node", id } : null)}
          />
        )}
      </div>

      {/* publish confirmation — publishing is outward-facing (an immutable version everyone can
          see and run), so it gets one deliberate step; drafts save silently in the background. */}
      {confirmPublish && (
        <Modal
          title="Publish this agent?"
          width="max-w-md"
          // The overlay doubles as the edit lock while the POST is in flight (see onPublish) —
          // no dismissing mid-publish.
          onClose={() => {
            if (!publishing) setConfirmPublish(false);
          }}
        >
          <div className="space-y-4">
            <p className="text-sm text-slate-300">
              Publishing creates an immutable version of <span className="mono">{ir.id}</span> at{" "}
              <span className="mono">v{ir.version}</span> that everyone can see, run, and build on.
              To change it later, publish a new version.
            </p>
            {autosave.draftId && (
              <p className="text-xs text-slate-500">The working draft is removed on success.</p>
            )}
            <div className="flex justify-end gap-2">
              <Button onClick={() => setConfirmPublish(false)} disabled={publishing}>
                Cancel
              </Button>
              <Button variant="primary" onClick={onPublish} disabled={publishing}>
                {publishing ? "Publishing…" : `Publish v${ir.version}`}
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* leave guard: only edits that haven't reached the draft yet are at risk (autosave covers
          the rest); the native browser prompt covers tab close / reload via enableBeforeUnload. */}
      {blocker.status === "blocked" && (
        <Modal title="Leave with unsaved changes?" width="max-w-sm" onClose={() => blocker.reset()}>
          <div className="space-y-4">
            <p className="text-sm text-slate-300">
              {codeDiverged
                ? "The code view has JSON that doesn’t parse yet — it can’t be saved to the draft and will be discarded if you leave."
                : "The latest edits haven’t been saved to the draft yet. Save them before leaving?"}
            </p>
            <div className="flex justify-end gap-2">
              <Button onClick={() => blocker.reset()}>Stay</Button>
              <Button variant="danger" onClick={() => blocker.proceed()}>
                Leave without saving
              </Button>
              {/* Flushing while the code view is diverged would save the LAST VALID parse and
                  silently drop the typed text — fixing the JSON is the only real save. */}
              {!codeDiverged && (
                <Button
                  variant="primary"
                  onClick={async () => {
                    const ok = await autosave.flush();
                    if (ok) blocker.proceed();
                    else {
                      notify.error("Draft save failed — staying on the editor", {
                        id: "editor-draft-error",
                      });
                      blocker.reset();
                    }
                  }}
                >
                  Save draft &amp; leave
                </Button>
              )}
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

// The autosave status, always visible in the toolbar: what the draft knows, what still hasn't
// reached it, and (when clean with no draft) the publish state of the loaded document.
function SaveStatus({
  autosave,
  savedHash,
  dirty,
}: {
  autosave: DraftAutosave;
  savedHash: string | null;
  dirty: boolean;
}) {
  if (autosave.status === "saving") {
    return <span className="text-[11px] text-slate-500">Saving draft…</span>;
  }
  if (autosave.status === "pending") {
    return <span className="text-[11px] text-amber-700 dark:text-amber-400">Unsaved changes</span>;
  }
  if (autosave.status === "error") {
    return (
      <button
        type="button"
        onClick={() => void autosave.flush()}
        title={autosave.error ?? "Draft save failed"}
        className="rounded px-1.5 py-0.5 text-[11px] font-medium text-red-700 hover:bg-[var(--c-hover)] dark:text-red-300"
      >
        Draft save failed — retry
      </button>
    );
  }
  if (autosave.draftId) {
    return (
      <span className="text-[11px] text-slate-500">
        Draft saved{" "}
        <TimeAgo iso={autosave.lastSavedAt ? new Date(autosave.lastSavedAt).toISOString() : null} />
      </span>
    );
  }
  if (savedHash) {
    return dirty ? <Badge tone="amber">modified</Badge> : <Badge tone="green">published</Badge>;
  }
  return <Badge tone="slate">not published</Badge>;
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center text-sm text-slate-500">{children}</div>
  );
}

// Chevron tucked into a panel's top INNER corner — the edge nearest the canvas — that collapses it to
// a rail: top-right on the left panel, top-left on the right panel, so both controls sit along the
// canvas boundary instead of against the window edges. The arrow points outward, toward the edge the
// panel folds into.
function CollapseButton({ side, onClick }: { side: "left" | "right"; onClick: () => void }) {
  const label = side === "left" ? "Collapse palette" : "Collapse inspector";
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      aria-expanded={true}
      className={`absolute top-2 z-10 flex h-5 w-5 items-center justify-center rounded text-slate-500 hover:bg-[var(--c-hover)] hover:text-slate-200 ${
        side === "left" ? "right-2" : "left-2"
      }`}
    >
      {side === "left" ? <ChevronLeft size={14} /> : <ChevronRight size={14} />}
    </button>
  );
}

// A collapsed side panel: a thin vertical rail that reopens the panel when clicked ANYWHERE — the
// expand chevron OR the rotated label are one target, so the folded panel's name is a live affordance,
// not just decoration. The chevron points inward (toward the canvas it reopens over); the label keeps
// the panel identifiable while folded.
function CollapsedRail({
  side,
  label,
  title,
  onExpand,
}: {
  side: "left" | "right";
  label: string;
  title: string;
  onExpand: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onExpand}
      title={title}
      aria-label={title}
      aria-expanded={false}
      className={`group/rail flex w-8 min-h-0 shrink-0 flex-col items-center bg-[var(--c-bg)] text-slate-400 transition-colors hover:bg-[var(--c-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500 ${
        side === "left" ? "border-r" : "border-l"
      } border-slate-800`}
    >
      <span className="mt-2 flex h-6 w-6 items-center justify-center group-hover/rail:text-slate-100">
        {side === "left" ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
      </span>
      <span className="mt-3 select-none text-[10px] uppercase tracking-wide text-slate-600 [writing-mode:vertical-rl] group-hover/rail:text-slate-300">
        {label}
      </span>
    </button>
  );
}
