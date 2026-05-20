import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const vcsStateMock = vi.fn();
const setVcsMutate = vi.fn();
const clearVcsMutate = vi.fn();

vi.mock("../queries", () => ({
  useVcsState: () => vcsStateMock(),
  useSetVcs: () => ({ mutate: setVcsMutate, isPending: false, isError: false, error: null }),
  useClearVcs: () => ({ mutate: clearVcsMutate, isPending: false }),
}));

vi.mock("@shared/plugin_picker", async () => {
  const actual =
    await vi.importActual<typeof import("@shared/plugin_picker")>("@shared/plugin_picker");
  return {
    ...actual,
    useAvailablePlugins: () => ({
      data: [
        {
          id: "github",
          type: "vcs",
          display_name: "GitHub",
          description: "GitHub App",
          docs_url: "https://docs.github.com",
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

import { VcsSettingsPage } from "../VcsSettingsPage";

describe("VcsSettingsPage", () => {
  beforeEach(() => {
    vcsStateMock.mockReset();
    setVcsMutate.mockReset();
    clearVcsMutate.mockReset();
  });

  it("empty state shows the picker; picking github fires setVcs", () => {
    vcsStateMock.mockReturnValue({
      data: { plugin_id: null, settings: {} },
      isLoading: false,
    });
    render(<VcsSettingsPage />);
    expect(screen.getByTestId("vcs-picker")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("vcs-picker-add-github"));
    expect(setVcsMutate).toHaveBeenCalledTimes(1);
    const call = setVcsMutate.mock.calls[0];
    if (!call) throw new Error("expected a call");
    expect(call[0].plugin_id).toBe("github");
  });

  it("connected state shows the chosen plugin + Remove + confirmation flow", () => {
    vcsStateMock.mockReturnValue({
      data: { plugin_id: "github", settings: { installation_id: 42 } },
      isLoading: false,
    });
    render(<VcsSettingsPage />);
    expect(screen.getByTestId("vcs-connected")).toBeInTheDocument();
    // Confirmation modal hidden until Remove clicked.
    expect(screen.queryByTestId("vcs-remove-confirm")).toBeNull();
    fireEvent.click(screen.getByTestId("vcs-remove"));
    expect(screen.getByTestId("vcs-remove-confirm")).toBeInTheDocument();
    // Cancel hides the modal without firing the mutation.
    fireEvent.click(screen.getByTestId("vcs-remove-cancel"));
    expect(screen.queryByTestId("vcs-remove-confirm")).toBeNull();
    expect(clearVcsMutate).not.toHaveBeenCalled();
    // Confirm fires it.
    fireEvent.click(screen.getByTestId("vcs-remove"));
    fireEvent.click(screen.getByTestId("vcs-remove-confirm-btn"));
    expect(clearVcsMutate).toHaveBeenCalledTimes(1);
  });

  it("loading state renders placeholder text", () => {
    vcsStateMock.mockReturnValue({ data: undefined, isLoading: true });
    render(<VcsSettingsPage />);
    expect(screen.getByText(/Loading/)).toBeInTheDocument();
  });
});
