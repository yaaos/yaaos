import type { Config } from "tailwindcss";

/**
 * Tailwind config — design tokens ported from plan/design/app/yaaof.css.
 * oklch values preserved; light + dark themes live as CSS variables, this
 * file maps them onto Tailwind utilities.
 */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        "bg-2": "var(--bg-2)",
        surface: "var(--surface)",
        "surface-2": "var(--surface-2)",
        "surface-3": "var(--surface-3)",
        hover: "var(--hover)",
        border: "var(--border)",
        "border-soft": "var(--border-soft)",
        "border-hard": "var(--border-hard)",
        text: "var(--text)",
        "text-2": "var(--text-2)",
        "text-3": "var(--text-3)",
        "text-4": "var(--text-4)",
        accent: "var(--accent)",
        "accent-2": "var(--accent-2)",
        "accent-dim": "var(--accent-dim)",
        "accent-bg": "var(--accent-bg)",
        "accent-border": "var(--accent-border)",
        success: "var(--success)",
        danger: "var(--danger)",
        warning: "var(--warning)",
        info: "var(--info)",
      },
      fontFamily: {
        sans: ['"Geist"', "system-ui", "sans-serif"],
        mono: ['"Geist Mono"', "ui-monospace", "monospace"],
      },
      borderRadius: {
        DEFAULT: "6px",
        card: "10px",
        chip: "4px",
        pill: "999px",
      },
      boxShadow: {
        sm: "0 1px 2px oklch(0 0 0 / 0.35)",
        DEFAULT: "0 6px 24px oklch(0 0 0 / 0.36), 0 1px 2px oklch(0 0 0 / 0.22)",
        lg: "0 20px 60px oklch(0 0 0 / 0.5)",
        glow: "0 0 0 1px var(--accent-border), 0 0 24px oklch(0.50 0.16 295 / 0.35)",
      },
    },
  },
  plugins: [],
} satisfies Config;
