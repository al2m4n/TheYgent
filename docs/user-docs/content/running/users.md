# Users, roles & sign-in

TheYgent's control plane has an **identity layer**: every person gets an account with one of three roles, every call to the API carries a bearer token, and admins can wire up single sign-on through any OIDC or OAuth2 provider. This page covers the first-run wizard, what each role can do, managing accounts, SSO, and the personal API keys that let your own scripts and frontends call TheYgent.

All examples target the control plane at `http://localhost:8080`.

## First run: create the admin

A fresh install has **zero accounts, and the API is closed** — every management endpoint returns `401` until the first account exists. The only surfaces that answer before setup are the sign-in machinery itself (`/auth/status`, `/auth/setup`, `/auth/login`, and the SSO browser redirects), the health probes (`/healthz`, `/readyz`), and the unattended entry points that carry their own credentials ([webhooks](triggers.md#webhooks) with their HMAC signature, and `POST /agents/{id}/invoke` behind its own gate — see [API keys](#personal-api-keys) below).

The first time you open the interface at `http://localhost:5174`, it shows a **first-run wizard**: pick a username and password, and you land in the app signed in. The account it creates is always an **admin** — someone has to own the install.

Under the hood the wizard makes one call:

```bash
curl -X POST http://localhost:8080/auth/setup \
  -H "Content-Type: application/json" \
  -d '{"username": "ada", "password": "correct-horse-battery", "display_name": "Ada"}'
```

It returns `201` with `{"token": "tys_…", "user": {…}}` — you are already signed in. Once **any** account exists, setup is permanently closed: further calls return `409 setup_already_complete`.

## Signing in

Local accounts sign in with a username and password:

- **Usernames are lowercase handles** — letters and digits, with `.` `_` `-` allowed after the first character. Sign-in is case-insensitive (`Ada` and `ada` are the same account); the display name is free text.
- **Passwords are at least 8 characters** and are stored only as a memory-hard hash — never recoverable, by anyone.

`POST /auth/login` returns `{token, user}`. The token is an opaque `tys_…` bearer that you present on every subsequent call as `Authorization: Bearer tys_…`; the server stores only its hash. A wrong password and an unknown username get one indistinguishable answer — `401 invalid_credentials` — while a disabled account is its own honest `403 account_disabled`.

!!! note "Session lifetime"
    A sign-in lasts **168 hours (7 days)** by default. Admins change this with the `auth.session_ttl_hours` platform setting on the Settings page; the new value applies to sessions minted after the change — existing sessions keep the expiry they were issued with. **Sign out** (in the profile menu) revokes the presented session immediately.

## The three roles

| What you can do | viewer | editor | admin |
|---|---|---|---|
| Chat with models and published agents; run published agents; your own chat sessions; read the agents list; inspect your runs and traces | ✓ | ✓ | ✓ |
| Manage your own profile: display name, theme, password, personal API keys | ✓ | ✓ | ✓ |
| Build agents: the editor, drafts, publishing, inline test runs | — | ✓ | ✓ |
| Configure the building blocks: connections, MCP servers, RAG sources, triggers, the Bench | — | ✓ | ✓ |
| Own the install: platform settings, user management, sign-in providers, import/export | — | — | ✓ |

Roles are ordered — an editor can do everything a viewer can, an admin everything an editor can. The floors are enforced **server-side, per endpoint**: a call below the floor returns `403 forbidden` no matter what any client shows. The interface's navigation is role-aware as a courtesy — **Runs** and the **Configuration** group appear from editor up, and the **Settings** entry is admin-only — but hiding is UX, not the enforcement.

### Chat sessions are per-account

Your conversations are yours. A viewer or editor sees only their **own** chat sessions (plus any ownerless sessions recorded before the identity layer existed); another user's session id answers `404`, exactly like a session that doesn't exist. Admins operate the install and can see and manage every session.

## Managing users (admin)

Admins manage accounts under **Settings → Users** in the interface, or over the API:

| Action | Endpoint | Notes |
|---|---|---|
| List | `GET /users` | The install's whole account list. |
| Create | `POST /users` | `username`, `role` (default `viewer`), optional `password`, `display_name`, `email`. A taken handle is `409 username_taken`. |
| Edit | `PATCH /users/{id}` | Change `display_name`, `email`, `role`, or `disabled`. |
| Reset password | `POST /users/{id}/password` | Sets a new password **and revokes every live session** of that account — the reset exists because the old credential can no longer be trusted. |
| Delete | `DELETE /users/{id}` | Returns `204`. The account's sessions and API keys die with it; runs and chat history keep their ownership breadcrumb — history outlives the account. |

Two behaviors worth calling out:

- **Disabling is immediate.** Setting `disabled: true` revokes every live session on the spot, and API keys owned by a disabled account stop resolving. A later sign-in attempt gets `403 account_disabled`.
- **Creating an account without a password** makes an **SSO-only** account: the user cannot password-login and is expected to arrive through a sign-in provider. Give it an `email` and the first provider sign-in with a matching verified email claims it — see [pre-provisioning](#who-gets-in-auto-provision-domains-and-pre-provisioning).

!!! warning "The last admin is protected"
    The last active admin can never be demoted, disabled, or deleted — any attempt returns `409 last_admin`. Promote a second admin first. This is deliberate: a TheYgent install with no admin has no in-band way to recover itself.

### Self-service

Every user, regardless of role, manages their own account from the **profile menu** at the bottom of the sidebar: display name, interface theme, password, API keys, and sign out. Changing your own password requires your current password and signs out every *other* session of your account (the one making the change survives). Over the API this is `GET/PATCH /auth/me`, `POST /auth/me/password`, and `POST /auth/logout`.

## Personal API keys

An API key is a long-lived programmatic bearer — the recommended way for **your own frontends, scripts, and CI jobs** to call the control plane. Any user can mint one from the profile menu (**API keys → Create**), or over the API:

```bash
curl -X POST http://localhost:8080/auth/api-keys \
  -H "Authorization: Bearer tys_your-session-token" \
  -H "Content-Type: application/json" \
  -d '{"name": "deploy-script"}'
```

The `201` response contains the raw `tyk_…` token **exactly once** — it is stored hashed and can never be shown again. Afterwards, only the key's first characters identify it in the list.

The rules that keep keys safe:

- **A key never widens.** Its role is capped at your role when minted (requesting more is a `400`), *and* narrowed to your current role at use — demote an account and every key it owns instantly narrows with it, no rotation needed.
- **Revocation is immediate.** `DELETE /auth/api-keys/{id}` (or the interface's Revoke button) kills the key on the spot.

A key opens **both** the regular API surface and the unattended invoke endpoint. Calling a regular endpoint:

```bash
curl http://localhost:8080/agents \
  -H "Authorization: Bearer tyk_your-api-key"
```

Calling a published agent's [unattended invoke endpoint](triggers.md#token-invoke):

```bash
curl -X POST http://localhost:8080/agents/agent.my-agent/invoke \
  -H "Authorization: Bearer tyk_your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"input": "summarize today'\''s incidents"}'
```

!!! note "What opens `/invoke`"
    `POST /agents/{id}/invoke` accepts an API key **or** the per-deploy `THEYGENT_INVOKE_TOKEN` (see [Triggers](triggers.md#token-invoke)). Interactive `tys_…` session tokens deliberately do **not** open it — it is the unattended surface. With neither credential configured nor any key presented, the endpoint stays closed (`401`).

## Single sign-on

Admins configure sign-in providers under **Settings → Sign-in**. Each enabled provider becomes a button on the login screen; the whole flow is authorization code + PKCE, resolved server-side — the control plane exchanges the code and reads the provider's userinfo endpoint, then bounces the browser back to the interface with a fresh session token.

A provider comes in two shapes:

- **OIDC** — set `issuer_url` and the control plane discovers the endpoints (`/.well-known/openid-configuration`).
- **Plain OAuth2** — for providers without a discovery document, set the three endpoints explicitly: `authorization_endpoint`, `token_endpoint`, `userinfo_endpoint`.

Common to both: `client_id`, a **client secret** (write-only — encrypted at rest, never echoed back; sending a new one rotates it in place), `scopes` (default `openid profile email`), and the provisioning policy below.

!!! warning "Set a persistent secret key first"
    The client secret is encrypted with `THEYGENT_SECRET_KEY`. If that variable is unset, the control plane falls back to an **ephemeral key that does not survive a restart** — so after the next restart the stored secret can no longer be decrypted and sign-in fails until you re-enter it. Set a stable [`THEYGENT_SECRET_KEY`](../reference/environment.md) *before* configuring a provider (generate one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).

### The redirect URL

When you register the app with your provider, the redirect / callback URL to enter is:

```text
http://localhost:8080/auth/oidc/callback
```

— that is, `{control-plane}/auth/oidc/callback`. If your control plane is reached at a different address (a reverse proxy, a non-default bind), change it with the `auth.oidc_redirect_url` platform setting or pin it with the `THEYGENT_OIDC_REDIRECT_URL` [environment variable](../reference/environment.md).

### Worked example: an OIDC provider (Google-style)

1. In the provider's console, create an OAuth client, register the redirect URL above, and note the client id and secret.
2. Create the provider (interface: **Settings → Sign-in → Add provider**; API below):

```bash
curl -X POST http://localhost:8080/auth/providers \
  -H "Authorization: Bearer tys_your-admin-session" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Google",
    "slug": "google",
    "config": {
      "issuer_url": "https://accounts.google.com",
      "client_id": "1234567890-abc.apps.googleusercontent.com",
      "scopes": "openid profile email",
      "auto_provision": true,
      "default_role": "viewer",
      "allowed_domains": ["you@example.com"]
    },
    "client_secret": "GOCSPX-your-client-secret"
  }'
```

A **Google** button appears on the login screen. A half-configured provider (neither an `issuer_url` nor all three explicit endpoints) is rejected up front with `400 invalid_provider`, and a taken slug is `409 provider_exists`.

### Plain OAuth2 example: GitHub

GitHub has no OIDC discovery document, so configure the endpoints explicitly:

```json
{
  "authorization_endpoint": "https://github.com/login/oauth/authorize",
  "token_endpoint": "https://github.com/login/oauth/access_token",
  "userinfo_endpoint": "https://api.github.com/user",
  "client_id": "your-github-client-id",
  "scopes": "read:user user:email"
}
```

!!! note "Plain-OAuth2 identity is what the provider returns"
    Identity comes from the provider's userinfo response — GitHub sends `id` and `login` rather than OIDC's `sub`. If the account's email is not exposed there, the created account has no email: a domain allowlist will then **deny** the sign-in (that is the policy working as intended), and email-based pre-provisioning cannot match.

### Who gets in: auto-provision, domains, and pre-provisioning

Three per-provider knobs decide what happens on a first sign-in:

- **`auto_provision`** (default on) — create an account automatically for anyone the provider vouches for, at **`default_role`** (`viewer`, `editor`, or `admin`; default `viewer`). With auto-provision off, unknown users are turned away with an `account_not_provisioned` message.
- **`allowed_domains`** — a sign-in allowlist. Each entry is either a **full email** (an exact-address allow) or a **bare domain** (a whole-organization allow). `["you@example.com"]` admits only that one address; `["example.com"]` admits everyone at `…@example.com`. A sign-in whose email is missing or matches nothing is refused (`domain_not_allowed`); no list means no restriction. For a personal install, list your own email so a domain-wide account can't sign in as you.
- **Pre-provisioning by email** — create the account yourself first (`POST /users` with an `email` and **no password**), and the first provider sign-in with a matching verified email links to it, keeping the role you chose. Only password-*less* accounts can be claimed this way — an account with a password is never linkable through a provider merely asserting an email.

After the first sign-in the provider identity is **linked** to the account: later sign-ins land on the same account even if the email changes, and a disabled account stays refused regardless of what the provider says. If the provider supplies a profile picture, it becomes the account's avatar (shown in the sidebar and profile) and refreshes on each sign-in; local password accounts show their initials instead.

Deleting a provider removes the sign-in button and its stored secret; accounts created through it survive (they just need another way in — a password set by an admin, or another provider).

## Related pages

- [Triggers & webhooks](triggers.md) — the unattended entry points API keys open.
- [Environment variables](../reference/environment.md) — `THEYGENT_OIDC_REDIRECT_URL`, `THEYGENT_INVOKE_TOKEN`.
- [Import & export](../import-export/index.md) — admin-only, like everything that moves the whole install.
- [Runs & sessions](../concepts/runs-and-sessions.md) — the history that outlives accounts.
