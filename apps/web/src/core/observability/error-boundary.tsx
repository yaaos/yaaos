/**
 * Root error boundary for the yaaos SPA.
 *
 * Wraps the application tree; any unhandled render error calls recordException
 * on the active OTel span (or opens a short-lived span) and renders a
 * user-facing fallback. Uses react-error-boundary as the implementation so
 * the component itself stays a simple function wrapper.
 */

import type React from "react";
import { type FallbackProps, ErrorBoundary as ReactErrorBoundary } from "react-error-boundary";
import { recordException } from "./sdk";

function FallbackRender({ error }: FallbackProps): React.ReactElement {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="p-8 text-foreground">
      <h1 className="mb-2 text-lg font-semibold">Something went wrong.</h1>
      <p className="text-muted-foreground text-sm">{message}</p>
    </div>
  );
}

function handleError(error: unknown): void {
  recordException(error);
}

type Props = { children: React.ReactNode };

export function ErrorBoundary({ children }: Props): React.ReactElement {
  return (
    <ReactErrorBoundary FallbackComponent={FallbackRender} onError={handleError}>
      {children}
    </ReactErrorBoundary>
  );
}
