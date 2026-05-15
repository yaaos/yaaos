import { Moon, Sun } from "lucide-react";
import { useState } from "react";
import { toggleTheme } from "./theme";

export function Topbar({ crumb }: { crumb: string }) {
  const [theme, setTheme] = useState<"light" | "dark">(() =>
    document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark",
  );

  const onToggle = () => {
    const next = toggleTheme();
    setTheme(next);
  };

  return (
    <header className="flex items-center gap-3 h-[44px] border-b border-border-soft bg-bg-2 px-4 shrink-0">
      <div className="mono text-text-2 text-[12px]">{crumb}</div>
      <div className="flex-1" />
      <button
        type="button"
        onClick={onToggle}
        className="rounded p-1.5 text-text-3 hover:bg-hover hover:text-text"
        title={theme === "dark" ? "Switch to light" : "Switch to dark"}
      >
        {theme === "dark" ? <Sun className="w-3.5 h-3.5" /> : <Moon className="w-3.5 h-3.5" />}
      </button>
      <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-pill bg-success/15 text-success text-[10.5px] font-medium">
        <span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />
        live
      </span>
    </header>
  );
}
