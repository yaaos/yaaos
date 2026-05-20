import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const codingAgentsMock = vi.fn();
const installMutate = vi.fn();
const uninstallMutate = vi.fn();

vi.mock("../queries", () => ({
  useCodingAgents: () => codingAgentsMock(),
  useInstallCodingAgent: () => ({
    mutate: installMutate,
    isPending: false,
    isError: false,
    error: null,
  }),
  useUninstallCodingAgent: () => ({ mutate: uninstallMutate, isPending: false }),
  useUpdateCodingAgentSettings: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@shared/plugin_picker", async () => {
  const actual =
    await vi.importActual<typeof import("@shared/plugin_picker")>("@shared/plugin_picker");
  return {
    ...actual,
    useAvailablePlugins: () => ({
      data: [
        {
          id: "claude_code",
          type: "coding_agent",
          display_name: "Claude Code",
          description: "Anthropic CLI",
          docs_url: null,
        },
        {
          id: "other",
          type: "coding_agent",
          display_name: "Other Agent",
          description: null,
          docs_url: null,
        },
      ],
      isLoading: false,
      error: null,
    }),
  };
});

vi.mock("@core/api", () => ({
  getCurrentOrgSlug: () => "acme",
}));

vi.mock("@domain/auth", () => ({
  useCurrentUser: () => ({
    data: {
      orgs: [{ slug: "acme", role: "owner", handle: "j", display_name: "Acme" }],
      current_org_slug: "acme",
      user: { id: "u", display_name: "u", primary_email: "u@x", emails: [] },
    },
  }),
}));

import { CodingAgentsSettingsPage } from "../CodingAgentsSettingsPage";

describe("CodingAgentsSettingsPage", () => {
  beforeEach(() => {
    codingAgentsMock.mockReset();
    installMutate.mockReset();
    uninstallMutate.mockReset();
  });

  it("empty state shows the empty message + Add button", () => {
    codingAgentsMock.mockReturnValue({ data: [], isLoading: false });
    render(<CodingAgentsSettingsPage />);
    expect(screen.getByTestId("ca-empty")).toBeInTheDocument();
    expect(screen.getByTestId("ca-add")).toBeInTheDocument();
  });

  it("Add opens the picker with installed plugins disabled", () => {
    codingAgentsMock.mockReturnValue({
      data: [
        {
          plugin_id: "claude_code",
          settings: {},
          created_at: "2026-05-20T00:00:00Z",
          updated_at: "2026-05-20T00:00:00Z",
        },
      ],
      isLoading: false,
    });
    render(<CodingAgentsSettingsPage />);
    fireEvent.click(screen.getByTestId("ca-add"));
    expect(screen.getByTestId("ca-picker-card")).toBeInTheDocument();
    // Already-installed claude_code is greyed out; other is addable.
    expect(screen.getByTestId("ca-picker-add-claude_code")).toBeDisabled();
    expect(screen.getByTestId("ca-picker-add-other")).not.toBeDisabled();
    fireEvent.click(screen.getByTestId("ca-picker-add-other"));
    expect(installMutate).toHaveBeenCalledTimes(1);
  });

  it("Remove confirmation flow gates the uninstall mutation", () => {
    codingAgentsMock.mockReturnValue({
      data: [
        {
          plugin_id: "claude_code",
          settings: {},
          created_at: "2026-05-20T00:00:00Z",
          updated_at: "2026-05-20T00:00:00Z",
        },
      ],
      isLoading: false,
    });
    render(<CodingAgentsSettingsPage />);
    expect(screen.getByTestId("ca-install-claude_code")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("ca-remove-claude_code"));
    expect(screen.getByTestId("ca-remove-confirm-claude_code")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("ca-remove-cancel-claude_code"));
    expect(uninstallMutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("ca-remove-claude_code"));
    fireEvent.click(screen.getByTestId("ca-remove-confirm-btn-claude_code"));
    expect(uninstallMutate).toHaveBeenCalledWith("claude_code");
  });

  it("Settings link targets the per-plugin route", () => {
    codingAgentsMock.mockReturnValue({
      data: [
        {
          plugin_id: "claude_code",
          settings: {},
          created_at: "2026-05-20T00:00:00Z",
          updated_at: "2026-05-20T00:00:00Z",
        },
      ],
      isLoading: false,
    });
    render(<CodingAgentsSettingsPage />);
    expect(screen.getByTestId("ca-settings-claude_code")).toHaveAttribute(
      "href",
      "/orgs/acme/settings/coding-agents/claude_code",
    );
  });
});
