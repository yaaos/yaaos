import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

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

import { vi } from "vitest";
import { CodingAgentSettingsPage } from "../CodingAgentSettingsPage";
import { _resetRegistryForTests, registerPluginSettingsComponent } from "../plugin_registry";

afterEach(() => _resetRegistryForTests());

describe("CodingAgentSettingsPage dispatch", () => {
  it("renders the registered component for known plugins", () => {
    registerPluginSettingsComponent("claude_code", () => (
      <div data-testid="claude-settings">claude code settings</div>
    ));
    render(<CodingAgentSettingsPage pluginId="claude_code" />);
    expect(screen.getByTestId("claude-settings")).toBeInTheDocument();
  });

  it("falls back to placeholder when no component is registered", () => {
    render(<CodingAgentSettingsPage pluginId="mystery" />);
    expect(screen.getByTestId("ca-settings-unavailable")).toBeInTheDocument();
  });
});
