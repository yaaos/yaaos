import { applyStoredTheme } from "@core/layout/theme";
import { ErrorBoundary } from "@core/observability/error-boundary";
import { router } from "@core/routing/router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import React from "react";
import ReactDOM from "react-dom/client";
import { Toaster } from "sonner";
import "./styles.css";

applyStoredTheme();

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
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
        <Toaster theme="system" position="bottom-right" />
      </QueryClientProvider>
    </ErrorBoundary>
  </React.StrictMode>,
);
