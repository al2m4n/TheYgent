// The auth gate's three worlds: a fresh install boots into the setup wizard, a signed-out
// browser gets the login screen (with one SSO button per enabled provider), and a valid
// session renders the app. Plus the client-side role order every nav/route gate uses.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthGate } from "../src/auth/AuthGate";
import type { AuthStatus } from "../src/lib/api";
import { AuthProvider, roleAtLeast } from "../src/lib/auth";
import { ThemeProvider } from "../src/lib/theme";

function stubLocalStorage() {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  });
}

function stubStatus(status: AuthStatus) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL) => {
      expect(String(url)).toContain("/auth/status");
      return {
        ok: true,
        status: 200,
        json: async () => status,
      } as Response;
    }),
  );
}

function mount(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <ThemeProvider>
      <QueryClientProvider client={qc}>
        <AuthProvider>
          <AuthGate>{children}</AuthGate>
        </AuthProvider>
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

const USER = {
  id: "usr_1",
  username: "sam",
  display_name: "Sam",
  email: null,
  role: "admin" as const,
  disabled: false,
  has_password: true,
  avatar_url: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  last_login_at: null,
};

describe("AuthGate", () => {
  beforeEach(stubLocalStorage);
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("boots a fresh install into the setup wizard", async () => {
    stubStatus({ setup_required: true, user: null, providers: [] });
    mount(<div>the app</div>);
    expect(await screen.findByText(/Welcome to TheYgent/)).toBeInTheDocument();
    expect(screen.queryByText("the app")).not.toBeInTheDocument();
  });

  it("shows the login screen — with a button per SSO provider — when signed out", async () => {
    stubStatus({
      setup_required: false,
      user: null,
      providers: [{ slug: "okta", name: "Okta" }],
    });
    mount(<div>the app</div>);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Okta/ })).toBeInTheDocument();
    expect(screen.queryByText("the app")).not.toBeInTheDocument();
  });

  it("renders the app when the session resolves to a user", async () => {
    stubStatus({ setup_required: false, user: USER, providers: [] });
    mount(<div>the app</div>);
    expect(await screen.findByText("the app")).toBeInTheDocument();
  });
});

describe("roleAtLeast", () => {
  it("orders viewer < editor < admin and fails closed on undefined", () => {
    expect(roleAtLeast("admin", "viewer")).toBe(true);
    expect(roleAtLeast("editor", "editor")).toBe(true);
    expect(roleAtLeast("viewer", "editor")).toBe(false);
    expect(roleAtLeast("editor", "admin")).toBe(false);
    expect(roleAtLeast(undefined, "viewer")).toBe(false);
  });
});
