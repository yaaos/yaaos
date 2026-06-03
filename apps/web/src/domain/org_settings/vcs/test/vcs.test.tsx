import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type React from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { server } from "../../../../test/msw/server";
import { VcsSettingsPage } from "../VcsSettingsPage";

/**
 * Tests for VcsSettingsPage via MSW.
 */

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

const GITHUB_PLUGIN = {
  id: "github",
  type: "vcs",
  display_name: "GitHub",
  description: "GitHub App",
  docs_url: "https://docs.github.com",
};

function setupBase() {
  server.use(
    http.get("/api/plugins/available", () => HttpResponse.json({ plugins: [GITHUB_PLUGIN] })),
    http.get("/api/github/installation", () =>
      HttpResponse.json({
        app_configured: true,
        installed: false,
        slug: null,
        account_login: null,
        install_external_id: null,
        installed_at: null,
        installations_url: null,
      }),
    ),
    http.get("/api/github/repositories", () =>
      HttpResponse.json({ total_count: 0, repositories: [] }),
    ),
  );
}

describe("VcsSettingsPage (MSW)", () => {
  beforeEach(() => {
    setupBase();
  });

  it("empty state shows the picker; picking github fires startGithubInstall (not setVcs)", async () => {
    let installCalled = false;
    server.use(
      http.get("/api/vcs", () => HttpResponse.json({ plugin_id: null, settings: {} })),
      http.post("/api/github/install/start", () => {
        installCalled = true;
        return HttpResponse.json({ redirect_url: "https://github.com/install" });
      }),
    );
    render(wrap(<VcsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("vcs-picker")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("vcs-picker-add-github"));
    await waitFor(() => expect(installCalled).toBe(true));
  });

  it("connected state shows the chosen plugin + Remove + confirmation flow", async () => {
    let clearCalled = false;
    server.use(
      http.get("/api/vcs", () =>
        HttpResponse.json({ plugin_id: "github", settings: { installation_id: 42 } }),
      ),
      http.get("/api/github/installation", () =>
        HttpResponse.json({
          app_configured: true,
          installed: true,
          slug: "acme-org",
          account_login: "acme-org",
          install_external_id: "42",
          installed_at: null,
          installations_url: "https://github.com/settings/installations/42",
        }),
      ),
      http.delete("/api/vcs", () => {
        clearCalled = true;
        return HttpResponse.json({ plugin_id: null, settings: {} });
      }),
    );
    render(wrap(<VcsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("vcs-remove")).toBeInTheDocument());
    expect(screen.queryByTestId("vcs-remove-confirm")).toBeNull();
    fireEvent.click(screen.getByTestId("vcs-remove"));
    expect(screen.getByTestId("vcs-remove-confirm")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("vcs-remove-cancel"));
    expect(screen.queryByTestId("vcs-remove-confirm")).toBeNull();
    expect(clearCalled).toBe(false);
    fireEvent.click(screen.getByTestId("vcs-remove"));
    fireEvent.click(screen.getByTestId("vcs-remove-confirm-btn"));
    await waitFor(() => expect(clearCalled).toBe(true));
  });

  it("connected state lists enabled repos and links out to GitHub", async () => {
    server.use(
      http.get("/api/vcs", () =>
        HttpResponse.json({ plugin_id: "github", settings: { installation_id: 42 } }),
      ),
      http.get("/api/github/installation", () =>
        HttpResponse.json({
          app_configured: true,
          installed: true,
          slug: "acme-org",
          account_login: "acme-org",
          install_external_id: "42",
          installed_at: null,
          installations_url: "https://github.com/settings/installations/42",
        }),
      ),
      http.get("/api/github/repositories", () =>
        HttpResponse.json({
          total_count: 2,
          repositories: [
            {
              full_name: "acme-org/api",
              html_url: "https://github.com/acme-org/api",
              private: true,
            },
            {
              full_name: "acme-org/web",
              html_url: "https://github.com/acme-org/web",
              private: false,
            },
          ],
        }),
      ),
    );
    render(wrap(<VcsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("vcs-repos-list")).toBeInTheDocument());
    const list = screen.getByTestId("vcs-repos-list");
    expect(list).toHaveTextContent("acme-org/api");
    expect(list).toHaveTextContent("acme-org/web");
    const manage = screen.getByTestId("vcs-manage-on-github") as HTMLAnchorElement;
    expect(manage.href).toBe("https://github.com/settings/installations/42");
    expect(screen.queryByTestId("vcs-reconnect")).toBeNull();
  });

  it("loading state renders a skeleton while VCS state is fetching", async () => {
    server.use(
      http.get("/api/vcs", async () => {
        // Delay to keep loading state visible.
        await new Promise((r) => setTimeout(r, 50));
        return HttpResponse.json({ plugin_id: null, settings: {} });
      }),
    );
    render(wrap(<VcsSettingsPage />));
    // Suspense skeleton renders while the query is pending; content appears after.
    await waitFor(() => expect(screen.getByTestId("vcs-picker")).toBeInTheDocument());
  });

  it("github with no install row renders 'needs setup' + install action, no repo list", async () => {
    server.use(
      http.get("/api/vcs", () =>
        HttpResponse.json({ plugin_id: "github", settings: { installation_id: 42 } }),
      ),
      http.get("/api/github/installation", () =>
        HttpResponse.json({
          app_configured: true,
          installed: false,
          slug: null,
          account_login: null,
          install_external_id: null,
          installed_at: null,
          installations_url: null,
        }),
      ),
    );
    render(wrap(<VcsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("vcs-github-needs-setup")).toBeInTheDocument());
    expect(screen.getByTestId("vcs-github-incomplete")).toBeInTheDocument();
    expect(screen.getByTestId("vcs-github-install")).toBeInTheDocument();
    expect(screen.queryByTestId("vcs-github-details")).toBeNull();
    expect(screen.queryByTestId("vcs-repos-list")).toBeNull();
    expect(screen.queryByTestId("vcs-manage-on-github")).toBeNull();
    expect(screen.getByTestId("vcs-remove")).toBeInTheDocument();
  });

  it("clicking the install button fires startGithubInstall", async () => {
    let installCalled = false;
    server.use(
      http.get("/api/vcs", () => HttpResponse.json({ plugin_id: "github", settings: {} })),
      http.post("/api/github/install/start", () => {
        installCalled = true;
        return HttpResponse.json({ redirect_url: "https://github.com/install" });
      }),
    );
    render(wrap(<VcsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("vcs-github-install")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("vcs-github-install"));
    await waitFor(() => expect(installCalled).toBe(true));
  });

  it("github with the platform App unprovisioned renders 'needs setup' + operator guidance, no install button", async () => {
    server.use(
      http.get("/api/vcs", () => HttpResponse.json({ plugin_id: "github", settings: {} })),
      http.get("/api/github/installation", () =>
        HttpResponse.json({
          app_configured: false,
          installed: false,
          slug: null,
          account_login: null,
          install_external_id: null,
          installed_at: null,
          installations_url: null,
        }),
      ),
    );
    render(wrap(<VcsSettingsPage />));
    await waitFor(() => expect(screen.getByTestId("vcs-github-needs-setup")).toBeInTheDocument());
    expect(screen.getByTestId("vcs-github-incomplete")).toHaveTextContent(/yaaos operator/i);
    expect(screen.queryByTestId("vcs-github-install")).toBeNull();
    expect(screen.getByTestId("vcs-remove")).toBeInTheDocument();
  });
});
