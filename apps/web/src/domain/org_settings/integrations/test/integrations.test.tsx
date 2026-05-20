import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const integrationsMock = vi.fn();
const patchMutate = vi.fn();
const deleteMutate = vi.fn();
const validateMutate = vi.fn();

vi.mock("../queries", () => ({
  useIntegrations: () => integrationsMock(),
  usePatchIntegration: () => ({
    mutate: patchMutate,
    isPending: false,
    isError: false,
    error: null,
  }),
  useDeleteIntegration: () => ({ mutate: deleteMutate, isPending: false }),
  useValidateIntegration: () => ({
    mutate: validateMutate,
    isPending: false,
    isSuccess: false,
    data: undefined,
  }),
}));

vi.mock("@core/api", () => ({
  getCurrentOrgSlug: () => "acme",
}));

vi.mock("@domain/auth", () => ({
  useCurrentUser: () => ({
    data: {
      orgs: [
        {
          slug: "acme",
          role: "owner",
          handle: "j",
          display_name: "Acme",
          broken_integrations: [],
        },
      ],
      current_org_slug: "acme",
      user: { id: "u", display_name: "u", primary_email: "u@x", emails: [] },
    },
  }),
}));

import { IntegrationsSettingsPage } from "../IntegrationsSettingsPage";

describe("IntegrationsSettingsPage", () => {
  beforeEach(() => {
    integrationsMock.mockReset();
    patchMutate.mockReset();
    deleteMutate.mockReset();
    validateMutate.mockReset();
  });

  it("renders not_set provider with Connect button", () => {
    integrationsMock.mockReturnValue({
      data: [
        {
          provider: "linear",
          status: "not_set",
          enabled: null,
          upstream_identity: null,
          last_validated_at: null,
          last_refresh_failed_at: null,
          allowed_tools: [],
        },
      ],
      isLoading: false,
    });
    render(<IntegrationsSettingsPage />);
    expect(screen.getByTestId("badge-linear-disconnected")).toBeTruthy();
    const link = screen.getByTestId("connect-linear") as HTMLAnchorElement;
    expect(link.href).toContain("/api/integrations/linear/connect");
  });

  it("renders connected provider with allowlist + Disconnect confirm flow", () => {
    integrationsMock.mockReturnValue({
      data: [
        {
          provider: "notion",
          status: "configured",
          enabled: true,
          upstream_identity: "notion-bot",
          last_validated_at: "2026-05-20T10:00:00Z",
          last_refresh_failed_at: null,
          allowed_tools: ["update_page"],
        },
      ],
      isLoading: false,
    });
    render(<IntegrationsSettingsPage />);
    expect(screen.getByTestId("badge-notion-connected")).toBeTruthy();
    expect(screen.getByTestId("allow-chip-notion-update_page")).toBeTruthy();
    fireEvent.click(screen.getByTestId("disconnect-notion"));
    expect(screen.getByTestId("disconnect-confirm-notion")).toBeTruthy();
    fireEvent.click(screen.getByTestId("disconnect-confirm-btn-notion"));
    expect(deleteMutate).toHaveBeenCalledWith("notion");
  });

  it("shows Reconnect-required badge for broken provider", () => {
    integrationsMock.mockReturnValue({
      data: [
        {
          provider: "linear",
          status: "broken",
          enabled: true,
          upstream_identity: "linear-bot",
          last_validated_at: null,
          last_refresh_failed_at: "2026-05-20T10:00:00Z",
          allowed_tools: [],
        },
      ],
      isLoading: false,
    });
    render(<IntegrationsSettingsPage />);
    expect(screen.getByTestId("badge-linear-broken")).toBeTruthy();
  });

  it("toggles enabled via PATCH", () => {
    integrationsMock.mockReturnValue({
      data: [
        {
          provider: "linear",
          status: "configured",
          enabled: true,
          upstream_identity: "linear-bot",
          last_validated_at: null,
          last_refresh_failed_at: null,
          allowed_tools: [],
        },
      ],
      isLoading: false,
    });
    render(<IntegrationsSettingsPage />);
    fireEvent.click(screen.getByTestId("enabled-linear"));
    expect(patchMutate).toHaveBeenCalledWith({
      provider: "linear",
      body: { enabled: false },
    });
  });

  it("adds and removes allowlist entries", () => {
    integrationsMock.mockReturnValue({
      data: [
        {
          provider: "linear",
          status: "configured",
          enabled: true,
          upstream_identity: "linear-bot",
          last_validated_at: null,
          last_refresh_failed_at: null,
          allowed_tools: ["update_issue"],
        },
      ],
      isLoading: false,
    });
    render(<IntegrationsSettingsPage />);

    fireEvent.click(screen.getByTestId("allow-remove-linear-update_issue"));
    expect(patchMutate).toHaveBeenCalledWith({
      provider: "linear",
      body: { allowed_tools: [] },
    });

    const input = screen.getByTestId("allow-input-linear") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "create_comment" } });
    fireEvent.click(screen.getByTestId("allow-add-linear"));
    expect(patchMutate).toHaveBeenCalledWith({
      provider: "linear",
      body: { allowed_tools: ["update_issue", "create_comment"] },
    });
  });
});
