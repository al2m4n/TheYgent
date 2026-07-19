// The identity layer's client half. One boot probe (GET /auth/status) decides which of three
// worlds the app is in — first-run setup, signed out, or signed in — and the AuthGate renders
// the wizard / login screen / the real app accordingly. The session bearer lives in
// localStorage (api.ts owns it); this module owns WHO is signed in and the transitions.
//
// The OIDC return leg is consumed here too: the control plane bounces the browser back to
// `/auth/callback#token=…` (or `#error=…`) — the fragment is read and scrubbed BEFORE the
// router ever renders, so no route needs to exist for it and the token never sits in history.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type ReactNode,
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  type AuthStatus,
  type AuthUser,
  type PublicProvider,
  type Role,
  UNAUTHORIZED_EVENT,
  api,
  setAuthToken,
} from "./api";
import { notify } from "./notify";

const ROLE_RANK: Record<Role, number> = { viewer: 0, editor: 1, admin: 2 };

/** viewer < editor < admin — the one ordering every gate uses. */
export function roleAtLeast(role: Role | undefined, minimum: Role): boolean {
  return role !== undefined && ROLE_RANK[role] >= ROLE_RANK[minimum];
}

// Stable codes the OIDC callback can land with — mapped to human copy on the login screen.
const OIDC_ERRORS: Record<string, string> = {
  oidc_denied: "The sign-in was cancelled at the provider.",
  oidc_error: "Sign-in failed at the identity provider. Ask an admin to check its settings.",
  provider_not_found: "That sign-in provider is no longer configured.",
  provider_secret_unreadable:
    "This provider's client secret can't be read — an admin needs to re-enter it (Settings → Sign-in → Edit).",
  domain_not_allowed: "Your email or domain isn't allowed for this workspace.",
  account_not_provisioned: "No account exists for you yet — ask an admin to create one.",
  account_disabled: "Your account is disabled.",
};

/** Read `/auth/callback#code=…` / `#error=…` BEFORE first render: capture the one-time code (to
 *  be exchanged for the real bearer) or the error, and scrub the URL so neither the code nor an
 *  error lingers in history. Runs once at module load; returns what it found. */
function consumeOidcCallback(): { code: string | null; error: string | null } {
  if (typeof window === "undefined") return { code: null, error: null };
  if (!window.location.pathname.startsWith("/auth/callback")) return { code: null, error: null };
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const code = fragment.get("code");
  const error = fragment.get("error");
  window.history.replaceState(null, "", "/");
  return { code, error };
}

const oidcCallback = consumeOidcCallback();

export interface AuthState {
  /** Which world the app is in. `loading` = the boot probe hasn't answered yet;
   *  `unreachable` = the control plane didn't answer at all (render a retry, not a login). */
  phase: "loading" | "unreachable" | "setup" | "login" | "ready";
  user: AuthUser | null;
  providers: PublicProvider[];
  /** Login-screen banner from a failed OIDC return leg (null = none). */
  oidcError: string | null;
  hasRole: (minimum: Role) => boolean;
  signIn: (username: string, password: string) => Promise<void>;
  completeSetup: (username: string, password: string, displayName?: string) => Promise<void>;
  signOut: () => Promise<void>;
  refresh: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const status = useQuery<AuthStatus>({
    queryKey: ["auth", "status"],
    queryFn: () => api.authStatus(),
    staleTime: 60_000,
    retry: 1,
  });

  // The OIDC return leg: the callback delivered a one-time code (not the bearer). Redeem it for
  // the real session, once, on mount. While it's in flight the gate stays on "loading" so the
  // login screen never flashes; a failed redemption surfaces as a login-screen banner.
  const [exchange, setExchange] = useState<{ pending: boolean; error: string | null }>(() => ({
    pending: oidcCallback.code !== null,
    error: null,
  }));
  const exchangedRef = useRef(false);
  useEffect(() => {
    if (exchangedRef.current || oidcCallback.code === null) return;
    exchangedRef.current = true;
    api
      .oidcExchange(oidcCallback.code)
      .then(async (session) => {
        setAuthToken(session.token);
        await qc.invalidateQueries({ queryKey: ["auth", "status"] });
        setExchange({ pending: false, error: null });
      })
      .catch(() => setExchange({ pending: false, error: "oidc_error" }));
  }, [qc]);

  // A dead session anywhere in the app (expired, revoked, account disabled) returns the shell
  // to the sign-in screen. The event fires only on 401 `unauthorized` — never on a failed
  // login attempt.
  useEffect(() => {
    const onUnauthorized = () => {
      setAuthToken(null);
      qc.invalidateQueries({ queryKey: ["auth", "status"] });
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [qc]);

  const value = useMemo<AuthState>(() => {
    const data = status.data;
    const phase: AuthState["phase"] =
      exchange.pending || status.isPending
        ? "loading"
        : status.isError
          ? "unreachable"
          : data?.setup_required
            ? "setup"
            : data?.user
              ? "ready"
              : "login";
    const user = data?.user ?? null;
    const oidcError = oidcCallback.error ?? exchange.error;

    const installSession = async (session: { token: string; user: AuthUser }) => {
      setAuthToken(session.token);
      qc.setQueryData<AuthStatus>(["auth", "status"], {
        setup_required: false,
        user: session.user,
        providers: data?.providers ?? [],
      });
      // Everything cached so far was fetched as nobody/another account — refetch the world.
      await qc.invalidateQueries();
    };

    return {
      phase,
      user,
      providers: data?.providers ?? [],
      oidcError: oidcError ? (OIDC_ERRORS[oidcError] ?? "Sign-in failed.") : null,
      hasRole: (minimum) => roleAtLeast(user?.role, minimum),
      signIn: async (username, password) => {
        await installSession(await api.authLogin({ username, password }));
      },
      completeSetup: async (username, password, displayName) => {
        await installSession(
          await api.authSetup({ username, password, display_name: displayName }),
        );
      },
      signOut: async () => {
        try {
          await api.authLogout();
        } catch {
          // Revoking a dead session is still a sign-out — never block the exit.
        }
        setAuthToken(null);
        // Flip the gate FIRST (write the known signed-out state into the status query) — that
        // unmounts the app and every per-account query observer; only then drop their caches.
        // Never qc.clear(): it would delete the status entry out from under its own observer,
        // and the follow-up invalidate would match nothing (the gate then never flips).
        qc.setQueryData<AuthStatus>(["auth", "status"], {
          setup_required: false,
          user: null,
          providers: data?.providers ?? [],
        });
        qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "auth" });
        await qc.invalidateQueries({ queryKey: ["auth", "status"] });
        notify.success("Signed out");
      },
      refresh: () => {
        void qc.invalidateQueries({ queryKey: ["auth", "status"] });
      },
    };
  }, [status.data, status.isPending, status.isError, exchange.pending, exchange.error, qc]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (ctx === null) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
