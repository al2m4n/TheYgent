import { Link, Outlet, useRouterState } from "@tanstack/react-router";

const NAV: { to: string; label: string; exact?: boolean }[] = [
  { to: "/", label: "Runs", exact: true },
  { to: "/threads", label: "Threads" },
  { to: "/compose", label: "Compose" },
  { to: "/registries", label: "Registries" },
];

export function Root() {
  const path = useRouterState({ select: (s) => s.location.pathname });
  return (
    <div className="mx-auto flex min-h-full max-w-6xl flex-col">
      <header className="flex items-center gap-6 border-b border-slate-800 px-4 py-3">
        <Link to="/" className="text-sm font-semibold tracking-tight text-slate-100">
          theygent <span className="text-slate-500">cockpit</span>
        </Link>
        <nav className="flex gap-1">
          {NAV.map((item) => {
            const active = item.exact ? path === item.to : path.startsWith(item.to);
            return (
              <Link
                key={item.to}
                to={item.to}
                className={`rounded-md px-3 py-1.5 text-sm transition-colors ${
                  active
                    ? "bg-slate-800 text-slate-100"
                    : "text-slate-400 hover:bg-slate-800/50 hover:text-slate-200"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
        <span className="ml-auto text-xs text-slate-600">localhost · single-user</span>
      </header>
      <main className="flex-1 px-4 py-5">
        <Outlet />
      </main>
    </div>
  );
}
