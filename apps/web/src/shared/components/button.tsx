import { cn } from "@shared/utils/cn";
import type { ButtonHTMLAttributes } from "react";

type Variant = "default" | "ghost" | "primary";

export function Button({
  className,
  variant = "default",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  const styles: Record<Variant, string> = {
    default: "bg-surface-2 text-text border border-border hover:bg-hover",
    ghost: "bg-transparent text-text-2 hover:bg-hover",
    primary: "bg-accent text-white hover:bg-accent-2 border border-accent-border",
  };
  return (
    <button
      type="button"
      className={cn(
        "inline-flex items-center gap-1.5 rounded h-[28px] px-3 text-[12.5px] font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed",
        styles[variant],
        className,
      )}
      {...props}
    />
  );
}
