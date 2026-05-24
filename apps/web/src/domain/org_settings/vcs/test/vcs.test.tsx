import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const vcsStateMock = vi.fn();
const setVcsMutate = vi.fn();
const clearVcsMutate = vi.fn();
const startGithubInstallMutate = vi.fn();

vi.mock("../queries", () => ({
  useVcsState: () => vcsStateMock(),
  useSetVcs: () => ({ mutate: setVcsMutate, isPending: false, isError: false, error: null }),
  useClearVcs: () => ({ mutate: clearVcsMutate, isPending: false }),
  useStartGithubInstall: () => ({
    mutate: startGithubInstallMutate,
    isPending: false,
    isError: false,
    error: null,
  }),
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

const installationMock = vi.fn();
const repositoriesMock = vi.fn();

vi.mock("@core/api", () => ({
  getCurrentOrgSlug: () => "acme",
  useGithubInstallation: () => installationMock(),
  useGithubRepositories: () => repositoriesMock(),
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
    startGithubInstallMutate.mockReset();
    installationMock.mockReset();
    repositoriesMock.mockReset();
    installationMock.mockReturnValue({ data: undefined, isLoading: false, error: null });
    repositoriesMock.mockReturnValue({ data: undefined, isLoading: false, error: null });
  });

  it("empty state shows the picker; picking github fires startGithubInstall (not setVcs)", () => {
    vcsStateMock.mockReturnValue({
      data: { plugin_id: null, settings: {} },
      isLoading: false,
    });
    render(<VcsSettingsPage />);
    expect(screen.getByTestId("vcs-picker")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("vcs-picker-add-github"));
    expect(startGithubInstallMutate).toHaveBeenCalledTimes(1);
    expect(setVcsMutate).not.toHaveBeenCalled();
  });

  it("connected state shows the chosen plugin + Remove + confirmation flow", () => {
    vcsStateMock.mockReturnValue({
      data: { plugin_id: "github", settings: { installation_id: 42 } },
      isLoading: false,
    });
    installationMock.mockReturnValue({
      data: {
        app_configured: true,
        installed: true,
        account_login: "acme-org",
        install_external_id: "42",
        installations_url: "https://github.com/settings/installations/42",
      },
      isLoading: false,
      error: null,
    });
    repositoriesMock.mockReturnValue({
      data: { total_count: 0, repositories: [] },
      isLoading: false,
      error: null,
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

  it("connected state lists enabled repos and links out to GitHub", () => {
    vcsStateMock.mockReturnValue({
      data: { plugin_id: "github", settings: { installation_id: 42 } },
      isLoading: false,
    });
    installationMock.mockReturnValue({
      data: {
        app_configured: true,
        installed: true,
        account_login: "acme-org",
        install_external_id: "42",
        installations_url: "https://github.com/settings/installations/42",
      },
      isLoading: false,
      error: null,
    });
    repositoriesMock.mockReturnValue({
      data: {
        total_count: 2,
        repositories: [
          { full_name: "acme-org/api", html_url: "https://github.com/acme-org/api", private: true },
          {
            full_name: "acme-org/web",
            html_url: "https://github.com/acme-org/web",
            private: false,
          },
        ],
      },
      isLoading: false,
      error: null,
    });
    render(<VcsSettingsPage />);
    const list = screen.getByTestId("vcs-repos-list");
    expect(list).toHaveTextContent("acme-org/api");
    expect(list).toHaveTextContent("acme-org/web");
    const manage = screen.getByTestId("vcs-manage-on-github") as HTMLAnchorElement;
    expect(manage.href).toBe("https://github.com/settings/installations/42");
    // Reconnect button is gone.
    expect(screen.queryByTestId("vcs-reconnect")).toBeNull();
  });

  it("loading state renders placeholder text", () => {
    vcsStateMock.mockReturnValue({ data: undefined, isLoading: true });
    render(<VcsSettingsPage />);
    expect(screen.getByText(/Loading/)).toBeInTheDocument();
  });

  it("github with no install row renders 'needs setup' + install action, no repo list", () => {
    vcsStateMock.mockReturnValue({
      data: { plugin_id: "github", settings: { installation_id: 42 } },
      isLoading: false,
    });
    installationMock.mockReturnValue({
      data: {
        app_configured: true,
        installed: false,
        account_login: null,
        install_external_id: null,
        installations_url: null,
      },
      isLoading: false,
      error: null,
    });
    render(<VcsSettingsPage />);
    expect(screen.getByTestId("vcs-github-needs-setup")).toBeInTheDocument();
    expect(screen.getByTestId("vcs-github-incomplete")).toBeInTheDocument();
    expect(screen.getByTestId("vcs-github-install")).toBeInTheDocument();
    // The healthy-state UI should not render.
    expect(screen.queryByTestId("vcs-github-details")).toBeNull();
    expect(screen.queryByTestId("vcs-repos-list")).toBeNull();
    expect(screen.queryByTestId("vcs-manage-on-github")).toBeNull();
    // Remove still works so the user can clear the stale VCS row.
    expect(screen.getByTestId("vcs-remove")).toBeInTheDocument();
  });

  it("clicking the install button fires startGithubInstall", () => {
    vcsStateMock.mockReturnValue({
      data: { plugin_id: "github", settings: {} },
      isLoading: false,
    });
    installationMock.mockReturnValue({
      data: {
        app_configured: true,
        installed: false,
        account_login: null,
        install_external_id: null,
        installations_url: null,
      },
      isLoading: false,
      error: null,
    });
    render(<VcsSettingsPage />);
    fireEvent.click(screen.getByTestId("vcs-github-install"));
    expect(startGithubInstallMutate).toHaveBeenCalledTimes(1);
  });

  it("github with the platform App unprovisioned renders 'needs setup' + operator guidance, no install button", () => {
    vcsStateMock.mockReturnValue({
      data: { plugin_id: "github", settings: {} },
      isLoading: false,
    });
    installationMock.mockReturnValue({
      data: {
        app_configured: false,
        installed: false,
        account_login: null,
        install_external_id: null,
        installations_url: null,
      },
      isLoading: false,
      error: null,
    });
    render(<VcsSettingsPage />);
    expect(screen.getByTestId("vcs-github-needs-setup")).toBeInTheDocument();
    expect(screen.getByTestId("vcs-github-incomplete")).toHaveTextContent(/yaaos operator/i);
    // No install button when the App isn't provisioned on the deployment.
    expect(screen.queryByTestId("vcs-github-install")).toBeNull();
    expect(screen.getByTestId("vcs-remove")).toBeInTheDocument();
  });
});
