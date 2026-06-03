import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider, useThemeContext } from "../theme-context";

// Minimal localStorage mock
const store: Record<string, string> = {};
const localStorageMock = {
  getItem: (key: string) => store[key] ?? null,
  setItem: (key: string, value: string) => {
    store[key] = value;
  },
  removeItem: (key: string) => {
    delete store[key];
  },
  clear: () => {
    for (const k of Object.keys(store)) delete store[k];
  },
};

beforeEach(() => {
  localStorageMock.clear();
  Object.defineProperty(window, "localStorage", {
    value: localStorageMock,
    writable: true,
  });
  // Reset html data-theme
  document.documentElement.removeAttribute("data-theme");
  // Stub matchMedia (jsdom doesn't implement it)
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockReturnValue({ matches: false }),
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ThemeProvider / useThemeContext", () => {
  it("defaults to stored theme when present", () => {
    store["yaaos:theme"] = "light";
    const { result } = renderHook(() => useThemeContext(), {
      wrapper: ThemeProvider,
    });
    expect(result.current.theme).toBe("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("defaults to dark when no stored value and OS prefers dark", () => {
    const { result } = renderHook(() => useThemeContext(), {
      wrapper: ThemeProvider,
    });
    expect(result.current.theme).toBe("dark");
  });

  it("setTheme writes through to localStorage and data-theme", () => {
    const { result } = renderHook(() => useThemeContext(), {
      wrapper: ThemeProvider,
    });
    act(() => {
      result.current.setTheme("light");
    });
    expect(result.current.theme).toBe("light");
    expect(store["yaaos:theme"]).toBe("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("setTheme to dark updates all three sinks", () => {
    store["yaaos:theme"] = "light";
    document.documentElement.setAttribute("data-theme", "light");
    const { result } = renderHook(() => useThemeContext(), {
      wrapper: ThemeProvider,
    });
    act(() => {
      result.current.setTheme("dark");
    });
    expect(result.current.theme).toBe("dark");
    expect(store["yaaos:theme"]).toBe("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("provides a non-system theme — regression guard for sonner drift bug", () => {
    // The sonner component reads theme from context and must never see "system"
    // (which was the stuck value when next-themes provider was not mounted).
    store["yaaos:theme"] = "dark";
    const { result } = renderHook(() => useThemeContext(), {
      wrapper: ThemeProvider,
    });
    expect(result.current.theme).not.toBe("system");
    expect(["light", "dark"]).toContain(result.current.theme);
  });
});
