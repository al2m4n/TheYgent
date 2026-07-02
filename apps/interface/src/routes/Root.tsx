import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import {
  Activity,
  Bot,
  Boxes,
  type LucideIcon,
  MessageSquare,
  Monitor,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Plug,
  Settings,
  SquarePen,
  Sun,
  User,
} from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { LocalCredentials } from "../components/LocalCredentials";
import { Modal } from "../components/ui";
import { NotificationCenter } from "../lib/notify";
import { type ThemePref, useTheme } from "../lib/theme";

// The shell: a collapsible LEFT sidebar + the routed view. The interface is canvas-first, so chrome
// stays quiet and out of the way; collapsing the rail hands the canvas the full width. Each entry is
// one Lucide icon + a label; collapsed, only the icon shows and the label moves to a hover tooltip.

interface NavEntry {
  to: string;
  label: string;
  icon: LucideIcon;
  exact?: boolean;
}

// Order is provisional (a fuller IA pass comes later). The canvas (Agents) leads; the operator
// surfaces (Runs/Threads/Compose) and the registries (Registries/MCP) follow.
const NAV: NavEntry[] = [
  { to: "/", label: "Agents", icon: Bot, exact: true },
  { to: "/runs", label: "Runs", icon: Activity },
  { to: "/threads", label: "Threads", icon: MessageSquare },
  { to: "/compose", label: "Compose", icon: SquarePen },
  { to: "/registries", label: "Registries", icon: Boxes },
  { to: "/mcp", label: "MCP", icon: Plug },
];

// The collapse preference is a pure UI pref (NOT the IR store — that stays the registry API). Persist
// it so the rail keeps its width across reloads; guarded for non-DOM (test) environments.
const COLLAPSE_KEY = "theygent.ui.navCollapsed";

function readCollapsed(): boolean {
  try {
    return typeof localStorage !== "undefined" && localStorage.getItem(COLLAPSE_KEY) === "1";
  } catch {
    return false;
  }
}

export function Root() {
  // `collapsed` is the user's persisted PREFERENCE — it governs every page except the editor.
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [settingsOpen, setSettingsOpen] = useState(false);

  // On the editor (canvas) we auto-collapse the rail to hand the canvas more room, WITHOUT touching
  // the saved preference — so leaving the editor restores whatever the rail was before. `editorOverride`
  // lets the user still expand it while editing; it's transient and resets on every editor enter/leave.
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const isEditor = pathname.startsWith("/editor");
  const [editorOverride, setEditorOverride] = useState<boolean | null>(null);
  const effectiveCollapsed = isEditor ? (editorOverride ?? true) : collapsed;

  useEffect(() => {
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
    } catch {
      // no localStorage (tests) — the in-memory state still drives the UI this session.
    }
  }, [collapsed]);

  // Reset the transient editor override on any editor enter/leave so the auto-collapse re-applies on
  // entry and the saved preference re-applies on exit.
  // biome-ignore lint/correctness/useExhaustiveDependencies: reset only when the editor opens/closes
  useEffect(() => {
    setEditorOverride(null);
  }, [isEditor]);

  // Toggle: on the editor it flips the transient override (doesn't persist); elsewhere it sets the
  // saved preference.
  const setRailCollapsed = (next: boolean) => {
    if (isEditor) setEditorOverride(next);
    else setCollapsed(next);
  };

  return (
    <div className="flex h-full">
      <aside
        className={`flex h-full shrink-0 flex-col border-r border-slate-800 bg-[var(--c-surface)] transition-[width] duration-200 ${
          effectiveCollapsed ? "w-14" : "w-56"
        }`}
      >
        {/* Brand + (when expanded) the collapse toggle. */}
        <div className="flex h-11 shrink-0 items-center border-b border-slate-800 px-2.5">
          <Link
            to="/"
            className="flex min-w-0 items-center gap-2 text-sm font-semibold text-slate-100"
            aria-label="theygent — home"
          >
            <span className="shrink-0 text-base text-blue-600 dark:text-blue-400">◆</span>
            {!effectiveCollapsed && <span className="truncate">theygent</span>}
          </Link>
          {!effectiveCollapsed && (
            <button
              type="button"
              onClick={() => setRailCollapsed(true)}
              aria-label="Collapse sidebar"
              title="Collapse sidebar"
              className="ml-auto rounded p-1 text-slate-500 transition-colors hover:bg-slate-800 hover:text-slate-200"
            >
              <PanelLeftClose size={16} />
            </button>
          )}
        </div>

        {/* When collapsed, the toggle becomes a full-width expand affordance under the brand. */}
        {effectiveCollapsed && (
          <button
            type="button"
            onClick={() => setRailCollapsed(false)}
            aria-label="Expand sidebar"
            title="Expand sidebar"
            className="flex h-9 shrink-0 items-center justify-center text-slate-500 transition-colors hover:bg-slate-800/60 hover:text-slate-200"
          >
            <PanelLeftOpen size={16} />
          </button>
        )}

        {/* Primary navigation. */}
        <nav className="flex-1 space-y-1 px-2 py-2">
          {NAV.map((item) => (
            <NavItem key={item.to} item={item} collapsed={effectiveCollapsed} />
          ))}
        </nav>

        {/* Bottom: settings + the user/profile entry — the latter opens the settings modal. */}
        <div className="shrink-0 space-y-1 border-t border-slate-800 px-2 py-2">
          <SideButton
            icon={Settings}
            label="Settings"
            collapsed={effectiveCollapsed}
            onClick={() => setSettingsOpen(true)}
          />
          <ProfileButton collapsed={effectiveCollapsed} onClick={() => setSettingsOpen(true)} />
        </div>
      </aside>

      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}

      {/* The single scroll region: the document never scrolls (body is overflow-hidden), so the
          sidebar stays fixed while a long page scrolls here. Routes that own their height (the
          canvas Editor) sit exactly h-full and never overflow. */}
      <main className="min-h-0 min-w-0 flex-1 overflow-y-auto">
        <Outlet />
      </main>

      {/* The one central place for messages + live download progress, bottom-right, above every
          page and persistent across navigation. */}
      <NotificationCenter />
    </div>
  );
}

// A row that shows label-to-the-right when collapsed (a delayed hover tooltip, so resting on the icon
// reveals it). Lives outside any scroll/overflow container so it can extend past the rail's edge.
function CollapsedTip({ label }: { label: string }) {
  return (
    <span className="pointer-events-none absolute top-1/2 left-full z-50 ml-2 -translate-y-1/2 whitespace-nowrap rounded-md border border-slate-700 bg-[var(--c-surface)] px-2 py-1 text-xs text-slate-100 opacity-0 shadow-lg transition-opacity delay-300 group-hover/item:opacity-100 group-focus-visible/item:opacity-100 group-focus-visible/item:delay-0">
      {label}
    </span>
  );
}

const itemClass = (collapsed: boolean) =>
  `group/item relative flex items-center gap-3 rounded-md px-2.5 py-2 text-sm transition-colors ${
    collapsed ? "justify-center" : ""
  }`;

function NavItem({ item, collapsed }: { item: NavEntry; collapsed: boolean }) {
  const Icon = item.icon;
  return (
    <Link
      to={item.to}
      activeOptions={item.exact ? { exact: true } : undefined}
      aria-label={item.label}
      className={`${itemClass(collapsed)} text-slate-400 hover:bg-slate-800/60 hover:text-slate-100 [&.active]:bg-slate-800 [&.active]:text-slate-100`}
    >
      <Icon size={18} strokeWidth={2} className="shrink-0" />
      {!collapsed && <span className="truncate">{item.label}</span>}
      {collapsed && <CollapsedTip label={item.label} />}
    </Link>
  );
}

// A non-routed button mirroring NavItem styling (Settings, etc.).
function SideButton({
  icon: Icon,
  label,
  collapsed,
  onClick,
}: {
  icon: LucideIcon;
  label: string;
  collapsed: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={collapsed ? undefined : label}
      onClick={onClick}
      className={`${itemClass(collapsed)} w-full text-slate-400 hover:bg-slate-800/60 hover:text-slate-100`}
    >
      <Icon size={18} strokeWidth={2} className="shrink-0" />
      {!collapsed && <span className="truncate">{label}</span>}
      {collapsed && <CollapsedTip label={label} />}
    </button>
  );
}

// The user/profile entry — an avatar chip that opens the settings modal. Expanded shows the
// (placeholder) identity; collapsed is the avatar alone with a hover tooltip.
function ProfileButton({
  collapsed,
  onClick,
}: { collapsed: boolean; onClick?: () => void }): ReactNode {
  return (
    <button
      type="button"
      aria-label="Open settings"
      onClick={onClick}
      className={`${itemClass(collapsed)} w-full text-slate-300 hover:bg-slate-800/60`}
    >
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-blue-600/80 text-white">
        <User size={14} strokeWidth={2} />
      </span>
      {!collapsed && (
        <span className="flex min-w-0 flex-col text-left leading-tight">
          <span className="truncate text-sm text-slate-200">Local user</span>
          <span className="truncate text-[11px] text-slate-500">single-user</span>
        </span>
      )}
      {collapsed && <CollapsedTip label="Profile" />}
    </button>
  );
}

// The user/settings modal (opened from the profile entry). For now it's mostly a placeholder for
// future user configuration; the one wired control is the theme switch (icon-only) in the corner.
const THEME_OPTIONS: { pref: ThemePref; icon: LucideIcon; label: string }[] = [
  { pref: "light", icon: Sun, label: "Light" },
  { pref: "dark", icon: Moon, label: "Dark" },
  { pref: "system", icon: Monitor, label: "System" },
];

function SettingsModal({ onClose }: { onClose: () => void }) {
  const { pref, setTheme } = useTheme();
  return (
    <Modal title="Settings" width="max-w-lg" onClose={onClose}>
      <div className="flex min-h-[200px] flex-col">
        {/* Identity + placeholder for the user configuration to come. */}
        <div className="flex items-center gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-blue-600/80 text-white">
            <User size={18} strokeWidth={2} />
          </span>
          <div className="min-w-0">
            <div className="text-sm font-medium text-slate-100">Local user</div>
            <div className="text-xs text-slate-500">single-user · localhost</div>
          </div>
        </div>
        <div className="mt-4 border-t border-slate-800 pt-4">
          <LocalCredentials />
        </div>

        {/* Theme switch — icon-only buttons pinned to the bottom-right corner. */}
        <div className="mt-auto flex items-center justify-between border-t border-slate-800 pt-3">
          <span className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
            Theme
          </span>
          <div className="flex items-center gap-1">
            {THEME_OPTIONS.map(({ pref: p, icon: Icon, label }) => {
              const active = pref === p;
              return (
                <button
                  key={p}
                  type="button"
                  onClick={() => setTheme(p)}
                  aria-label={`${label} theme`}
                  aria-pressed={active}
                  title={`${label} theme`}
                  className={`flex h-8 w-8 items-center justify-center rounded-md border transition-colors ${
                    active
                      ? "border-blue-500 bg-blue-500/15 text-blue-700 dark:text-blue-300"
                      : "border-slate-700 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
                  }`}
                >
                  <Icon size={16} strokeWidth={2} />
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </Modal>
  );
}
