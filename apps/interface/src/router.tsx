import { Link, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { Empty, Page, linkClass } from "./components/ui";
import { Chat } from "./routes/Chat";
import { Editor } from "./routes/Editor";
import { Home } from "./routes/Home";
import { Mcp } from "./routes/Mcp";
import { Rag } from "./routes/Rag";
import { Registries } from "./routes/Registries";
import { Root } from "./routes/Root";
import { RunDetail } from "./routes/RunDetail";
import { RunsList } from "./routes/RunsList";
import { SessionDetail } from "./routes/SessionDetail";
import { SessionsList } from "./routes/SessionsList";
import { Settings } from "./routes/Settings";

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

// ── operator surface: chat, runs, sessions ──
const chatRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/chat",
  component: Chat,
});

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

const sessionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions",
  component: SessionsList,
});

const sessionDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/sessions/$sessionId",
  component: SessionDetail,
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

const ragRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/rag",
  component: Rag,
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: Settings,
});

const routeTree = rootRoute.addChildren([
  homeRoute,
  chatRoute,
  runsRoute,
  runDetailRoute,
  sessionsRoute,
  sessionDetailRoute,
  editorRoute,
  registriesRoute,
  mcpRoute,
  ragRoute,
  settingsRoute,
]);

export const router = createRouter({ routeTree, defaultPreload: "intent" });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
