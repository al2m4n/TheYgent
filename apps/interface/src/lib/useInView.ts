import { useEffect, useRef } from "react";

// A scroll sentinel for infinite lists: attach the returned ref to an empty element at the END of a
// list, and `onInView` fires when it scrolls near the viewport (default 300px early, so the next page
// loads before the user reaches the very bottom — no "load more" button).
//
// The effect re-arms whenever `enabled` flips, which is the trick that keeps a short list filling: an
// IntersectionObserver only fires on an intersection CHANGE, so a sentinel that stays visible (a list
// shorter than the viewport) wouldn't re-trigger on its own. Driving `enabled` off `!isFetching`
// re-observes after each page lands — firing the initial callback again while the sentinel is still in
// view — so it keeps loading until the viewport is full or the data runs out, then stops.
//
// Works inside any scroll container (a page's <main> or a modal body): the observer's root is the
// viewport, and an intervening scroll container clips the sentinel — so it reports "in view" only once
// that container is scrolled far enough to actually reveal it, not merely because it geometrically
// overlaps the viewport. Where IntersectionObserver is unavailable (jsdom/tests), it's a no-op — the
// list simply shows its first page.
export function useInView(
  onInView: () => void,
  { enabled = true, rootMargin = "300px" }: { enabled?: boolean; rootMargin?: string } = {},
) {
  const ref = useRef<HTMLDivElement>(null);
  const cb = useRef(onInView);
  cb.current = onInView;

  useEffect(() => {
    if (!enabled || typeof IntersectionObserver === "undefined") return;
    const el = ref.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) cb.current();
      },
      { rootMargin },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [enabled, rootMargin]);

  return ref;
}
