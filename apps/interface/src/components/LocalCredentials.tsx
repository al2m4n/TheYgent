import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { Button, ErrorBanner, Field, Input } from "./ui";

// Settings → Local credentials: manage the named secrets that reachable (openai-compatible) model
// bindings reference as `secret://NAME`. Values are WRITE-ONLY — the store never returns a value, so
// the list shows names only ("set"), and nothing here leaves the machine (the inference plane is the
// user's own service). Pick these names in the Add-model modal instead of typing a raw ref.
export function LocalCredentials() {
  const qc = useQueryClient();
  const {
    data: creds,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["credentials"],
    queryFn: () => api.listCredentials(),
  });
  const [name, setName] = useState("");
  const [value, setValue] = useState("");

  // Deleting a secret is irreversible (the store is write-only — the value can't be re-read), so
  // removal is a two-step inline confirm: first click arms, second click deletes. The armed state
  // auto-resets after a few seconds so a stray click never lingers.
  const [confirming, setConfirming] = useState<string | null>(null);
  const confirmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (confirmTimer.current) clearTimeout(confirmTimer.current);
    },
    [],
  );

  const add = useMutation({
    mutationFn: () => api.putCredential(name.trim(), value),
    onSuccess: () => {
      setName("");
      setValue("");
      qc.invalidateQueries({ queryKey: ["credentials"] });
    },
  });
  const remove = useMutation({
    mutationFn: (n: string) => api.deleteCredential(n),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["credentials"] }),
  });

  const canAdd = name.trim() !== "" && value !== "" && !add.isPending;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
          Local credentials
        </span>
        <span className="text-[10px] text-slate-600">
          stored on this machine · never sent to theygent
        </span>
      </div>

      {isLoading ? (
        <p className="text-xs text-slate-500">Loading…</p>
      ) : creds && creds.length > 0 ? (
        <ul className="space-y-1">
          {creds.map((c) => (
            <li
              key={c.name}
              className="flex items-center justify-between rounded border border-slate-800 bg-[var(--c-surface)] px-2.5 py-1.5"
            >
              <span className="mono truncate text-xs text-slate-200" title={c.name}>
                {c.name}
              </span>
              <div className="flex items-center gap-2">
                <span className="text-[10px] text-slate-500">•••••• set</span>
                <button
                  type="button"
                  onClick={() => {
                    if (confirmTimer.current) clearTimeout(confirmTimer.current);
                    if (confirming === c.name) {
                      setConfirming(null);
                      remove.mutate(c.name);
                    } else {
                      setConfirming(c.name);
                      confirmTimer.current = setTimeout(() => setConfirming(null), 3000);
                    }
                  }}
                  disabled={remove.isPending}
                  className="text-[11px] text-rose-600 hover:underline disabled:opacity-50 dark:text-rose-400"
                >
                  {confirming === c.name ? "Confirm remove?" : "Remove"}
                </button>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-slate-500">
          No credentials yet. Add an API key below, then reference it as{" "}
          <span className="mono">secret://NAME</span> when registering a hosted model.
        </p>
      )}

      <div className="flex items-end gap-2 pt-1">
        <div className="flex-1">
          <Field label="Name">
            <Input
              value={name}
              placeholder="OPENAI_API_KEY"
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
        </div>
        <div className="flex-1">
          <Field label="Value (write-only)">
            <Input
              type="password"
              value={value}
              placeholder="sk-…"
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && canAdd) add.mutate();
              }}
            />
          </Field>
        </div>
        <Button variant="primary" onClick={() => canAdd && add.mutate()} disabled={!canAdd}>
          Add
        </Button>
      </div>
      <ErrorBanner error={add.error ?? remove.error ?? error} />
    </div>
  );
}
