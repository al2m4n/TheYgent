import { Link, Outlet } from "@tanstack/react-router";
import { NotificationCenter } from "../lib/notify";

// The shell: a thin top bar + the routed view. The interface is canvas-first, so chrome stays out
// of the way. Two pages: the canvas (Agents/Editor) and Registries (browse + install models).
const navLink =
  "rounded px-2 py-1 text-sm text-slate-400 hover:text-slate-100 [&.active]:text-slate-100 [&.active]:bg-slate-800";

export function Root() {
  return (
    <div className="flex h-full flex-col">
      <header className="flex h-11 shrink-0 items-center gap-4 border-b border-slate-800 bg-[#0e131c] px-4">
        <Link to="/" className="flex items-center gap-2 text-sm font-semibold text-slate-100">
          <span className="text-blue-400">◆</span> theygent
        </Link>
        <nav className="flex items-center gap-1">
          <Link to="/" className={navLink} activeOptions={{ exact: true }}>
            Agents
          </Link>
          <Link to="/runs" className={navLink}>
            Runs
          </Link>
          <Link to="/threads" className={navLink}>
            Threads
          </Link>
          <Link to="/compose" className={navLink}>
            Compose
          </Link>
          <Link to="/registries" className={navLink}>
            Registries
          </Link>
          <Link to="/mcp" className={navLink}>
            MCP
          </Link>
        </nav>
      </header>
      <main className="min-h-0 flex-1">
        <Outlet />
      </main>
      {/* The one central place for messages + live download progress, bottom-right, above every
          page and persistent across navigation. */}
      <NotificationCenter />
    </div>
  );
}
