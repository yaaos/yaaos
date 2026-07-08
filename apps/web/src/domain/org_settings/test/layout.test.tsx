import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { OrgSettingsLayout } from "@shared/components/public/layout/org-settings-layout";

describe("OrgSettingsLayout", () => {
  it("is a passthrough wrapper — renders children with no top chrome", () => {
    render(
      <OrgSettingsLayout active="auth">
        <div data-testid="content">section content</div>
      </OrgSettingsLayout>,
    );
    expect(screen.getByTestId("content")).toBeInTheDocument();
    // "No topbar ever" — there must be no tab strip rendered above content.
    expect(screen.queryByTestId("org-settings-tabs")).toBeNull();
  });
});
