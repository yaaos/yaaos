import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { describe, expect, it } from "vitest";
import { server } from "../../../../test/msw/server";
import { BYOKSettingsPage } from "../BYOKSettingsPage";

/**
 * Tests for BYOKSettingsPage via MSW — exercises the not_set / configured
 * / rotate states and the save / test / clear action flows.
 */

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

const NOT_SET = {
  provider: "anthropic",
  status: "not_set",
  last_validated_at: null,
  last_used_at: null,
  updated_at: null,
};

const CONFIGURED = {
  provider: "anthropic",
  status: "configured",
  last_validated_at: "2026-05-20T01:00:00Z",
  last_used_at: "2026-05-20T02:00:00Z",
  updated_at: "2026-05-20T00:00:00Z",
};

describe("BYOKSettingsPage (MSW)", () => {
  it("not_set: shows status badge + Save (no Test/Remove until configured)", async () => {
    server.use(http.get("/api/api-keys", () => HttpResponse.json([NOT_SET])));
    render(wrap(<BYOKSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("byok-card-anthropic")).toBeInTheDocument());
    expect(screen.getByTestId("byok-status-anthropic")).toHaveTextContent(/not set/i);
    expect(screen.getByTestId("byok-save-anthropic")).toBeDisabled();
    expect(screen.queryByTestId("byok-test-anthropic")).toBeNull();
    expect(screen.queryByTestId("byok-clear-anthropic")).toBeNull();
  });

  it("typing enables Save; Save fires the mutation with provider+value", async () => {
    let savedBody: unknown = null;
    server.use(
      http.get("/api/api-keys", () => HttpResponse.json([NOT_SET])),
      http.post("/api/api-keys/:provider", async ({ request }) => {
        savedBody = await request.json();
        return HttpResponse.json({ status: "ok" });
      }),
    );
    render(wrap(<BYOKSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("byok-input-anthropic")).toBeInTheDocument());
    const input = screen.getByTestId("byok-input-anthropic");
    fireEvent.change(input, { target: { value: "sk-ant-test" } });
    fireEvent.click(screen.getByTestId("byok-save-anthropic"));
    await waitFor(() => expect(savedBody).toEqual({ value: "sk-ant-test" }));
  });

  it("configured: shows summary + Test/Rotate/Clear (input is hidden until Rotate)", async () => {
    let validateCalled = false;
    let clearCalled = false;
    server.use(
      http.get("/api/api-keys", () => HttpResponse.json([CONFIGURED])),
      http.post("/api/api-keys/:provider/validate", () => {
        validateCalled = true;
        return HttpResponse.json({ valid: true });
      }),
      http.delete("/api/api-keys/:provider", () => {
        clearCalled = true;
        return HttpResponse.json({ removed: true });
      }),
    );
    render(wrap(<BYOKSettingsPage />));
    await waitFor(() =>
      expect(screen.getByTestId("byok-status-anthropic")).toHaveTextContent(/configured/i),
    );
    expect(screen.getByTestId("byok-configured-summary-anthropic")).toHaveTextContent(/last set/i);
    expect(screen.queryByTestId("byok-input-anthropic")).toBeNull();
    expect(screen.getByTestId("byok-test-anthropic")).toBeInTheDocument();
    expect(screen.getByTestId("byok-rotate-anthropic")).toBeInTheDocument();
    expect(screen.getByTestId("byok-clear-anthropic")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("byok-test-anthropic"));
    await waitFor(() => expect(validateCalled).toBe(true));

    fireEvent.click(screen.getByTestId("byok-clear-anthropic"));
    await waitFor(() => expect(clearCalled).toBe(true));

    expect(screen.getByTestId("byok-timestamps-anthropic")).toBeInTheDocument();
  });

  it("configured + Rotate: clicking Rotate reveals input; Cancel hides it again", async () => {
    server.use(http.get("/api/api-keys", () => HttpResponse.json([CONFIGURED])));
    render(wrap(<BYOKSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("byok-rotate-anthropic")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("byok-rotate-anthropic"));
    const input = screen.getByTestId("byok-input-anthropic") as HTMLInputElement;
    expect(input.type).toBe("password");
    fireEvent.click(screen.getByTestId("byok-rotate-cancel-anthropic"));
    expect(screen.queryByTestId("byok-input-anthropic")).toBeNull();
  });

  it("empty provider list shows empty message", async () => {
    server.use(http.get("/api/api-keys", () => HttpResponse.json([])));
    render(wrap(<BYOKSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("byok-empty")).toBeInTheDocument());
  });

  it("not_set: input is always type=password (no reveal toggle)", async () => {
    server.use(http.get("/api/api-keys", () => HttpResponse.json([NOT_SET])));
    render(wrap(<BYOKSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("byok-input-anthropic")).toBeInTheDocument());
    const input = screen.getByTestId("byok-input-anthropic") as HTMLInputElement;
    expect(input.type).toBe("password");
    expect(screen.queryByTestId("byok-reveal-anthropic")).toBeNull();
  });
});
