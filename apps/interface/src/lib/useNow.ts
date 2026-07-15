// A ticking "now" for rolling time windows. A relative range ("last 5 minutes") is resolved against
// the current time, but nothing re-renders a list purely because wall-clock time moved — the memo
// that filters would freeze `now` at whatever it was when the data last changed, so the window stops
// rolling and drifts from its own label. This hook re-renders on an interval WHILE a relative window
// is active (and refreshes immediately on activation), so the filter keeps up. When inactive it stops
// ticking — "all time" and absolute windows don't depend on now.

import { useEffect, useState } from "react";

export function useNow(active: boolean, intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [active, intervalMs]);
  return now;
}
