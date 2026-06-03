import { ThemeProvider, applyStoredTheme } from "@core/layout";
import { ErrorBoundary, configure } from "@core/observability";
import { Toaster } from "@shared/components/ui/sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
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

// Initialize OTel SDK. Export is gated on the collector endpoint:
// endpoint set → export via OTLP/HTTP; endpoint absent → no export.
configure({
  collectorEndpoint: import.meta.env.VITE_OTEL_COLLECTOR_ENDPOINT as string | undefined,
});

const queryClient = new QueryClient({
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
