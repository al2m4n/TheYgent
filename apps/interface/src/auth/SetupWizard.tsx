// First-run setup: the wizard a fresh install shows before anything else. One real decision —
// the admin account — book-ended by a welcome step and a "what roles exist" primer, so the
// person who installs TheYgent also learns how to bring in their team. POST /auth/setup only
// works while the install has zero users; success signs the new admin straight in.

import { useMutation } from "@tanstack/react-query";
import { Eye, Pencil, Rocket, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Button, ErrorBanner, Field, Input } from "../components/ui";
import { useAuth } from "../lib/auth";
import { AuthShell } from "./AuthShell";

const MIN_PASSWORD = 8;

const ROLE_PRIMER = [
  {
    icon: ShieldCheck,
    name: "Admin",
    blurb: "Owns the install: settings, users, sign-in providers, import/export.",
  },
  {
    icon: Pencil,
    name: "Editor",
    blurb: "Builds agents: the canvas, connections, MCP, RAG, triggers.",
  },
  { icon: Eye, name: "Viewer", blurb: "Uses agents: chat, run published agents." },
] as const;

export function SetupWizard() {
  const { completeSetup } = useAuth();
  const [step, setStep] = useState(0);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");

  const create = useMutation({
    mutationFn: () => completeSetup(username.trim(), password, displayName.trim() || undefined),
  });

  const mismatch = confirm !== "" && confirm !== password;
  const tooShort = password !== "" && password.length < MIN_PASSWORD;
  const canCreate =
    username.trim() !== "" &&
    password.length >= MIN_PASSWORD &&
    confirm === password &&
    !create.isPending;

  return (
    <AuthShell footer="You can add teammates any time under Settings → Users.">
      <div className="space-y-4">
        {/* Step dots — a small, honest progress cue for a three-beat flow. */}
        <div className="flex items-center gap-1.5" aria-label={`Step ${step + 1} of 3`}>
          {[0, 1, 2].map((i) => (
            <span
              key={i}
              className={`h-1.5 rounded-full transition-all ${
                i === step ? "w-6 bg-primary" : "w-1.5 bg-muted-foreground/30"
              }`}
            />
          ))}
        </div>

        {step === 0 && (
          <div className="space-y-4">
            <div>
              <h1 className="text-base font-semibold text-foreground">Welcome to TheYgent</h1>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                Your agents, your models, your machine. Before anything runs, this install needs its
                first account — the administrator. That's you.
              </p>
            </div>
            <Button variant="primary" className="w-full" onClick={() => setStep(1)}>
              <Rocket size={15} />
              Get started
            </Button>
          </div>
        )}

        {step === 1 && (
          <div className="space-y-3">
            <div>
              <h1 className="text-base font-semibold text-foreground">Create the admin account</h1>
              <p className="text-xs text-muted-foreground">
                Stored locally, password hashed — nothing leaves this machine.
              </p>
            </div>
            <Field label="Username">
              <Input
                value={username}
                autoFocus
                autoComplete="username"
                placeholder="e.g. sam"
                onChange={(e) => setUsername(e.target.value)}
              />
            </Field>
            <Field label="Display name (optional)">
              <Input
                value={displayName}
                placeholder="How your name appears"
                onChange={(e) => setDisplayName(e.target.value)}
              />
            </Field>
            <Field label="Password">
              <Input
                type="password"
                value={password}
                autoComplete="new-password"
                placeholder={`At least ${MIN_PASSWORD} characters`}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
            <Field label="Confirm password">
              <Input
                type="password"
                value={confirm}
                autoComplete="new-password"
                onChange={(e) => setConfirm(e.target.value)}
              />
            </Field>
            {tooShort && (
              <p className="text-xs text-destructive">
                Password must be at least {MIN_PASSWORD} characters.
              </p>
            )}
            {mismatch && <p className="text-xs text-destructive">Passwords don't match.</p>}
            <div className="flex gap-2 pt-1">
              <Button onClick={() => setStep(0)}>Back</Button>
              <Button
                variant="primary"
                className="flex-1"
                disabled={!(username.trim() && password.length >= MIN_PASSWORD && !mismatch)}
                onClick={() => setStep(2)}
              >
                Continue
              </Button>
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="space-y-4">
            <div>
              <h1 className="text-base font-semibold text-foreground">Three roles, kept simple</h1>
              <p className="text-xs text-muted-foreground">
                Invite teammates later from Settings → Users, or wire up SSO under Sign-in.
              </p>
            </div>
            <ul className="space-y-2.5">
              {ROLE_PRIMER.map(({ icon: Icon, name, blurb }) => (
                <li key={name} className="flex items-start gap-2.5">
                  <Icon size={15} className="mt-0.5 shrink-0 text-primary" />
                  <div className="min-w-0 text-xs leading-relaxed">
                    <span className="font-medium text-foreground">{name}</span>{" "}
                    <span className="text-muted-foreground">— {blurb}</span>
                  </div>
                </li>
              ))}
            </ul>
            <div className="flex gap-2">
              <Button onClick={() => setStep(1)} disabled={create.isPending}>
                Back
              </Button>
              <Button
                variant="primary"
                className="flex-1"
                disabled={!canCreate}
                onClick={() => canCreate && create.mutate()}
              >
                {create.isPending ? "Creating…" : "Create account & enter"}
              </Button>
            </div>
            <ErrorBanner error={create.error} />
          </div>
        )}
      </div>
    </AuthShell>
  );
}
