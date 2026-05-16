import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DashboardPage } from "../index";

/**
 * Unit-level smoke: the page mounts and surfaces a loading state while
 * onboarding state is unknown. Full UI flows (onboarding stepper visible,
 * populated state metrics) are validated end-to-end against the running
 * Docker stack — see the e2e suite. openapi-fetch captures `globalThis.fetch`
 * at construction, which makes per-test fetch mocking awkward; the loading
 * branch is the cheap test we can run here.
 */

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

describe("DashboardPage", () => {
  it("renders the loading state while onboarding query is pending", () => {
    render(wrap(<DashboardPage />));
    expect(screen.getByTestId("dashboard-loading")).toBeInTheDocument();
  });
});
