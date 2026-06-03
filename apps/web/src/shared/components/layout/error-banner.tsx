/**
 * Error banner — in-page error that doesn't break the page.
 *
 * Spec from requirements.md § C2: sticky banner at the top of the affected
 * section. "Couldn't load X. [Retry]". Voice (D3): blame the system, not
 * the user; "Couldn't …" not "You did something wrong".
 */

import { cn } from "@shared/utils";
import { AlertCircle, RefreshCw } from "lucide-react";

interface ErrorBannerProps {
  message: string;
  onRetry?: () => void;
  className?: string;
}

export function ErrorBanner({ message, onRetry, className }: ErrorBannerProps) {
  return (
    <div
      role="alert"
      className={cn(
        "flex items-center gap-3 px-3 py-2 rounded border border-destructive/30 bg-destructive/10 text-sm",
        className,
      )}
    >
      <AlertCircle className="w-4 h-4 text-destructive shrink-0" aria-hidden="true" />
      <span className="flex-1 text-foreground">{message}</span>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex items-center gap-1.5 text-xs font-medium text-foreground hover:text-destructive transition-colors"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Retry
        </button>
      )}
    </div>
  );
}
