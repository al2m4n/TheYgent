import { Link, Outlet } from "@tanstack/react-router";

// The shell: a thin top bar + the routed view. The interface is canvas-first, so chrome stays out
// of the way.
export function Root() {
  return (
    <div className="flex h-full flex-col">
      <header className="flex h-11 shrink-0 items-center gap-4 border-b border-slate-800 bg-[#0e131c] px-4">
        <Link to="/" className="flex items-center gap-2 text-sm font-semibold text-slate-100">
          <span className="text-blue-400">◆</span> theygent interface
        </Link>
        <span className="text-[11px] text-slate-600">visual agent canvas · IR ⇄ React Flow</span>
      </header>
      <main className="min-h-0 flex-1">
        <Outlet />
      </main>
    </div>
  );
}
