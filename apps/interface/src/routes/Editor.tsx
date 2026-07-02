// The editor: render a saved agent's IR on a React Flow canvas, edit basic structure + node
// config, and save it back as an agent. Three columns — palette · canvas · inspector — over one
// IR held as the single source of truth. Loading and saving go through the agent registry; the IR
// (with its `view`) is what crosses the wire, and the server owns the contentHash.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getRouteApi, useBlocker, useNavigate } from "@tanstack/react-router";
import type { IRDocument } from "@theygent/ir-types";
import { Check, ChevronLeft, ChevronRight, TriangleAlert, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { type Selection, relayout, withDerivedTools } from "../adapter";
import { GraphCanvas } from "../components/GraphCanvas";
import { IRCodeEditor } from "../components/IRCodeEditor";
import { Inspector } from "../components/Inspector";
import { Palette } from "../components/Palette";
import { ResizeHandle } from "../components/ResizeHandle";
import { Badge, Button, ErrorBanner, Input, Modal, Spinner } from "../components/ui";
import { blankGraph, fromStoredVersion } from "../lib/agent";
import { ApiError, api } from "../lib/api";
import { sameHashedContent } from "../lib/canonical";
import { notify } from "../lib/notify";
import { latestHash, saveAgent } from "../lib/save";
import { type ValidationIssue, validateGraph } from "../lib/validate";

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
  const { agent: agentId, version } = routeApi.useSearch();
  const navigate = useNavigate();
  const loadingExisting = Boolean(agentId && version);
  // A URL naming an agent but no version (shared / hand-edited) still means "open that agent" —
  // resolve its latest version from the registry and complete the URL rather than silently
  // discarding the agent param and opening a blank graph.
  const resolvingLatest = Boolean(agentId && !version);
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
  const [savedSnapshot, setSavedSnapshot] = useState<IRDocument | null>(null);
  const [savedHash, setSavedHash] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Bumped by "Tidy" to force the canvas to re-seed positions (a layout-only change is otherwise
  // invisible to the structural re-seed).
  const [reseedKey, setReseedKey] = useState(0);
  const [showIssues, setShowIssues] = useState(false);
  // Side-panel widths (px) — drag the splitters to resize, double-click to reset. Pure UI layout
  // state (kept in memory, not the IR or localStorage — the registry is the only persistence).
  const [paletteWidth, setPaletteWidth] = useState(280);
  const [inspectorWidth, setInspectorWidth] = useState(450);
  // Either side panel can be collapsed to a thin rail to give the canvas more room. UI-only state.
  const [paletteCollapsed, setPaletteCollapsed] = useState(false);
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  // Visual canvas vs. raw-IR JSON editor — two views over the one IRDocument. `codeValid` gates Save
  // so a half-typed (unparseable) IR in the code view can't be saved.
  const [mode, setMode] = useState<"visual" | "code">("visual");
  const [codeValid, setCodeValid] = useState(true);
  // The node/edge to flash on the canvas while hovering its issue (display only — not selection).
  const [highlight, setHighlight] = useState<Selection>(null);
  // Whether the open agent already exists in the registry (drives create vs add-version).
  const existsRef = useRef(false);
  // Which graph identity is currently seeded into `ir`, so we seed exactly ONCE per identity and
  // never re-create the IR on subsequent renders (re-seeding every render wipes the canvas — it
  // would render for a frame, then the next seed replaces it). A new blank graph keys as "new".
  const seededRef = useRef<string | null>(null);

  // Seed the IR: a loaded version, or a fresh blank graph for "new".
  useEffect(() => {
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
    } else {
      if (seededRef.current === "new") return; // already seeded the blank graph
      seededRef.current = "new";
      const blank = blankGraph("agent.untitled", "Untitled agent");
      dispatch({ type: "reset", ir: blank });
      setSavedSnapshot(blank);
      setSavedHash(null);
      setSelection(null);
      existsRef.current = false;
    }
  }, [stored, loadingExisting, resolvingLatest]);

  // "Modified" = the would-be-hashed content diverges from the last saved snapshot (a pure layout
  // change is NOT dirty — layout lives in the unhashed `view` block). This drives the Revert
  // button and the leave-guard.
  const dirty = useMemo(
    () => (ir && savedSnapshot ? !sameHashedContent(ir, savedSnapshot) : false),
    [ir, savedSnapshot],
  );

  // Warn before losing unsaved work — both for in-app navigation (the resolver modal below) AND a
  // browser tab close / reload (`enableBeforeUnload` → the native prompt). `savingNavRef` suppresses
  // the guard for our OWN post-save navigation (which fires before `dirty` recomputes to false).
  const savingNavRef = useRef(false);
  const blocker = useBlocker({
    shouldBlockFn: () => dirty && !savingNavRef.current,
    enableBeforeUnload: () => dirty && !savingNavRef.current,
    withResolver: true,
  });

  // Selecting a node or edge always reveals the inspector — that's where the selection's config is
  // edited, so a collapsed inspector would hide the very panel the click was meant to open.
  useEffect(() => {
    if (selection) setInspectorCollapsed(false);
  }, [selection]);

  const onRevert = () => {
    if (savedSnapshot) {
      dispatch({ type: "commit", ir: savedSnapshot });
      setResyncKey((k) => k + 1); // revert may restore positions — re-seed them onto the canvas
      setSelection(null);
      notify.dismiss("editor-save-error"); // clear a stale save error — the edits causing it are gone
    }
  };

  // Live, in-editor validation — the FAST mirror of the backend's `validate_graph` (lib/validate).
  // Surfaces structural problems inline so they're caught before Save, not as a 400.
  const issues = useMemo<ValidationIssue[]>(() => (ir ? validateGraph(ir) : []), [ir]);
  const errorCount = issues.filter((i) => i.severity === "error").length;

  // Keyboard shortcuts (Cmd/Ctrl+S save, Esc deselect). Registered once; reads the latest handlers
  // through a ref so the listener never needs re-binding (the handlers close over post-guard state).
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

  if (loadError || latestError) {
    return (
      <Centered>
        <ErrorBanner error={loadError ?? latestError} />
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
  if (!ir || (loadingExisting && isLoading) || resolvingLatest) {
    return (
      <Centered>
        <Spinner label="Loading agent…" />
      </Centered>
    );
  }

  const patchEnvelope = (patch: Partial<IRDocument>) =>
    dispatch({ type: "commit", ir: { ...ir, ...patch } });

  const onSave = async () => {
    setSaving(true);
    try {
      const detail = await saveAgent(ir, existsRef.current);
      existsRef.current = true;
      const hash = latestHash(detail);
      if (hash) setSavedHash(hash.contentHash);
      setSavedSnapshot(ir);
      notify.dismiss("editor-save-error"); // a prior failure (e.g. version_conflict) is now resolved
      // Refresh the registry caches so a freshly-saved version is visible everywhere it's consumed —
      // notably the per-agent bench, which pins a version from this list. Without this, saving a new
      // version (e.g. a model swap) leaves the bench running the STALE latest (the "model change
      // didn't apply" bug): the cached agent detail never re-fetched the new version.
      qc.invalidateQueries({ queryKey: ["agents"] });
      qc.invalidateQueries({ queryKey: ["agent", ir.id] });
      qc.invalidateQueries({ queryKey: ["agentVersion", ir.id] });
      // Re-point the URL at the now-saved version so a reload re-opens it. Suppress
      // the leave-guard for this programmatic nav (dirty hasn't recomputed to false yet this tick).
      savingNavRef.current = true;
      navigate({ to: "/editor", search: { agent: ir.id, version: ir.version } });
      window.setTimeout(() => {
        savingNavRef.current = false;
      }, 0);
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? `${e.message} (${e.code})`
          : ((e as Error).message ?? "save failed");
      // One place for errors: the global toast. A stable id replaces a prior save error rather than
      // stacking, and lets a later success/revert dismiss it (see onSave success + onRevert).
      notify.error(msg, { id: "editor-save-error" });
    } finally {
      setSaving(false);
    }
  };

  const onTidy = () => {
    dispatch({ type: "commit", ir: relayout(ir) });
    setReseedKey((k) => k + 1);
  };

  // Keep the keyboard-shortcut handlers pointing at the current closures. Cmd/Ctrl+S honors the
  // same validation gate as the Save button — no saving an invalid graph from the keyboard.
  actionsRef.current = {
    save: () => {
      if (!saving && errorCount === 0 && !(mode === "code" && !codeValid)) onSave();
    },
    deselect: () => setSelection(null),
    undo,
    redo,
  };

  return (
    <div className="relative flex h-full flex-col">
      {/* toolbar */}
      <div className="flex items-center gap-3 border-b border-slate-800 bg-[var(--c-surface)] px-3 py-2">
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-2 text-[11px] text-slate-500">
            id
            <Input
              className="!w-44 mono !py-1 text-xs"
              value={ir.id}
              disabled={existsRef.current}
              onChange={(e) => patchEnvelope({ id: e.target.value })}
            />
          </label>
          <label className="flex items-center gap-2 text-[11px] text-slate-500">
            name
            <Input
              className="!w-44 !py-1 text-xs"
              value={ir.name}
              onChange={(e) => patchEnvelope({ name: e.target.value })}
            />
          </label>
          <label className="flex items-center gap-2 text-[11px] text-slate-500">
            version
            <Input
              className="!w-24 mono !py-1 text-xs"
              value={ir.version}
              onChange={(e) => patchEnvelope({ version: e.target.value })}
            />
          </label>
        </div>

        <div className="ml-auto flex items-center gap-3">
          {/* Visual ⇄ Code: two views over the one IR */}
          <div className="flex items-center rounded-md border border-slate-700 p-0.5">
            {(["visual", "code"] as const).map((m) => {
              // Switching away from an unparseable code edit would silently destroy the typed
              // text (the code view only commits valid JSON upward) — block it until it parses.
              const blockedByInvalidJson = m === "visual" && mode === "code" && !codeValid;
              return (
                <button
                  key={m}
                  type="button"
                  onClick={() => setMode(m)}
                  disabled={blockedByInvalidJson}
                  aria-pressed={mode === m}
                  className={`rounded px-2 py-0.5 text-xs font-medium capitalize transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                    mode === m ? "bg-blue-600 text-white" : "text-slate-400 hover:text-slate-200"
                  }`}
                  title={
                    blockedByInvalidJson
                      ? "Fix the invalid JSON before switching back to Visual"
                      : m === "visual"
                        ? "Edit on the canvas"
                        : "Edit the raw IR as JSON"
                  }
                >
                  {m}
                </button>
              );
            })}
          </div>
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
          <Button
            onClick={onTidy}
            disabled={mode === "code"}
            title="Re-run the auto-layout to tidy positions"
          >
            Tidy
          </Button>
          {savedHash === null && !dirty ? (
            <Badge tone="slate">not saved</Badge>
          ) : dirty ? (
            <Badge tone="amber">modified</Badge>
          ) : (
            <Badge tone="green">saved</Badge>
          )}
          <Button onClick={onRevert} disabled={!dirty} title="Discard changes since the last save">
            Revert
          </Button>
          <Button
            variant="primary"
            onClick={onSave}
            disabled={saving || errorCount > 0 || (mode === "code" && !codeValid)}
            title={
              mode === "code" && !codeValid
                ? "Fix the invalid JSON before saving"
                : errorCount > 0
                  ? `Fix ${errorCount} validation error${errorCount === 1 ? "" : "s"} before saving`
                  : "Save this agent (⌘S)"
            }
          >
            {saving ? "Saving…" : "Save agent"}
          </Button>
        </div>
      </div>

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

      {/* body — Visual: three resizable columns (palette · canvas · inspector); Code: the raw IR */}
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
                <Palette />
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
            {/* key by the opened-agent identity so the canvas remounts (and re-fits) when a different
                agent is opened, but NOT on edits within the same agent. */}
            <GraphCanvas
              key={agentId ? `${agentId}@${version}` : "new"}
              ir={ir}
              onChange={applyIr}
              selection={selection}
              onSelect={setSelection}
              reseedKey={reseedKey}
              resyncKey={resyncKey}
              highlight={highlight}
              onUndo={undo}
              onRedo={redo}
              canUndo={canUndo}
              canRedo={canRedo}
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

      {/* unsaved-changes guard: shown when an in-app navigation is blocked (the native browser
          prompt covers tab close / reload via enableBeforeUnload). */}
      {blocker.status === "blocked" && (
        <Modal title="Leave with unsaved changes?" width="max-w-sm" onClose={() => blocker.reset()}>
          <div className="space-y-4">
            <p className="text-sm text-slate-300">
              This agent has changes that haven’t been saved. If you leave now they’ll be lost.
            </p>
            <div className="flex justify-end gap-2">
              <Button onClick={() => blocker.reset()}>Stay</Button>
              <Button variant="danger" onClick={() => blocker.proceed()}>
                Leave without saving
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center text-sm text-slate-500">{children}</div>
  );
}

// Chevron tucked into a panel's top corner that collapses it to a rail. The arrow points outward
// (toward the edge it folds into): left on the left panel, right on the right.
function CollapseButton({ side, onClick }: { side: "left" | "right"; onClick: () => void }) {
  const label = side === "left" ? "Collapse palette" : "Collapse inspector";
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      className="absolute right-2 top-2 z-10 flex h-5 w-5 items-center justify-center rounded text-slate-500 hover:bg-[var(--c-hover)] hover:text-slate-200"
    >
      {side === "left" ? <ChevronLeft size={14} /> : <ChevronRight size={14} />}
    </button>
  );
}

// A collapsed side panel: a thin vertical rail with an expand chevron (pointing inward, toward the
// canvas it would reopen over) and a rotated label so the panel stays identifiable while folded.
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
    <div
      className={`flex w-8 min-h-0 shrink-0 flex-col items-center bg-[var(--c-bg)] ${
        side === "left" ? "border-r" : "border-l"
      } border-slate-800`}
    >
      <button
        type="button"
        onClick={onExpand}
        title={title}
        aria-label={title}
        className="mt-2 flex h-6 w-6 items-center justify-center rounded text-slate-400 hover:bg-[var(--c-hover)] hover:text-slate-100"
      >
        {side === "left" ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
      </button>
      <span className="mt-3 select-none text-[10px] uppercase tracking-wide text-slate-600 [writing-mode:vertical-rl]">
        {label}
      </span>
    </div>
  );
}
