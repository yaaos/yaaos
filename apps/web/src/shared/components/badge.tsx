import { cn } from "@shared/utils/cn";
import type { HTMLAttributes } from "react";

type Variant = "default" | "success" | "danger" | "accent" | "soft";

export function Badge({
  className,
  variant = "default",
  ...props
}: HTMLAttributes<HTMLSpanElement> & { variant?: Variant }) {
  const styles: Record<Variant, string> = {
    default: "bg-surface-2 text-text-2 border border-border",
    success: "bg-success/15 text-success border border-success/30",
    danger: "bg-danger/15 text-danger border border-danger/30",
    accent: "bg-accent-bg text-accent border border-accent-border",
    soft: "bg-surface-3 text-text-3 border border-border-soft",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-pill px-2 py-0.5 text-[10.5px] font-medium uppercase tracking-wider",
        styles[variant],
        className,
      )}
      {...props}
    />
  );
}
