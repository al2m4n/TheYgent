import "@testing-library/jest-dom/vitest";

// jsdom has no ResizeObserver; the canvas renderer requires one to mount.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// jsdom implements neither half of the object-URL API. Every media surface mints one to play or
// show bytes it already holds (an artifact reply, a staged clip), so without these a modality test
// dies on an unrelated TypeError instead of asserting what it came to assert.
if (typeof URL.createObjectURL === "undefined") {
  let n = 0;
  URL.createObjectURL = () => `blob:test/${++n}`;
  URL.revokeObjectURL = () => {};
}
