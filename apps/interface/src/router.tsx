import { Link, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { Empty, Page, linkClass } from "./components/ui";
import { Compose } from "./routes/Compose";
import { Editor } from "./routes/Editor";
import { Home } from "./routes/Home";
import { Mcp } from "./routes/Mcp";
import { Registries } from "./routes/Registries";
import { Root } from "./routes/Root";
import { RunDetail } from "./routes/RunDetail";
import { RunsList } from "./routes/RunsList";
import { ThreadDetail } from "./routes/ThreadDetail";
import { ThreadsList } from "./routes/ThreadsList";

// An unknown URL lands on a styled page inside the shell (not the router's bare default text),
// with a link back out.
function NotFound() {
  return (
    <Page>
      <Empty>
        Page not found —{" "}
        <Link to="/" className={linkClass}>
          back to Agents
        </Link>
      </Empty>
    </Page>
  );
}

const rootRoute = createRootRoute({ component: Root, notFoundComponent: NotFound });

const homeRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: Home });

// ── operator surface (ported from the cockpit): runs, threads, compose ──
const runsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs",
  component: RunsList,
});

const runDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs/$runId",
  component: RunDetail,
});

const threadsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/threads",
  component: ThreadsList,
});

const threadDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/threads/$threadId",
  component: ThreadDetail,
});

const composeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/compose",
  component: Compose,
  // The "new run in this thread" link pre-fills the composer via ?threadId=.
  validateSearch: (search: Record<string, unknown>): { threadId?: string } => ({
    threadId: typeof search.threadId === "string" ? search.threadId : undefined,
  }),
});

const registriesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/registries",
  component: Registries,
});

const editorRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/editor",
  component: Editor,
  // Open an existing agent version via ?agent=<id>&version=<v>; absent ⇒ a new blank graph.
  validateSearch: (search: Record<string, unknown>): { agent?: string; version?: string } => ({
    agent: typeof search.agent === "string" ? search.agent : undefined,
    version: typeof search.version === "string" ? search.version : undefined,
  }),
});

const mcpRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/mcp",
  component: Mcp,
});

const routeTree = rootRoute.addChildren([
  homeRoute,
  runsRoute,
  runDetailRoute,
  threadsRoute,
  threadDetailRoute,
  composeRoute,
  editorRoute,
  registriesRoute,
  mcpRoute,
]);

export const router = createRouter({ routeTree, defaultPreload: "intent" });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
