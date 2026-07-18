import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import {
  Activity,
  ArrowRight,
  Bot,
  Boxes,
  ChevronRight,
  Database,
  LayoutDashboard,
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
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Modal } from "../components/ui";
import { Avatar, AvatarFallback } from "../components/ui/avatar";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarProvider,
  SidebarSeparator,
  SidebarTrigger,
  useSidebar,
} from "../components/ui/sidebar";
import { ToggleGroup, ToggleGroupItem } from "../components/ui/toggle-group";
import { TooltipProvider } from "../components/ui/tooltip";
import { statusTone, toneOf } from "../lib/categories";
import { shortId } from "../lib/format";
import { NotificationCenter } from "../lib/notify";
import type { Run, SessionSummary } from "../lib/runtypes";
import { type ThemePref, useTheme } from "../lib/theme";
import { flattenPages, useRunsInfinite, useSessionsInfinite } from "../queries";

// The shell: a collapsible LEFT sidebar + the routed view. The interface is canvas-first, so chrome
// stays quiet and out of the way; collapsing the rail to its icon mode hands the canvas the full
// width. Each entry is one Lucide icon + a label; collapsed, only the icon shows and the label moves
// to a hover tooltip.

interface NavEntry {
  to: string;
  label: string;
  icon: LucideIcon;
  exact?: boolean;
}

// The rail, top to bottom: Dashboard (the home overview), then the build/observe surfaces
// (Agents + a collapsible Runs that previews recent runs), the conversational surfaces (New Chat +
// a collapsible Chats that previews recent sessions), and a collapsible Configuration group
// (Registries, MCP, RAG, app Settings). Each group is separated in the rail.
const NAV_DASHBOARD: NavEntry = { to: "/", label: "Dashboard", icon: LayoutDashboard, exact: true };
const NAV_AGENTS: NavEntry = { to: "/agents", label: "Agents", icon: Bot };
const NAV_RUNS: NavEntry = { to: "/runs", label: "Runs", icon: Activity };
const NAV_NEW_CHAT: NavEntry = { to: "/chat", label: "New Chat", icon: SquarePen };
const NAV_CHATS: NavEntry = { to: "/sessions", label: "Chats", icon: MessagesSquare };

const NAV_CONFIG_HEAD: NavEntry[] = [
  { to: "/registries", label: "Registries", icon: Boxes },
  { to: "/mcp", label: "MCP", icon: Plug },
  { to: "/rag", label: "RAG", icon: Database },
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

// The disclosure state of a collapsible nav group (Runs, Chats). Defaults OPEN so the recent
// previews are visible out of the box; the toggle persists like the rail width does. Guarded for
// non-DOM (test) environments.
const RUNS_OPEN_KEY = "theygent.ui.runsOpen";
const CHATS_OPEN_KEY = "theygent.ui.chatsOpen";

function readOpenPref(key: string): boolean {
  try {
    return typeof localStorage === "undefined" || localStorage.getItem(key) !== "0";
  } catch {
    return true;
  }
}

function writeOpenPref(key: string, open: boolean): void {
  try {
    localStorage.setItem(key, open ? "1" : "0");
  } catch {
    // no localStorage (tests) — the in-memory state still drives the UI this session.
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
    <TooltipProvider delayDuration={0}>
      {/* The sidebar open state is controlled from here (not the component's cookie) so the existing
          collapse preference keeps persisting to localStorage and the editor auto-collapse works. */}
      <SidebarProvider
        className="h-full min-h-0"
        open={!effectiveCollapsed}
        onOpenChange={(open) => setRailCollapsed(!open)}
      >
        <AppSidebar onOpenSettings={() => setSettingsOpen(true)} />

        {settingsOpen && <UserSettingsModal onClose={() => setSettingsOpen(false)} />}

        {/* The single scroll region: the document never scrolls (body is overflow-hidden), so the
            sidebar stays fixed while a long page scrolls here. Routes that own their height (the
            canvas Editor) sit exactly h-full and never overflow. */}
        <SidebarInset className="min-h-0 min-w-0 overflow-y-auto">
          {/* On narrow viewports the rail lives in an off-canvas sheet, so surface a trigger. */}
          <div className="flex shrink-0 items-center border-b border-border p-1.5 md:hidden">
            <SidebarTrigger />
          </div>
          <Outlet />
        </SidebarInset>

        {/* The one central place for messages + live download progress, bottom-right, above every
            page and persistent across navigation. */}
        <NotificationCenter />
      </SidebarProvider>
    </TooltipProvider>
  );
}

// The rail itself: brand head, the three nav groups + recents, and the profile footer. In icon
// mode every entry collapses to its icon and the label moves into the menu button's tooltip.
function AppSidebar({ onOpenSettings }: { onOpenSettings: () => void }) {
  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="border-b border-sidebar-border p-0">
        <Brand />
      </SidebarHeader>

      <SidebarContent>
        {/* Dashboard — the home overview, on its own at the top. */}
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              <NavItem item={NAV_DASHBOARD} />
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarSeparator />
        {/* Build / observe: Agents + a collapsible Runs that previews the latest runs. */}
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              <NavItem item={NAV_AGENTS} />
              <RunsNav />
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarSeparator />
        {/* Converse: New Chat + a collapsible Chats that previews recent conversations. */}
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              <NavItem item={NAV_NEW_CHAT} />
              <ChatsNav />
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarSeparator />
        <ConfigGroup />
      </SidebarContent>

      {/* Bottom: the user/profile entry — USER settings (identity, theme), not app settings. */}
      <SidebarFooter className="border-t border-sidebar-border">
        <SidebarMenu>
          <SidebarMenuItem>
            <ProfileButton onClick={onOpenSettings} />
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  );
}

// The rail head: brand mark + the collapse/expand control. Expanded shows the logo mark beside the
// "TheYgent" wordmark (real text, so it re-colors with the theme) and a collapse button; collapsed
// shows the mark alone — resting on it swaps the mark for the expand control, so the logo doubles as
// the affordance to reopen the rail. The mark artwork is theme-aware: a light-on-dark set for the dark
// theme, a dark-on-light set for the light theme (served from the static logo folder).
function Brand() {
  const { state, toggleSidebar } = useSidebar();
  const { resolved } = useTheme();
  const dark = resolved === "dark";
  const mark = dark ? "/logo/theygent-logo-dark.svg" : "/logo/theygent-logo.svg";

  if (state === "collapsed") {
    // The whole head is the expand control: the mark shows at rest and fades out on hover/focus while
    // the expand glyph fades in — one target, so there's no click ambiguity between logo and button.
    return (
      <button
        type="button"
        onClick={toggleSidebar}
        aria-label="Expand sidebar"
        aria-expanded={false}
        title="Expand sidebar"
        className="group/brand relative flex h-11 w-full shrink-0 items-center justify-center transition-colors hover:bg-sidebar-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-sidebar-ring"
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
          className="absolute text-muted-foreground opacity-0 transition-opacity group-hover/brand:opacity-100 group-focus-visible/brand:opacity-100"
        />
      </button>
    );
  }

  return (
    <div className="flex h-11 shrink-0 items-center gap-2 px-2.5">
      <Link to="/" aria-label="TheYgent — home" className="flex min-w-0 items-center gap-2">
        {/* The mark is decorative (empty alt) — the link's aria-label + the visible wordmark carry
            the accessible name. The wordmark is real text, so it re-colors with the theme. */}
        <img src={mark} alt="" className="h-6 w-auto shrink-0" />
        <span className="truncate text-[15px] font-semibold tracking-tight text-sidebar-foreground">
          TheYgent
        </span>
      </Link>
      <button
        type="button"
        onClick={toggleSidebar}
        aria-label="Collapse sidebar"
        aria-expanded={true}
        title="Collapse sidebar"
        className="ml-auto rounded-md p-1 text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring"
      >
        <PanelLeftClose size={16} />
      </button>
    </div>
  );
}

// One nav row: a menu button wrapping the router Link. Active state is computed here (exact for
// the home entry, prefix for the rest — mirroring the router's fuzzy matching) because the menu
// button styles via data-active, not the router's `.active` class. The label doubles as the
// tooltip, which the menu button only shows while the rail is collapsed to icons.
function NavItem({ item }: { item: NavEntry }) {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const isActive = item.exact
    ? pathname === item.to
    : pathname === item.to || pathname.startsWith(`${item.to}/`);
  const Icon = item.icon;
  return (
    <SidebarMenuItem>
      <SidebarMenuButton asChild isActive={isActive} tooltip={item.label}>
        <Link
          to={item.to}
          activeOptions={item.exact ? { exact: true } : undefined}
          aria-label={item.label}
        >
          <Icon />
          <span>{item.label}</span>
        </Link>
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
}

// A collapsible nav entry: the label navigates to the page; a chevron toggles a preview of recent
// items right in the rail; and a "Show all" row at the bottom is a second, explicit way through to
// the page. In icon mode the preview and chevron hide (built into the sub/action components) and the
// icon-sized label stays the entry point. The disclosure state persists per group.
function CollapsibleNav({
  item,
  storageKey,
  showAllLabel,
  emptyLabel,
  hasItems,
  loaded,
  children,
}: {
  item: NavEntry;
  storageKey: string;
  showAllLabel: string;
  emptyLabel: string;
  hasItems: boolean;
  /** The list query has resolved. Only then is an empty preview a genuine "nothing yet" — while the
   *  query is still loading (or the control plane is unreachable) we show nothing, never a false
   *  "No runs yet". */
  loaded: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(() => readOpenPref(storageKey));
  const { state } = useSidebar();
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const isActive = pathname === item.to || pathname.startsWith(`${item.to}/`);
  const Icon = item.icon;

  useEffect(() => writeOpenPref(storageKey, open), [storageKey, open]);

  return (
    <SidebarMenuItem>
      <SidebarMenuButton asChild isActive={isActive} tooltip={item.label}>
        <Link to={item.to} aria-label={item.label}>
          <Icon />
          <span>{item.label}</span>
        </Link>
      </SidebarMenuButton>
      {/* The chevron toggles the preview; it sits apart from the label so a click on the label still
          navigates. Hidden in icon mode (the sub-list has nowhere to render there). */}
      <SidebarMenuAction
        aria-label={open ? `Collapse ${item.label}` : `Expand ${item.label}`}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="text-muted-foreground"
      >
        <ChevronRight className={`transition-transform ${open ? "rotate-90" : ""}`} />
      </SidebarMenuAction>
      {open && state !== "collapsed" && (
        <SidebarMenuSub>
          {children}
          {hasItems ? (
            <SidebarMenuSubItem>
              <SidebarMenuSubButton asChild className="text-muted-foreground">
                <Link to={item.to}>
                  <span>{showAllLabel}</span>
                  <ArrowRight className="ml-auto opacity-70" />
                </Link>
              </SidebarMenuSubButton>
            </SidebarMenuSubItem>
          ) : loaded ? (
            <SidebarMenuSubItem>
              <span className="flex h-7 items-center px-2 text-xs text-muted-foreground/70">
                {emptyLabel}
              </span>
            </SidebarMenuSubItem>
          ) : null}
        </SidebarMenuSub>
      )}
    </SidebarMenuItem>
  );
}

// How many recent items each collapsible previews — a short peek; "Show all" opens the full page.
const RAIL_PREVIEW = 5;

// Runs, collapsible: the label opens the Runs page; expanded, it previews the latest runs. Shares
// the Runs page's infinite query (same cache) which already auto-refreshes, so the preview stays
// live without its own poll.
function RunsNav() {
  const { data, isSuccess } = useRunsInfinite();
  const runs = useMemo(() => flattenPages(data).slice(0, RAIL_PREVIEW), [data]);
  return (
    <CollapsibleNav
      item={NAV_RUNS}
      storageKey={RUNS_OPEN_KEY}
      showAllLabel="Show all runs"
      emptyLabel="No runs yet"
      hasItems={runs.length > 0}
      loaded={isSuccess}
    >
      {runs.map((r) => (
        <RunSubItem key={r.id} run={r} />
      ))}
    </CollapsibleNav>
  );
}

// A compact run row: a status dot + the run's model (or graph / id). The dot keeps a status legible
// even truncated to the rail width.
function runNavLabel(run: Run): string {
  return run.model || (run.graph_id ? shortId(run.graph_id, 12) : shortId(run.id, 12));
}

function RunSubItem({ run }: { run: Run }) {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  return (
    <SidebarMenuSubItem>
      <SidebarMenuSubButton
        asChild
        isActive={pathname === `/runs/${run.id}`}
        className="text-muted-foreground"
      >
        <Link to="/runs/$runId" params={{ runId: run.id }} title={run.id}>
          <span
            className={`inline-block size-2 shrink-0 rounded-full ${toneOf(statusTone(run.status)).dot}`}
            aria-hidden
          />
          <span>{runNavLabel(run)}</span>
        </Link>
      </SidebarMenuSubButton>
    </SidebarMenuSubItem>
  );
}

// Chats, collapsible: the label opens the Chats page; expanded, it previews recent conversations —
// the "continue where I left off" list. Every chat surface records into a session, so this covers
// the chat page, model benches, and agent chats.
function ChatsNav() {
  const { data, isSuccess } = useSessionsInfinite();
  const sessions = useMemo(() => flattenPages(data).slice(0, RAIL_PREVIEW), [data]);
  return (
    <CollapsibleNav
      item={NAV_CHATS}
      storageKey={CHATS_OPEN_KEY}
      showAllLabel="Show all chats"
      emptyLabel="No chats yet"
      hasItems={sessions.length > 0}
      loaded={isSuccess}
    >
      {sessions.map((s) => (
        <ChatSubItem key={s.id} session={s} />
      ))}
    </CollapsibleNav>
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

function ChatSubItem({ session }: { session: SessionSummary }) {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const Icon = session.metadata?.kind === "bench.agent" ? Bot : MessageSquare;
  return (
    <SidebarMenuSubItem>
      <SidebarMenuSubButton
        asChild
        isActive={pathname === `/sessions/${session.id}`}
        className="text-muted-foreground"
      >
        <Link
          to="/sessions/$sessionId"
          params={{ sessionId: session.id }}
          title={session.preview ?? session.id}
        >
          <Icon />
          <span>{recentLabel(session)}</span>
        </Link>
      </SidebarMenuSubButton>
    </SidebarMenuSubItem>
  );
}

// The collapsible Configuration group. Expanded rail: a disclosure header over the entries;
// icon rail: the entries render as plain icons regardless of the disclosure (a hidden group
// would strand them — the header itself hides via the group-label icon-mode rule).
// The open preference persists like the rail width does.
const CONFIG_OPEN_KEY = "theygent.ui.configOpen";

function readConfigOpen(): boolean {
  try {
    return typeof localStorage === "undefined" || localStorage.getItem(CONFIG_OPEN_KEY) !== "0";
  } catch {
    return true;
  }
}

function ConfigGroup() {
  const [open, setOpen] = useState(readConfigOpen);
  const { state } = useSidebar();
  useEffect(() => {
    try {
      localStorage.setItem(CONFIG_OPEN_KEY, open ? "1" : "0");
    } catch {
      // no localStorage (tests) — in-memory state still drives the UI this session.
    }
  }, [open]);

  return (
    <SidebarGroup>
      <SidebarGroupLabel asChild className="group-data-[collapsible=icon]:hidden">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
          className="w-full gap-2 transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
        >
          <Settings2 />
          <span className="truncate font-semibold uppercase tracking-wide">Configuration</span>
          <ChevronRight
            className={`ml-auto transition-transform ${open ? "rotate-90" : ""}`}
            aria-hidden
          />
        </button>
      </SidebarGroupLabel>
      {(open || state === "collapsed") && (
        <SidebarGroupContent>
          <SidebarMenu>
            {NAV_CONFIG_HEAD.map((item) => (
              <NavItem key={item.to} item={item} />
            ))}
            <NavItem item={NAV_SETTINGS} />
          </SidebarMenu>
        </SidebarGroupContent>
      )}
    </SidebarGroup>
  );
}

// The user/profile entry — an avatar chip that opens the settings modal. Expanded shows the
// (placeholder) identity; collapsed is the avatar alone with a hover tooltip.
function ProfileButton({ onClick }: { onClick?: () => void }) {
  return (
    <SidebarMenuButton size="lg" aria-label="Open settings" tooltip="Profile" onClick={onClick}>
      <Avatar size="sm">
        <AvatarFallback className="bg-primary text-primary-foreground">
          <User />
        </AvatarFallback>
      </Avatar>
      <span className="flex min-w-0 flex-col text-left leading-tight">
        <span className="truncate text-sm text-sidebar-foreground">Local user</span>
        <span className="truncate text-[11px] text-muted-foreground">single-user</span>
      </span>
    </SidebarMenuButton>
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
          <Avatar size="lg">
            <AvatarFallback className="bg-primary text-primary-foreground">
              <User size={18} strokeWidth={2} />
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0">
            <div className="text-sm font-medium text-foreground">Local user</div>
            <div className="text-xs text-muted-foreground">single-user · localhost</div>
          </div>
        </div>

        {/* Theme switch — icon-only buttons pinned to the bottom-right corner. */}
        <div className="mt-auto flex items-center justify-between border-t border-border pt-3">
          <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            Theme
          </span>
          <ToggleGroup
            type="single"
            variant="outline"
            value={pref}
            onValueChange={(next) => {
              // Radix reports "" when the active item is re-clicked — a theme is never unset.
              if (next) setTheme(next as ThemePref);
            }}
            aria-label="Theme"
          >
            {THEME_OPTIONS.map(({ pref: p, icon: Icon, label }) => (
              <ToggleGroupItem
                key={p}
                value={p}
                aria-label={`${label} theme`}
                title={`${label} theme`}
                className="data-[state=on]:bg-primary/10 data-[state=on]:text-primary"
              >
                <Icon size={16} strokeWidth={2} />
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </div>
      </div>
    </Modal>
  );
}
