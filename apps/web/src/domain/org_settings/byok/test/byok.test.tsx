import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const providersMock = vi.fn();
const setMutate = vi.fn();
const validateMutate = vi.fn();
const clearMutate = vi.fn();

vi.mock("../queries", () => ({
  useByokProviders: () => providersMock(),
  useSetByok: () => ({ mutate: setMutate, isPending: false, isError: false, error: null }),
  useValidateByok: () => ({
    mutate: validateMutate,
    isPending: false,
    data: undefined,
    variables: undefined,
  }),
  useClearByok: () => ({ mutate: clearMutate, isPending: false }),
}));

vi.mock("@core/api", () => ({ getCurrentOrgSlug: () => "acme" }));
vi.mock("@domain/auth", () => ({
  useCurrentUser: () => ({
    data: {
      orgs: [{ slug: "acme", role: "owner", handle: "j", display_name: "Acme" }],
      current_org_slug: "acme",
      user: { id: "u", display_name: "u", primary_email: "u@x", emails: [] },
    },
  }),
}));

import { BYOKSettingsPage } from "../BYOKSettingsPage";

describe("BYOKSettingsPage", () => {
  beforeEach(() => {
    providersMock.mockReset();
    setMutate.mockReset();
    validateMutate.mockReset();
    clearMutate.mockReset();
  });

  it("not_set: shows status badge + Save (no Test/Remove until configured)", () => {
    providersMock.mockReturnValue({
      data: [
        {
          provider: "anthropic",
          status: "not_set",
          last_validated_at: null,
          last_used_at: null,
          updated_at: null,
        },
      ],
      isLoading: false,
    });
    render(<BYOKSettingsPage />);
    expect(screen.getByTestId("byok-card-anthropic")).toBeInTheDocument();
    expect(screen.getByTestId("byok-status-anthropic")).toHaveTextContent(/not set/i);
    expect(screen.getByTestId("byok-save-anthropic")).toBeDisabled(); // empty value
    expect(screen.queryByTestId("byok-test-anthropic")).toBeNull();
    expect(screen.queryByTestId("byok-clear-anthropic")).toBeNull();
  });

  it("typing enables Save; Save fires the mutation with provider+value", () => {
    providersMock.mockReturnValue({
      data: [
        {
          provider: "anthropic",
          status: "not_set",
          last_validated_at: null,
          last_used_at: null,
          updated_at: null,
        },
      ],
      isLoading: false,
    });
    render(<BYOKSettingsPage />);
    const input = screen.getByTestId("byok-input-anthropic");
    fireEvent.change(input, { target: { value: "sk-ant-test" } });
    fireEvent.click(screen.getByTestId("byok-save-anthropic"));
    expect(setMutate).toHaveBeenCalledTimes(1);
    const call = setMutate.mock.calls[0];
    if (!call) throw new Error("no call");
    expect(call[0]).toEqual({ provider: "anthropic", value: "sk-ant-test" });
  });

  it("configured: Test + Remove + timestamps render", () => {
    providersMock.mockReturnValue({
      data: [
        {
          provider: "anthropic",
          status: "configured",
          last_validated_at: "2026-05-20T01:00:00Z",
          last_used_at: "2026-05-20T02:00:00Z",
          updated_at: "2026-05-20T00:00:00Z",
        },
      ],
      isLoading: false,
    });
    render(<BYOKSettingsPage />);
    expect(screen.getByTestId("byok-status-anthropic")).toHaveTextContent(/configured/i);
    expect(screen.getByTestId("byok-test-anthropic")).toBeInTheDocument();
    expect(screen.getByTestId("byok-clear-anthropic")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("byok-test-anthropic"));
    expect(validateMutate).toHaveBeenCalledWith("anthropic");
    fireEvent.click(screen.getByTestId("byok-clear-anthropic"));
    expect(clearMutate).toHaveBeenCalledWith("anthropic");
    // Timestamps block is present.
    expect(screen.getByTestId("byok-timestamps-anthropic")).toBeInTheDocument();
  });

  it("empty provider list shows empty message", () => {
    providersMock.mockReturnValue({ data: [], isLoading: false });
    render(<BYOKSettingsPage />);
    expect(screen.getByTestId("byok-empty")).toBeInTheDocument();
  });

  it("reveal toggle flips the input type", () => {
    providersMock.mockReturnValue({
      data: [
        {
          provider: "anthropic",
          status: "not_set",
          last_validated_at: null,
          last_used_at: null,
          updated_at: null,
        },
      ],
      isLoading: false,
    });
    render(<BYOKSettingsPage />);
    const input = screen.getByTestId("byok-input-anthropic") as HTMLInputElement;
    expect(input.type).toBe("password");
    fireEvent.click(screen.getByTestId("byok-reveal-anthropic"));
    expect(input.type).toBe("text");
  });
});
