import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { AgentDetail, AgentsList } from "./routes/Agents";
import { Compose } from "./routes/Compose";
import { Registries } from "./routes/Registries";
import { Root } from "./routes/Root";
import { RunDetail } from "./routes/RunDetail";
import { RunsList } from "./routes/RunsList";
import { ThreadDetail } from "./routes/ThreadDetail";
import { ThreadsList } from "./routes/ThreadsList";

const rootRoute = createRootRoute({ component: Root });

const runsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: RunsList });
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
const agentsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/agents",
  component: AgentsList,
});
const agentDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/agents/$agentId",
  component: AgentDetail,
});

const routeTree = rootRoute.addChildren([
  runsRoute,
  runDetailRoute,
  threadsRoute,
  threadDetailRoute,
  composeRoute,
  registriesRoute,
  agentsRoute,
  agentDetailRoute,
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
