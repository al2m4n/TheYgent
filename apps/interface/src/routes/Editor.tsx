// The editor: render a saved agent's IR on a React Flow canvas, edit basic structure + node
// config, and save it back as an agent (M15 §2). Three columns — palette · canvas · inspector —
// over one IR held as the single source of truth. Loading and saving go through M11; the IR
// (with its `view`) is what crosses the wire, and the server owns the contentHash.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getRouteApi, useBlocker, useNavigate } from "@tanstack/react-router";
import type { IRDocument } from "@theygent/ir-types";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type Selection, relayout, withDerivedTools } from "../adapter";
import { GraphCanvas } from "../components/GraphCanvas";
import { IRCodeEditor } from "../components/IRCodeEditor";
import { Inspector } from "../components/Inspector";
import { Palette } from "../components/Palette";
import { ResizeHandle } from "../components/ResizeHandle";
import { Badge, Button, Input } from "../components/ui";
import { blankGraph, fromStoredVersion } from "../lib/agent";
import { ApiError, api } from "../lib/api";
import { sameHashedContent } from "../lib/canonical";
import { notify } from "../lib/notify";
import { latestHash, saveAgent } from "../lib/save";
import { type ValidationIssue, validateGraph } from "../lib/validate";

const routeApi = getRouteApi("/editor");

export function Editor() {
  const { agent: agentId, version } = routeApi.useSearch();
  const navigate = useNavigate();
  const loadingExisting = Boolean(agentId && version);
  const qc = useQueryClient();

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

  const [ir, setIr] = useState<IRDocument | null>(null);
  const [selection, setSelection] = useState<Selection>(null);
  const [savedSnapshot, setSavedSnapshot] = useState<IRDocument | null>(null);
  const [savedHash, setSavedHash] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Bumped by "Tidy" to force the canvas to re-seed positions (a layout-only change is otherwise
  // invisible to the structural re-seed).
  const [reseedKey, setReseedKey] = useState(0);
  const [showIssues, setShowIssues] = useState(false);
  // Side-panel widths (px) — drag the splitters to resize, double-click to reset. Pure UI layout
  // state (kept in memory, not the IR or localStorage — persistence is the registry, §Do-NOT).
  const [paletteWidth, setPaletteWidth] = useState(200);
  const [inspectorWidth, setInspectorWidth] = useState(320);
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
    if (loadingExisting) {
      if (!stored) return; // query still loading — keep the spinner, don't seed yet
      const key = `${stored.agent_id}@${stored.version}`;
      if (seededRef.current === key) return; // already seeded this exact version
      seededRef.current = key;
      const loaded = fromStoredVersion(stored);
      setIr(loaded);
      setSavedSnapshot(loaded);
      setSavedHash(stored.content_hash);
      setSelection(null);
      existsRef.current = true;
    } else {
      if (seededRef.current === "new") return; // already seeded the blank graph
      seededRef.current = "new";
      const blank = blankGraph("agent.untitled", "Untitled agent");
      setIr(blank);
      setSavedSnapshot(blank);
      setSavedHash(null);
      setSelection(null);
      existsRef.current = false;
    }
  }, [stored, loadingExisting]);

  // "Modified" = the would-be-hashed content diverges from the last saved snapshot (a pure layout
  // change is NOT dirty — decision §1.4). This drives the Revert button and the leave-guard.
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
      setIr(savedSnapshot);
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
  const actionsRef = useRef<{ save: () => void; deselect: () => void }>({
    save: () => {},
    deselect: () => {},
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        actionsRef.current.save();
      } else if (e.key === "Escape") {
        actionsRef.current.deselect();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // M22: every structural/config edit re-derives `ir.tools` (the global tool registry) from the
  // wired tool nodes, so it stays in sync as you build — like `ir.models` fills in when you bind a
  // model. Load/revert/relayout use the raw setter (no derive needed; load is already in sync).
  // Declared BEFORE the early returns below so the hook order is identical on the loading and the
  // loaded renders (Rules of Hooks — an early `return` must never sit between two hook calls).
  const applyIr = useCallback((next: IRDocument) => setIr(withDerivedTools(next)), []);

  if (loadError) {
    return (
      <Centered>
        <p className="text-sm text-red-300">Could not load agent: {(loadError as Error).message}</p>
      </Centered>
    );
  }
  if (!ir || (loadingExisting && isLoading)) {
    return <Centered>Loading…</Centered>;
  }

  const patchEnvelope = (patch: Partial<IRDocument>) =>
    setIr((prev) => (prev ? { ...prev, ...patch } : prev));

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
      // Re-point the URL at the now-saved version so a reload re-opens it (M15 acceptance). Suppress
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
    setIr((prev) => (prev ? relayout(prev) : prev));
    setReseedKey((k) => k + 1);
  };

  // Keep the keyboard-shortcut handlers pointing at the current closures. Cmd/Ctrl+S honors the
  // same validation gate as the Save button — no saving an invalid graph from the keyboard.
  actionsRef.current = {
    save: () => {
      if (!saving && errorCount === 0 && !(mode === "code" && !codeValid)) onSave();
    },
    deselect: () => setSelection(null),
  };

  return (
    <div className="relative flex h-full flex-col">
      {/* toolbar */}
      <div className="flex items-center gap-3 border-b border-slate-800 bg-[#0e131c] px-3 py-2">
        <div className="flex items-center gap-2">
          <label className="text-[11px] text-slate-500">id</label>
          <Input
            className="!w-44 mono !py-1 text-xs"
            value={ir.id}
            disabled={existsRef.current}
            onChange={(e) => patchEnvelope({ id: e.target.value })}
          />
          <label className="text-[11px] text-slate-500">name</label>
          <Input
            className="!w-44 !py-1 text-xs"
            value={ir.name}
            onChange={(e) => patchEnvelope({ name: e.target.value })}
          />
          <label className="text-[11px] text-slate-500">version</label>
          <Input
            className="!w-24 mono !py-1 text-xs"
            value={ir.version}
            onChange={(e) => patchEnvelope({ version: e.target.value })}
          />
        </div>

        <div className="ml-auto flex items-center gap-3">
          {/* Visual ⇄ Code: two views over the one IR */}
          <div className="flex items-center rounded-md border border-slate-700 p-0.5">
            {(["visual", "code"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                className={`rounded px-2 py-0.5 text-xs font-medium capitalize transition-colors ${
                  mode === m ? "bg-blue-600 text-white" : "text-slate-400 hover:text-slate-200"
                }`}
                title={m === "visual" ? "Edit on the canvas" : "Edit the raw IR as JSON"}
              >
                {m}
              </button>
            ))}
          </div>
          {savedHash ? (
            <span
              className="mono max-w-[260px] truncate text-[11px] text-slate-500"
              title={savedHash}
            >
              {savedHash}
            </span>
          ) : (
            <span className="text-[11px] text-slate-600">unsaved</span>
          )}
          {/* validation indicator — toggles the issues panel */}
          <button
            type="button"
            onClick={() => setShowIssues((s) => !s)}
            title="Show validation issues"
            className="rounded px-1.5 py-0.5 text-[11px] font-medium hover:bg-[#1d2433]"
          >
            {errorCount > 0 ? (
              <span className="text-red-300">
                ⚠ {errorCount} issue{errorCount === 1 ? "" : "s"}
              </span>
            ) : issues.length > 0 ? (
              <span className="text-amber-300">
                ⚠ {issues.length} warning{issues.length === 1 ? "" : "s"}
              </span>
            ) : (
              <span className="text-emerald-400">✓ valid</span>
            )}
          </button>
          <Button
            onClick={onTidy}
            disabled={mode === "code"}
            title="Re-run the auto-layout to tidy positions"
          >
            Tidy
          </Button>
          {dirty ? <Badge tone="amber">modified</Badge> : <Badge tone="green">saved</Badge>}
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
        <div className="absolute top-[46px] right-3 z-30 max-h-[60vh] w-[380px] overflow-hidden rounded-lg border border-slate-700 bg-[#0e131c] shadow-2xl">
          <div className="flex items-center justify-between border-b border-slate-800 px-3 py-1.5">
            <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
              Validation
            </span>
            <button
              type="button"
              onClick={() => setShowIssues(false)}
              title="Close"
              className="text-slate-500 hover:text-slate-300"
            >
              ✕
            </button>
          </div>
          <div className="max-h-[calc(60vh-34px)] overflow-y-auto p-2">
            {issues.length === 0 ? (
              <p className="px-1 py-1 text-xs text-emerald-400">
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
                        className="flex w-full items-start gap-2 rounded px-1.5 py-1 text-left text-xs hover:bg-[#1d2433]"
                        onMouseEnter={() => setHighlight(target)}
                        onMouseLeave={() => setHighlight(null)}
                        onClick={() => {
                          if (target) setSelection(target);
                          setHighlight(null);
                        }}
                      >
                        <span
                          className={issue.severity === "error" ? "text-red-400" : "text-amber-400"}
                        >
                          {issue.severity === "error" ? "✗" : "⚠"}
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
                className="relative min-h-0 shrink-0 overflow-hidden border-r border-slate-800 bg-[#0b0e14]"
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
                max={360}
                defaultWidth={200}
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
              highlight={highlight}
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
                max={520}
                defaultWidth={320}
                label="inspector"
              />
              <aside
                className="relative min-h-0 shrink-0 overflow-hidden border-l border-slate-800 bg-[#0b0e14]"
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
        <div className="absolute inset-0 z-20 flex items-center justify-center bg-black/50">
          <div className="w-[380px] rounded-lg border border-slate-700 bg-[#161b26] p-5 shadow-2xl">
            <h2 className="text-sm font-semibold text-slate-100">Leave with unsaved changes?</h2>
            <p className="mt-1.5 text-xs text-slate-400">
              This agent has changes that haven’t been saved. If you leave now they’ll be lost.
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <Button onClick={() => blocker.reset()}>Stay</Button>
              <Button variant="danger" onClick={() => blocker.proceed()}>
                Leave without saving
              </Button>
            </div>
          </div>
        </div>
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
// (toward the edge it folds into): ‹ on the left panel, › on the right.
function CollapseButton({ side, onClick }: { side: "left" | "right"; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={side === "left" ? "Collapse palette" : "Collapse inspector"}
      className="absolute right-2 top-2 z-10 flex h-5 w-5 items-center justify-center rounded text-slate-500 hover:bg-[#1d2433] hover:text-slate-200"
    >
      {side === "left" ? "‹" : "›"}
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
      className={`flex w-8 min-h-0 shrink-0 flex-col items-center bg-[#0b0e14] ${
        side === "left" ? "border-r" : "border-l"
      } border-slate-800`}
    >
      <button
        type="button"
        onClick={onExpand}
        title={title}
        className="mt-2 flex h-6 w-6 items-center justify-center rounded text-slate-400 hover:bg-[#1d2433] hover:text-slate-100"
      >
        {side === "left" ? "›" : "‹"}
      </button>
      <span className="mt-3 select-none text-[10px] uppercase tracking-wide text-slate-600 [writing-mode:vertical-rl]">
        {label}
      </span>
    </div>
  );
}
