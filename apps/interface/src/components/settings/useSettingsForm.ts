// The page-level dirty-state store for the control-plane settings catalog. ONE instance lives on
// the Settings route and every tab renders from it, so staged edits survive tab switches; each
// tab's save bar PATCHes only its own group's keys. Client-side invalid fields register as
// "blockers" that hold the save button until fixed (the server would 422 the whole batch
// otherwise — validation there is atomic).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useState } from "react";
import { type SettingEntry, type SettingGroup, type SettingsView, api } from "../../lib/api";
import { notify } from "../../lib/notify";

/**
 * What an editable control shows before any edit: the stored value when one exists and still
 * validates, else the effective value (the default — or the env value, for a pinned key).
 */
export function controlValue(e: SettingEntry): unknown {
  if (e.stored_invalid) return e.value;
  return e.stored_value ?? e.value;
}

// Structural equality for staged-edit dedupe (settings values are small JSON scalars/arrays).
function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

export interface PlatformSettingsForm {
  data: SettingsView | undefined;
  isLoading: boolean;
  loadError: unknown;
  entries: (group: SettingGroup) => SettingEntry[];
  entry: (key: string) => SettingEntry | undefined;
  /** The staged PATCH body: {key: value} to set, {key: null} to reset to default. */
  edits: Record<string, unknown>;
  setEdit: (key: string, value: unknown) => void;
  /** Stage a reset-to-default (PATCHes null). */
  stageReset: (key: string) => void;
  /** Drop a staged edit (back to the server state). */
  unstage: (key: string) => void;
  /** Client-side validation failures, keyed by setting key — they hold the group's save. */
  blockers: Record<string, string>;
  setBlocker: (key: string, message: string | null) => void;
  /** Per-key live-apply hook failures from the LAST save (value stored; hook failed). */
  applyErrors: Record<string, string>;
  dirtyKeys: (group: SettingGroup) => string[];
  blockerKeys: (group: SettingGroup) => string[];
  saveGroup: (group: SettingGroup) => void;
  /** Delete an orphaned stored row (a key no longer in the catalog): PATCH {key: null}. */
  removeOrphan: (key: string) => void;
  saving: boolean;
  saveError: unknown;
}

/** The value a control should DISPLAY: a staged edit wins; a staged reset previews the default. */
export function displayValue(form: PlatformSettingsForm, e: SettingEntry): unknown {
  if (e.key in form.edits) {
    const v = form.edits[e.key];
    return v === null ? e.default : v;
  }
  return controlValue(e);
}

export function usePlatformSettingsForm(): PlatformSettingsForm {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["settings"], queryFn: () => api.getSettings() });
  const [edits, setEdits] = useState<Record<string, unknown>>({});
  const [blockers, setBlockers] = useState<Record<string, string>>({});
  const [applyErrors, setApplyErrors] = useState<Record<string, string>>({});

  const byKey = useMemo(() => new Map((q.data?.settings ?? []).map((e) => [e.key, e])), [q.data]);

  const setEdit = useCallback(
    (key: string, value: unknown) => {
      setEdits((prev) => {
        const e = byKey.get(key);
        // Typing the server value back un-dirties the field instead of staging a no-op write.
        if (e && same(value, controlValue(e))) {
          if (!(key in prev)) return prev;
          const { [key]: _dropped, ...rest } = prev;
          return rest;
        }
        return { ...prev, [key]: value };
      });
    },
    [byKey],
  );

  const stageReset = useCallback((key: string) => {
    setEdits((prev) => ({ ...prev, [key]: null }));
    setBlockers((prev) => {
      if (!(key in prev)) return prev;
      const { [key]: _dropped, ...rest } = prev;
      return rest;
    });
  }, []);

  const unstage = useCallback((key: string) => {
    setEdits((prev) => {
      if (!(key in prev)) return prev;
      const { [key]: _dropped, ...rest } = prev;
      return rest;
    });
    setBlockers((prev) => {
      if (!(key in prev)) return prev;
      const { [key]: _dropped, ...rest } = prev;
      return rest;
    });
  }, []);

  const setBlocker = useCallback((key: string, message: string | null) => {
    setBlockers((prev) => {
      if (message === null) {
        if (!(key in prev)) return prev;
        const { [key]: _dropped, ...rest } = prev;
        return rest;
      }
      if (prev[key] === message) return prev;
      return { ...prev, [key]: message };
    });
  }, []);

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.patchSettings(body),
    onSuccess: (res, body) => {
      // The response IS the post-write GET shape (+ apply_errors) — install it as the cache so
      // every tab re-renders from the echoed state without a refetch.
      const { apply_errors, ...view } = res;
      qc.setQueryData<SettingsView>(["settings"], view);
      // Drop a staged edit only when it still matches what this PATCH sent — a re-edit typed
      // while the request was in flight is NOT saved yet and must stay dirty.
      setEdits((prev) =>
        Object.fromEntries(
          Object.entries(prev).filter(([k, v]) => !(k in body) || !same(v, body[k])),
        ),
      );
      setApplyErrors(apply_errors ?? {});
      const failed = Object.keys(apply_errors ?? {}).length;
      if (failed > 0) {
        notify.warning(
          `Saved, but ${failed} ${failed === 1 ? "value" : "values"} could not be applied live — see the fields below.`,
        );
      } else {
        notify.success("Settings saved");
      }
    },
  });

  const groupOf = useCallback((key: string) => byKey.get(key)?.group, [byKey]);

  const dirtyKeys = useCallback(
    (group: SettingGroup) => Object.keys(edits).filter((k) => groupOf(k) === group),
    [edits, groupOf],
  );

  const blockerKeys = useCallback(
    (group: SettingGroup) => Object.keys(blockers).filter((k) => groupOf(k) === group),
    [blockers, groupOf],
  );

  const saveGroup = useCallback(
    (group: SettingGroup) => {
      const body = Object.fromEntries(Object.entries(edits).filter(([k]) => groupOf(k) === group));
      if (Object.keys(body).length === 0) return;
      save.mutate(body);
    },
    [edits, groupOf, save],
  );

  const removeOrphan = useCallback((key: string) => save.mutate({ [key]: null }), [save]);

  return {
    data: q.data,
    isLoading: q.isLoading,
    loadError: q.error,
    entries: (group) => (q.data?.settings ?? []).filter((e) => e.group === group),
    entry: (key) => byKey.get(key),
    edits,
    setEdit,
    stageReset,
    unstage,
    blockers,
    setBlocker,
    applyErrors,
    dirtyKeys,
    blockerKeys,
    saveGroup,
    removeOrphan,
    saving: save.isPending,
    saveError: save.error,
  };
}
