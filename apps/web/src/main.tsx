import { applyStoredTheme } from "@core/layout/public/theme";
import { ThemeProvider } from "@core/layout/public/theme-context";
import { ErrorBoundary } from "@core/observability/public/error-boundary";
import { configure, recordException } from "@core/observability/public/sdk";
import { Toaster } from "@shared/components/ui/sonner";
import { MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import React from "react";
import ReactDOM from "react-dom/client";
import { router } from "./router";
import "./styles.css";

applyStoredTheme();

// Runtime axe-core accessibility scan — dev only. Reports violations to the
// browser console as they occur. Never bundled in production builds.
if (import.meta.env.DEV) {
  import("@axe-core/react").then(({ default: axe }) => {
    axe(React, ReactDOM, 1000);
  });
}

// Initialize OTel SDK. Export requires endpoint + authToken + dataset; any
// missing field falls back to NoopSpanProcessor (SDK stays active, traceparent
// still injected so the backend always gets a parent span).
// VITE_ENVIRONMENT is the deployment environment (production/staging/…) — NOT
// import.meta.env.MODE, which reflects the Vite build mode.
configure({
  collectorEndpoint: import.meta.env.VITE_OTEL_COLLECTOR_ENDPOINT as string | undefined,
  authToken: import.meta.env.VITE_DASH0_AUTH_TOKEN as string | undefined,
  dataset: import.meta.env.VITE_DASH0_DATASET as string | undefined,
  serviceVersion: import.meta.env.VITE_SERVICE_VERSION as string | undefined,
  environmentName: import.meta.env.VITE_ENVIRONMENT as string | undefined,
});

const queryClient = new QueryClient({
  // Route all unhandled mutation errors to OTel as span exceptions so they
  // appear in Dash0 traces alongside request spans.
  mutationCache: new MutationCache({
    onError: (error) => {
      recordException(error);
    },
  }),
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("Could not find #root element");

ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <ErrorBoundary>
      <ThemeProvider>
        <QueryClientProvider client={queryClient}>
          <RouterProvider router={router} />
          <Toaster position="bottom-right" />
        </QueryClientProvider>
      </ThemeProvider>
    </ErrorBoundary>
  </React.StrictMode>,
);
