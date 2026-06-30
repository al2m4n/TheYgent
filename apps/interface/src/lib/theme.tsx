// Theme: Dark / Light / System, persisted, applied as a class on <html> (+ color-scheme). The actual
// palette lives in index.css — `html.light` overrides the surface tokens (--c-*) and the Tailwind
// slate ramp, so every slate-* / surface utility flips with no per-component edits. System follows
// the OS via matchMedia. This is pure UI chrome (localStorage), never the IR.

import { type ReactNode, createContext, useContext, useEffect, useState } from "react";

export type ThemePref = "dark" | "light" | "system";
export type Resolved = "dark" | "light";

const KEY = "theygent.theme";

function systemResolved(): Resolved {
  try {
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  } catch {
    return "dark";
  }
}

function readPref(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    // no localStorage (tests) — fall through to the default
  }
  return "dark";
}

interface ThemeCtx {
  pref: ThemePref;
  resolved: Resolved;
  setTheme: (p: ThemePref) => void;
}

const Ctx = createContext<ThemeCtx>({ pref: "dark", resolved: "dark", setTheme: () => {} });

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [pref, setPref] = useState<ThemePref>(readPref);
  const [sys, setSys] = useState<Resolved>(systemResolved);
  const resolved: Resolved = pref === "system" ? sys : pref;

  // Follow OS appearance changes (only matters while pref === "system").
  useEffect(() => {
    let mq: MediaQueryList;
    try {
      mq = window.matchMedia("(prefers-color-scheme: light)");
    } catch {
      return;
    }
    const onChange = () => setSys(mq.matches ? "light" : "dark");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  // Apply the resolved theme to <html>: the class drives the CSS-variable overrides; color-scheme
  // tells the UA to theme native bits (scrollbars, form controls).
  useEffect(() => {
    const el = document.documentElement;
    el.classList.toggle("light", resolved === "light");
    el.classList.toggle("dark", resolved === "dark");
    el.style.colorScheme = resolved;
  }, [resolved]);

  const setTheme = (p: ThemePref) => {
    setPref(p);
    try {
      localStorage.setItem(KEY, p);
    } catch {
      // no localStorage — the in-memory state still drives this session
    }
  };

  return <Ctx.Provider value={{ pref, resolved, setTheme }}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeCtx {
  return useContext(Ctx);
}
