import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

/**
 * Smoke tests for the Login page. Mocks `useSsoDiscover` + `useProviders`
 * and asserts:
 *   - the top-level "Sign in with GitHub" button is rendered whenever the
 *     github provider is configured — no email required.
 *   - the email-first SAML discovery flow still works for enterprise orgs.
 *   - the test stub provider surfaces in the "Other" section.
 */

const discoverMutate = vi.fn();
type DiscoverResult = {
  provider: "github" | "saml";
  saml_idp_name?: string;
  saml_org_slug?: string;
};
let discoverDataMock: DiscoverResult | undefined = undefined;

vi.mock("@core/api", () => ({
  useSsoDiscover: () => ({
    mutate: discoverMutate,
    isPending: false,
    data: discoverDataMock,
  }),
}));

vi.mock("../queries", () => ({
  useProviders: () => ({
    data: { providers: ["github", "test"] },
    isLoading: false,
  }),
}));

import { LoginPage } from "../LoginPage";

describe("LoginPage", () => {
  beforeEach(() => {
    discoverMutate.mockReset();
    discoverDataMock = undefined;
  });

  it("renders a top-level Sign in with GitHub button without typing an email", () => {
    render(<LoginPage />);
    expect(screen.getByTestId("login-github")).toBeInTheDocument();
    expect(screen.getByTestId("login-email")).toBeInTheDocument();
    expect(screen.getByTestId("login-continue")).toBeInTheDocument();
    // Test stub surfaces in the "Other" section for non-prod parity.
    expect(screen.getByTestId("login-test")).toBeInTheDocument();
  });

  it("Continue fires useSsoDiscover.mutate with the typed email", () => {
    render(<LoginPage />);
    fireEvent.change(screen.getByTestId("login-email"), {
      target: { value: "alice@example.com" },
    });
    fireEvent.click(screen.getByTestId("login-continue"));
    expect(discoverMutate).toHaveBeenCalledWith("alice@example.com");
  });

  it("saml discovery result surfaces the discovered-saml button", () => {
    discoverDataMock = { provider: "saml", saml_idp_name: "Okta", saml_org_slug: "acme" };
    render(<LoginPage />);
    expect(screen.getByTestId("login-discovered-saml")).toBeInTheDocument();
  });
});

// `beforeEach` is hoisted by Vitest when this module is run; declare for TS.
declare const beforeEach: (fn: () => void) => void;
