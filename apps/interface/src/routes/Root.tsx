import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import {
  Activity,
  Bot,
  Boxes,
  ChevronDown,
  ChevronRight,
  Database,
  type LucideIcon,
  MessageSquare,
  MessagesSquare,
  Monitor,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Plug,
  Settings,
  Settings2,
  SquarePen,
  Sun,
  User,
} from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { Modal } from "../components/ui";
import { api } from "../lib/api";
import { shortId } from "../lib/format";
import { NotificationCenter } from "../lib/notify";
import type { SessionSummary } from "../lib/runtypes";
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

// Three groups, separated in the rail: the build/observe surfaces (Agents/Runs), the
// conversational surfaces (New Chat / Chats, with the recent sessions right below), and a
// collapsible Configuration group (Registries, MCP, the reserved RAG slot, app Settings).
const NAV_MAIN: NavEntry[] = [
  { to: "/", label: "Agents", icon: Bot, exact: true },
  { to: "/runs", label: "Runs", icon: Activity },
];

const NAV_CHAT: NavEntry[] = [
  { to: "/chat", label: "New Chat", icon: SquarePen },
  { to: "/sessions", label: "Chats", icon: MessagesSquare },
];

// Configuration entries around the RAG placeholder (which is reserved, not routed).
const NAV_CONFIG_HEAD: NavEntry[] = [
  { to: "/registries", label: "Registries", icon: Boxes },
  { to: "/mcp", label: "MCP", icon: Plug },
];
// App-level settings (endpoints, credentials) — distinct from the profile's USER settings
// (identity, theme) at the bottom of the rail.
const NAV_SETTINGS: NavEntry = { to: "/settings", label: "Settings", icon: Settings };

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
        {/* Rail head: brand + the collapse/expand control. */}
        <Brand
          collapsed={effectiveCollapsed}
          onExpand={() => setRailCollapsed(false)}
          onCollapse={() => setRailCollapsed(true)}
        />

        {/* Primary navigation: build/observe · chat · configuration · recents. */}
        <nav className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2 py-2">
          {NAV_MAIN.map((item) => (
            <NavItem key={item.to} item={item} collapsed={effectiveCollapsed} />
          ))}
          <NavSeparator />
          {NAV_CHAT.map((item) => (
            <NavItem key={item.to} item={item} collapsed={effectiveCollapsed} />
          ))}
          <NavSeparator />
          <ConfigGroup collapsed={effectiveCollapsed} />
          <RecentSessions collapsed={effectiveCollapsed} />
        </nav>

        {/* Bottom: the user/profile entry — USER settings (identity, theme), not app settings. */}
        <div className="shrink-0 space-y-1 border-t border-slate-800 px-2 py-2">
          <ProfileButton collapsed={effectiveCollapsed} onClick={() => setSettingsOpen(true)} />
        </div>
      </aside>

      {settingsOpen && <UserSettingsModal onClose={() => setSettingsOpen(false)} />}

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

// The rail head: brand mark + the collapse/expand control. Expanded shows the logo mark beside the
// "TheYgent" wordmark (real text, so it re-colors with the theme) and a collapse button; collapsed
// shows the mark alone — resting on it swaps the mark for the expand control, so the logo doubles as
// the affordance to reopen the rail. The mark artwork is theme-aware: a light-on-dark set for the dark
// theme, a dark-on-light set for the light theme (served from the static logo folder).
function Brand({
  collapsed,
  onExpand,
  onCollapse,
}: {
  collapsed: boolean;
  onExpand: () => void;
  onCollapse: () => void;
}) {
  const { resolved } = useTheme();
  const dark = resolved === "dark";
  const mark = dark ? "/logo/TheYgent-logo-dark.svg" : "/logo/TheYgent-logo.svg";

  if (collapsed) {
    // The whole head is the expand control: the mark shows at rest and fades out on hover/focus while
    // the expand glyph fades in — one target, so there's no click ambiguity between logo and button.
    return (
      <button
        type="button"
        onClick={onExpand}
        aria-label="Expand sidebar"
        aria-expanded={false}
        title="Expand sidebar"
        className="group/brand relative flex h-11 w-full shrink-0 items-center justify-center border-b border-slate-800 transition-colors hover:bg-slate-800/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500"
      >
        {/* Decorative: the button's aria-label already names the control, so the mark is hidden from
            the a11y tree (empty alt) to avoid a duplicate announcement. */}
        <img
          src={mark}
          alt=""
          className="h-7 w-auto transition-opacity group-hover/brand:opacity-0 group-focus-visible/brand:opacity-0"
        />
        <PanelLeftOpen
          size={17}
          className="absolute text-slate-400 opacity-0 transition-opacity group-hover/brand:opacity-100 group-focus-visible/brand:opacity-100"
        />
      </button>
    );
  }

  return (
    <div className="flex h-11 shrink-0 items-center gap-2 border-b border-slate-800 px-2.5">
      <Link to="/" aria-label="TheYgent — home" className="flex min-w-0 items-center gap-2">
        {/* The mark is decorative (empty alt) — the link's aria-label + the visible wordmark carry
            the accessible name. The wordmark is real text, so it re-colors with the theme. */}
        <img src={mark} alt="" className="h-6 w-auto shrink-0" />
        <span className="truncate text-[15px] font-semibold tracking-tight text-slate-100">
          TheYgent
        </span>
      </Link>
      <button
        type="button"
        onClick={onCollapse}
        aria-label="Collapse sidebar"
        aria-expanded={true}
        title="Collapse sidebar"
        className="ml-auto rounded p-1 text-slate-500 transition-colors hover:bg-slate-800 hover:text-slate-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
      >
        <PanelLeftClose size={16} />
      </button>
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

// The most recent sessions, right under the primary nav — one click back into any conversation.
// Every chat surface (the chat page, a model bench, an agent chat) records into a session, so
// this is the "continue where I left off" list. Hidden while the rail is collapsed (the Sessions
// nav item stays as the icon-sized entry point).
function RecentSessions({ collapsed }: { collapsed: boolean }) {
  const recent = useQuery({
    queryKey: ["sessions", "recent"],
    queryFn: () => api.listSessions({ limit: 8 }),
    refetchInterval: 30_000,
    // The rail must never surface a scary error — an unreachable control plane just means no list.
    retry: false,
  });
  if (collapsed || !recent.data || recent.data.length === 0) return null;
  return (
    <div className="mt-3 border-t border-slate-800 pt-2">
      <p className="px-2.5 pb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
        Recents
      </p>
      {recent.data.map((s) => (
        <RecentItem key={s.id} session={s} />
      ))}
    </div>
  );
}

// Prefer a human label: an explicit title, the first user message, the target — the id only as
// the last resort.
function recentLabel(s: SessionSummary): string {
  const meta = s.metadata ?? {};
  const title = typeof meta.title === "string" ? meta.title : "";
  const target =
    typeof meta.agent_name === "string"
      ? meta.agent_name
      : typeof meta.model === "string"
        ? meta.model
        : "";
  return title || s.preview?.trim() || target || shortId(s.id);
}

function RecentItem({ session }: { session: SessionSummary }) {
  const Icon = session.metadata?.kind === "bench.agent" ? Bot : MessageSquare;
  return (
    <Link
      to="/sessions/$sessionId"
      params={{ sessionId: session.id }}
      title={session.preview ?? session.id}
      className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-xs text-slate-400 transition-colors hover:bg-slate-800/60 hover:text-slate-100 [&.active]:bg-slate-800 [&.active]:text-slate-100"
    >
      <Icon size={13} className="shrink-0" />
      <span className="truncate">{recentLabel(session)}</span>
    </Link>
  );
}

// A quiet horizontal rule between nav groups.
function NavSeparator() {
  return <div aria-hidden className="mx-1 my-2 border-t border-slate-800" />;
}

// The reserved retrieval slot: visible so the IA already has its place, inert until it ships.
function RagPlaceholder({ collapsed }: { collapsed: boolean }) {
  return (
    <div
      aria-disabled
      title={collapsed ? undefined : "Coming soon"}
      className={`${itemClass(collapsed)} cursor-default text-slate-600`}
    >
      <Database size={18} strokeWidth={2} className="shrink-0" />
      {!collapsed && (
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate">RAG</span>
          <span className="rounded bg-slate-800/70 px-1.5 py-0.5 text-[10px] text-slate-500">
            soon
          </span>
        </span>
      )}
      {collapsed && <CollapsedTip label="RAG — soon" />}
    </div>
  );
}

// The collapsible Configuration group. Expanded rail: a disclosure header over the entries;
// collapsed rail: the entries render as plain icons (a hidden dropdown would strand them).
// The open preference persists like the rail width does.
const CONFIG_OPEN_KEY = "theygent.ui.configOpen";

function readConfigOpen(): boolean {
  try {
    return typeof localStorage === "undefined" || localStorage.getItem(CONFIG_OPEN_KEY) !== "0";
  } catch {
    return true;
  }
}

function ConfigGroup({ collapsed }: { collapsed: boolean }) {
  const [open, setOpen] = useState(readConfigOpen);
  useEffect(() => {
    try {
      localStorage.setItem(CONFIG_OPEN_KEY, open ? "1" : "0");
    } catch {
      // no localStorage (tests) — in-memory state still drives the UI this session.
    }
  }, [open]);

  const items = (
    <>
      {NAV_CONFIG_HEAD.map((item) => (
        <NavItem key={item.to} item={item} collapsed={collapsed} />
      ))}
      <RagPlaceholder collapsed={collapsed} />
      <NavItem item={NAV_SETTINGS} collapsed={collapsed} />
    </>
  );

  if (collapsed) return items;

  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className={`${itemClass(false)} w-full text-slate-500 hover:bg-slate-800/60 hover:text-slate-300`}
      >
        <Settings2 size={18} strokeWidth={2} className="shrink-0" />
        <span className="truncate text-xs font-semibold uppercase tracking-wide">
          Configuration
        </span>
        <Chevron size={14} className="ml-auto shrink-0" />
      </button>
      {open && <div className="mt-1 space-y-1">{items}</div>}
    </div>
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

// The USER settings modal (opened from the profile entry): identity + theme. App-level
// configuration (endpoints, credentials) lives on the /settings page under Configuration.
const THEME_OPTIONS: { pref: ThemePref; icon: LucideIcon; label: string }[] = [
  { pref: "light", icon: Sun, label: "Light" },
  { pref: "dark", icon: Moon, label: "Dark" },
  { pref: "system", icon: Monitor, label: "System" },
];

function UserSettingsModal({ onClose }: { onClose: () => void }) {
  const { pref, setTheme } = useTheme();
  return (
    <Modal title="User settings" width="max-w-lg" onClose={onClose}>
      <div className="flex min-h-[160px] flex-col">
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
