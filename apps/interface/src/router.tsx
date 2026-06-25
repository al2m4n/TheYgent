import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { Editor } from "./routes/Editor";
import { Home } from "./routes/Home";
import { Mcp } from "./routes/Mcp";
import { Registries } from "./routes/Registries";
import { Root } from "./routes/Root";

const rootRoute = createRootRoute({ component: Root });

const homeRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: Home });

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

const routeTree = rootRoute.addChildren([homeRoute, editorRoute, registriesRoute, mcpRoute]);

export const router = createRouter({ routeTree, defaultPreload: "intent" });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
