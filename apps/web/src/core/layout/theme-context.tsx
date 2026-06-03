import type React from "react";
import { createContext, useContext, useState } from "react";
import { type Theme, applyStoredTheme, getStoredTheme, setStoredTheme } from "./theme";

interface ThemeContextValue {
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(() => {
    // Resolve initial theme: stored value > OS preference > dark.
    // applyStoredTheme() sets the data-theme attribute at the same time.
    applyStoredTheme();
    return (
      getStoredTheme() ??
      (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")
    );
  });

  function setTheme(next: Theme) {
    setStoredTheme(next);
    setThemeState(next);
  }

  return <ThemeContext.Provider value={{ theme, setTheme }}>{children}</ThemeContext.Provider>;
}

export function useThemeContext(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useThemeContext must be used inside ThemeProvider");
  return ctx;
}
