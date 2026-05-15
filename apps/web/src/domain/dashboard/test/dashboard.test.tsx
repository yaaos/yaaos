import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DashboardPage } from "../page";

/**
 * The dashboard renders Hello World plus the /api/health response.
 *
 * Asserting on the rendered API response is intentionally NOT a unit-test
 * concern — openapi-fetch captures `globalThis.fetch` at construction time,
 * which makes per-test mocking awkward. The full FE→BE wiring is validated
 * end-to-end via the running Docker stack (see README) and by the backend's
 * own `test_health.py` integration test.
 */

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

describe("DashboardPage", () => {
  it("renders Hello World", () => {
    render(wrap(<DashboardPage />));
    expect(screen.getByText("Hello World")).toBeInTheDocument();
  });

  it("includes the /api/health card heading", () => {
    render(wrap(<DashboardPage />));
    expect(screen.getByText("/api/health")).toBeInTheDocument();
  });
});
