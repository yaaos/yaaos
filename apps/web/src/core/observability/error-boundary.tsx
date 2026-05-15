import React from "react";

type Props = { children: React.ReactNode };
type State = { error: Error | null };

export class ErrorBoundary extends React.Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: React.ErrorInfo): void {
    // Structured client-side log. Real telemetry pipe lands when needed.
    console.error("yaaof.client.error", { error, componentStack: info.componentStack });
  }

  override render(): React.ReactNode {
    if (this.state.error) {
      return (
        <div className="p-8 text-text">
          <h1 className="mb-2 text-lg font-semibold">Something went wrong.</h1>
          <p className="text-text-3 text-sm">{this.state.error.message}</p>
        </div>
      );
    }
    return this.props.children;
  }
}
