import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { describe, expect, it } from "vitest";
import { server } from "../../../test/msw/server";
import { LoginPage } from "../LoginPage";

/**
 * Smoke tests for the Login page. Uses MSW to intercept:
 *   - GET /api/auth/providers — controls which provider buttons render.
 *   - POST /api/sso/discover — controls the SSO discovery flow.
 *
 * Asserts:
 *   - the top-level "Sign in with GitHub" button renders when github is configured.
 *   - the email-first SAML discovery flow renders the discovered button.
 *   - the test stub provider surfaces in the "Other" section.
 */

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

describe("LoginPage (MSW)", () => {
  it("renders a top-level Sign in with GitHub button without typing an email", async () => {
    server.use(
      http.get("/api/auth/providers", () => HttpResponse.json({ providers: ["github", "test"] })),
    );
    render(wrap(<LoginPage />));
    await waitFor(() => expect(screen.getByTestId("login-github")).toBeInTheDocument());
    expect(screen.getByTestId("login-email")).toBeInTheDocument();
    expect(screen.getByTestId("login-continue")).toBeInTheDocument();
    expect(screen.getByTestId("login-test")).toBeInTheDocument();
  });

  it("Continue fires the SSO discover mutation and surfaces the SAML button", async () => {
    server.use(
      http.get("/api/auth/providers", () => HttpResponse.json({ providers: ["github"] })),
      http.get("/api/sso/discover", () =>
        HttpResponse.json({
          provider: "saml" as const,
          saml_idp_name: "Okta",
          saml_org_slug: "acme",
        }),
      ),
    );
    render(wrap(<LoginPage />));
    await waitFor(() => expect(screen.getByTestId("login-github")).toBeInTheDocument());

    await act(async () => {
      fireEvent.change(screen.getByTestId("login-email"), {
        target: { value: "alice@example.com" },
      });
      fireEvent.click(screen.getByTestId("login-continue"));
    });

    await waitFor(() => {
      expect(screen.getByTestId("login-discovered-saml")).toBeInTheDocument();
    });
  });

  it("no providers configured shows the fallback message", async () => {
    server.use(http.get("/api/auth/providers", () => HttpResponse.json({ providers: [] })));
    render(wrap(<LoginPage />));
    await waitFor(() =>
      expect(screen.getByText(/No identity providers configured/i)).toBeInTheDocument(),
    );
  });
});
