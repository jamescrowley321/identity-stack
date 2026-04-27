import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

type ChangeHandler = () => void;

interface MockMediaQueryList {
  matches: boolean;
  media: string;
  onchange: null;
  addListener: () => void;
  removeListener: () => void;
  addEventListener: (event: string, handler: ChangeHandler) => void;
  removeEventListener: (event: string, handler: ChangeHandler) => void;
  dispatchEvent: () => boolean;
}

const changeHandlers: ChangeHandler[] = [];
let mockWidth = 1280;

function createMockMQL(query: string): MockMediaQueryList {
  return {
    get matches() {
      if (query.includes("max-width: 767px")) {
        return mockWidth < 768;
      }
      if (query.includes("min-width: 768px") && query.includes("max-width: 1023px")) {
        return mockWidth >= 768 && mockWidth < 1024;
      }
      return false;
    },
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: (_event: string, handler: ChangeHandler) => {
      changeHandlers.push(handler);
    },
    removeEventListener: (_event: string, handler: ChangeHandler) => {
      const idx = changeHandlers.indexOf(handler);
      if (idx >= 0) changeHandlers.splice(idx, 1);
    },
    dispatchEvent: () => false,
  };
}

beforeEach(() => {
  mockWidth = 1280;
  changeHandlers.length = 0;
  vi.stubGlobal("matchMedia", (query: string) => createMockMQL(query));
  Object.defineProperty(window, "innerWidth", {
    writable: true,
    configurable: true,
    value: mockWidth,
  });
});

function setWidth(w: number) {
  mockWidth = w;
  Object.defineProperty(window, "innerWidth", {
    writable: true,
    configurable: true,
    value: w,
  });
}

describe("useBreakpoint", () => {
  it("returns 'desktop' above 1024px", async () => {
    setWidth(1280);
    vi.resetModules();
    const { useBreakpoint } = await import("../use-mobile");
    const { result } = renderHook(() => useBreakpoint());
    expect(result.current).toBe("desktop");
  });

  it("returns 'tablet' between 768-1024px", async () => {
    setWidth(900);
    vi.resetModules();
    const { useBreakpoint } = await import("../use-mobile");
    const { result } = renderHook(() => useBreakpoint());
    expect(result.current).toBe("tablet");
  });

  it("returns 'mobile' below 768px", async () => {
    setWidth(500);
    vi.resetModules();
    const { useBreakpoint } = await import("../use-mobile");
    const { result } = renderHook(() => useBreakpoint());
    expect(result.current).toBe("mobile");
  });

  it("returns 'tablet' at exactly 768px", async () => {
    setWidth(768);
    vi.resetModules();
    const { useBreakpoint } = await import("../use-mobile");
    const { result } = renderHook(() => useBreakpoint());
    expect(result.current).toBe("tablet");
  });

  it("returns 'desktop' at exactly 1024px", async () => {
    setWidth(1024);
    vi.resetModules();
    const { useBreakpoint } = await import("../use-mobile");
    const { result } = renderHook(() => useBreakpoint());
    expect(result.current).toBe("desktop");
  });

  it("updates when matchMedia listeners fire", async () => {
    setWidth(1280);
    vi.resetModules();
    const { useBreakpoint } = await import("../use-mobile");
    const { result } = renderHook(() => useBreakpoint());
    expect(result.current).toBe("desktop");

    act(() => {
      setWidth(500);
      changeHandlers.forEach((h) => h());
    });
    expect(result.current).toBe("mobile");
  });
});

describe("useIsMobile", () => {
  it("returns false above 768px", async () => {
    setWidth(1280);
    vi.resetModules();
    const { useIsMobile } = await import("../use-mobile");
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);
  });

  it("returns true below 768px", async () => {
    setWidth(500);
    vi.resetModules();
    const { useIsMobile } = await import("../use-mobile");
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(true);
  });

  it("returns false at tablet width (backwards compatible)", async () => {
    setWidth(900);
    vi.resetModules();
    const { useIsMobile } = await import("../use-mobile");
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);
  });
});
