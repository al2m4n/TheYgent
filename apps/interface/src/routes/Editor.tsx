// The editor: render a saved agent's IR on a React Flow canvas, edit basic structure + node
// config, and save it back as an agent (M15 §2). Three columns — palette · canvas · inspector —
// over one IR held as the single source of truth. Loading and saving go through M11; the IR
// (with its `view`) is what crosses the wire, and the server owns the contentHash.

import { useQuery } from "@tanstack/react-query";
import { getRouteApi, useBlocker, useNavigate } from "@tanstack/react-router";
import type { IRDocument } from "@theygent/ir-types";
import { useEffect, useMemo, useRef, useState } from "react";
import { type Selection, relayout } from "../adapter";
import { GraphCanvas } from "../components/GraphCanvas";
import { Inspector } from "../components/Inspector";
import { Palette } from "../components/Palette";
import { Badge, Button, Input } from "../components/ui";
import { blankGraph, fromStoredVersion } from "../lib/agent";
import { ApiError, api } from "../lib/api";
import { sameHashedContent } from "../lib/canonical";
import { latestHash, saveAgent } from "../lib/save";
import { type ValidationIssue, validateGraph } from "../lib/validate";

const routeApi = getRouteApi("/editor");

export function Editor() {
  const { agent: agentId, version } = routeApi.useSearch();
  const navigate = useNavigate();
  const loadingExisting = Boolean(agentId && version);

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
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Bumped by "Tidy" to force the canvas to re-seed positions (a layout-only change is otherwise
  // invisible to the structural re-seed).
  const [reseedKey, setReseedKey] = useState(0);
  const [showIssues, setShowIssues] = useState(false);
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

  const onRevert = () => {
    if (savedSnapshot) {
      setIr(savedSnapshot);
      setSelection(null);
      setSaveError(null);
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
    setSaveError(null);
    try {
      const detail = await saveAgent(ir, existsRef.current);
      existsRef.current = true;
      const hash = latestHash(detail);
      if (hash) setSavedHash(hash.contentHash);
      setSavedSnapshot(ir);
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
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  };

  const onTidy = () => {
    setIr((prev) => (prev ? relayout(prev) : prev));
    setReseedKey((k) => k + 1);
  };

  // Keep the keyboard-shortcut handlers pointing at the current closures.
  actionsRef.current = { save: onSave, deselect: () => setSelection(null) };

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
          <Button onClick={onTidy} title="Re-run the auto-layout to tidy positions">
            Tidy
          </Button>
          {dirty ? <Badge tone="amber">modified</Badge> : <Badge tone="green">saved</Badge>}
          <Button onClick={onRevert} disabled={!dirty} title="Discard changes since the last save">
            Revert
          </Button>
          <Button variant="primary" onClick={onSave} disabled={saving}>
            {saving ? "Saving…" : "Save agent"}
          </Button>
        </div>
      </div>

      {showIssues && (
        <div className="max-h-44 overflow-y-auto border-b border-slate-800 bg-[#0e131c] px-3 py-2">
          {issues.length === 0 ? (
            <p className="text-xs text-emerald-400">No issues — the graph is structurally valid.</p>
          ) : (
            <ul className="space-y-1">
              {issues.map((issue, i) => (
                <li key={`${issue.nodeId ?? issue.edgeId ?? "g"}:${i}`}>
                  <button
                    type="button"
                    className="flex w-full items-start gap-2 rounded px-1.5 py-1 text-left text-xs hover:bg-[#1d2433]"
                    onClick={() => {
                      if (issue.nodeId) setSelection({ kind: "node", id: issue.nodeId });
                      else if (issue.edgeId) setSelection({ kind: "edge", id: issue.edgeId });
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
              ))}
            </ul>
          )}
        </div>
      )}

      {saveError && (
        <div className="border-b border-red-900 bg-red-950 px-3 py-1.5 text-xs text-red-200">
          {saveError}
        </div>
      )}

      {/* body */}
      <div className="grid min-h-0 flex-1 grid-cols-[200px_1fr_320px]">
        <aside className="min-h-0 border-r border-slate-800 bg-[#0b0e14]">
          <Palette />
        </aside>
        <section className="min-h-0">
          {/* key by the opened-agent identity so the canvas remounts (and re-fits) when a different
              agent is opened, but NOT on edits within the same agent. */}
          <GraphCanvas
            key={agentId ? `${agentId}@${version}` : "new"}
            ir={ir}
            onChange={setIr}
            selection={selection}
            onSelect={setSelection}
            reseedKey={reseedKey}
          />
        </section>
        <aside className="min-h-0 border-l border-slate-800 bg-[#0b0e14]">
          <Inspector ir={ir} selection={selection} onChange={setIr} onSelect={setSelection} />
        </aside>
      </div>

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
