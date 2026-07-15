// A list page can be shown as a spacious card grid or a dense row list; the choice is the user's and
// sticks per page. Pure UI chrome (localStorage, keyed by page), never the IR — mirrors the theme
// preference. `initial` is the page's natural default (the card pages start on "grid", the table
// pages on "list"); a stored choice wins over it.

import { useCallback, useState } from "react";

export type ViewMode = "grid" | "list";

const KEY_PREFIX = "theygent.view.";

function read(page: string, initial: ViewMode): ViewMode {
  try {
    const v = localStorage.getItem(KEY_PREFIX + page);
    if (v === "grid" || v === "list") return v;
  } catch {
    // no localStorage (tests / private mode) — fall through to the default
  }
  return initial;
}

export function useViewMode(page: string, initial: ViewMode): [ViewMode, (v: ViewMode) => void] {
  const [view, setView] = useState<ViewMode>(() => read(page, initial));
  const set = useCallback(
    (v: ViewMode) => {
      setView(v);
      try {
        localStorage.setItem(KEY_PREFIX + page, v);
      } catch {
        // best-effort persistence; the in-memory state still flips
      }
    },
    [page],
  );
  return [view, set];
}
