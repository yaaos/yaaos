/** Theme + sidebar-pin user preferences. localStorage-only in M01 (no auth). */

export type Theme = "light" | "dark";

const THEME_KEY = "yaaof:theme";
const SIDEBAR_KEY = "yaaof:sidebar-pinned";

export function getStoredTheme(): Theme | null {
  const v = localStorage.getItem(THEME_KEY);
  return v === "light" || v === "dark" ? v : null;
}

export function setStoredTheme(theme: Theme): void {
  localStorage.setItem(THEME_KEY, theme);
  document.documentElement.setAttribute("data-theme", theme);
}

/** Run once at boot from main.tsx. Honors stored value, else OS preference. */
export function applyStoredTheme(): void {
  const stored = getStoredTheme();
  if (stored) {
    document.documentElement.setAttribute("data-theme", stored);
    return;
  }
  const prefersLight = window.matchMedia("(prefers-color-scheme: light)").matches;
  document.documentElement.setAttribute("data-theme", prefersLight ? "light" : "dark");
}

export function toggleTheme(): Theme {
  const current =
    document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  const next: Theme = current === "light" ? "dark" : "light";
  setStoredTheme(next);
  return next;
}

export function getSidebarPinned(): boolean {
  const v = localStorage.getItem(SIDEBAR_KEY);
  if (v === null) return true; // default: pinned
  return v === "1";
}

export function setSidebarPinned(pinned: boolean): void {
  localStorage.setItem(SIDEBAR_KEY, pinned ? "1" : "0");
}
