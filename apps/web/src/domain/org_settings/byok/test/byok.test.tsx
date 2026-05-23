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

  it("configured: shows summary + Test/Rotate/Clear (input is hidden until Rotate)", () => {
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
    expect(screen.getByTestId("byok-configured-summary-anthropic")).toHaveTextContent(/last set/i);
    expect(screen.queryByTestId("byok-input-anthropic")).toBeNull();
    expect(screen.getByTestId("byok-test-anthropic")).toBeInTheDocument();
    expect(screen.getByTestId("byok-rotate-anthropic")).toBeInTheDocument();
    expect(screen.getByTestId("byok-clear-anthropic")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("byok-test-anthropic"));
    expect(validateMutate).toHaveBeenCalledWith("anthropic");
    fireEvent.click(screen.getByTestId("byok-clear-anthropic"));
    expect(clearMutate).toHaveBeenCalledWith("anthropic");
    // Timestamps block is present.
    expect(screen.getByTestId("byok-timestamps-anthropic")).toBeInTheDocument();
  });

  it("configured + Rotate: clicking Rotate reveals input; Cancel hides it again", () => {
    providersMock.mockReturnValue({
      data: [
        {
          provider: "anthropic",
          status: "configured",
          last_validated_at: null,
          last_used_at: null,
          updated_at: "2026-05-20T00:00:00Z",
        },
      ],
      isLoading: false,
    });
    render(<BYOKSettingsPage />);
    fireEvent.click(screen.getByTestId("byok-rotate-anthropic"));
    const input = screen.getByTestId("byok-input-anthropic") as HTMLInputElement;
    expect(input.type).toBe("password");
    fireEvent.click(screen.getByTestId("byok-rotate-cancel-anthropic"));
    expect(screen.queryByTestId("byok-input-anthropic")).toBeNull();
  });

  it("empty provider list shows empty message", () => {
    providersMock.mockReturnValue({ data: [], isLoading: false });
    render(<BYOKSettingsPage />);
    expect(screen.getByTestId("byok-empty")).toBeInTheDocument();
  });

  it("not_set: input is always type=password (no reveal toggle)", () => {
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
    expect(screen.queryByTestId("byok-reveal-anthropic")).toBeNull();
  });
});
