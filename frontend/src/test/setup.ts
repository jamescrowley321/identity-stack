import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

// The first render of the whole shell in a file pays for loading its modules,
// which under a full parallel run can outlast the 1s default of findBy/waitFor.
configure({ asyncUtilTimeout: 5000 });

// Mock window.matchMedia for jsdom (used by shadcn sidebar's use-mobile hook)
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});
