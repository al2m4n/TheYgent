import "@testing-library/jest-dom/vitest";

// jsdom has no ResizeObserver; the canvas renderer requires one to mount.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
