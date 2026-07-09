import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type React from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { server } from "../../../test/msw/server";
import { RepoSettingsPage } from "../public/RepoSettingsPage";

/**
 * Component tests for the Repos settings page: the protected-code
 * mode-switch confirm flow, the trigger form's schedule-field visibility +
 * validation, and the path-set row list + Sheet editor + Delete confirm.
 */

const REPO = {
  repo_external_id: "acme/web",
  trigger_count: 0,
  has_protected_code: false,
  auto_approve_enabled: false,
};

const INTAKE_POINTS = [
  { id: "github:pr_opened", kind: "webhook", label: "PR opened", plugin_id: "github" },
  { id: "schedule", kind: "schedule", label: "Schedule", plugin_id: null },
];

const PIPELINE_SUMMARY = {
  id: "p1",
  name: "dev",
  stage_count: 1,
  updated_at: "2026-05-23T00:00:00Z",
  updated_by_login: "alice",
  referenced: false,
};

const MEMBER = {
  user_id: "u1",
  handle: "alice",
  display_name: "Alice",
  role: "owner",
  primary_email: null,
};

const EMPTY_CONFIG = {
  repo_external_id: "acme/web",
  protected_mode: "deny",
  protected_path_sets: [],
  auto_approve_enabled: false,
  auto_approve_conditions: {},
  bindings: [],
};

const PATH_SET_ID = "00000000-0000-0000-0000-000000000001";

const CONFIG_WITH_PATH_SET = {
  ...EMPTY_CONFIG,
  protected_path_sets: [
    { id: PATH_SET_ID, name: "Infra paths", globs: ["infra/**"], owner_user_ids: [] },
  ],
};

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

function withBaseHandlers() {
  server.use(
    http.get("/api/repos", () => HttpResponse.json({ repos: [REPO] })),
    http.get("/api/intake/points", () => HttpResponse.json({ points: INTAKE_POINTS })),
    http.get("/api/pipelines", () => HttpResponse.json({ pipelines: [PIPELINE_SUMMARY] })),
    http.get("/api/memberships", () => HttpResponse.json([MEMBER])),
    http.get("/api/repos/config", () => HttpResponse.json(EMPTY_CONFIG)),
  );
}

async function expandRepoRow(user: ReturnType<typeof userEvent.setup>) {
  render(wrap(<RepoSettingsPage />));
  const row = await screen.findByTestId("repo-row-acme/web");
  await user.click(within(row).getByText("acme/web"));
  await screen.findByTestId(`repo-config-${REPO.repo_external_id}`);
  return row;
}

describe("RepoSettingsPage (MSW)", () => {
  beforeEach(() => withBaseHandlers());

  it("shows the unconfigured badge for a repo with no config", async () => {
    render(wrap(<RepoSettingsPage />));
    const row = await screen.findByTestId("repo-row-acme/web");
    expect(within(row).getByTestId("repo-row-acme/web-status")).toHaveTextContent("unconfigured");
  });

  it("switching protected-code mode opens a confirm dialog; cancel leaves the mode unchanged", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await expandRepoRow(user);

    const denyRadio = screen.getByRole("radio", { name: /Deny list/ });
    const allowRadio = screen.getByRole("radio", { name: /Allow list/ });
    expect(denyRadio).toBeChecked();

    await user.click(allowRadio);
    const dialog = await screen.findByTestId("repo-protected-mode-confirm");
    expect(within(dialog).getByText("This inverts what's protected.")).toBeInTheDocument();

    // Cancel — the mode must not have changed.
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("repo-protected-mode-confirm")).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Deny list/ })).toBeChecked();

    // Confirm — now it applies.
    await user.click(screen.getByRole("radio", { name: /Allow list/ }));
    const dialog2 = await screen.findByTestId("repo-protected-mode-confirm");
    await user.click(within(dialog2).getByTestId("repo-protected-mode-confirm-switch"));
    expect(screen.queryByTestId("repo-protected-mode-confirm")).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Allow list/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Deny list/ })).not.toBeChecked();
  });

  it("trigger form reveals schedule fields only for a schedule-kind intake point, and gates submit", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await expandRepoRow(user);

    await user.click(screen.getByTestId("repo-add-trigger"));
    const form = await screen.findByTestId("repo-trigger-form");

    // Webhook-kind point: no schedule fields, enabled once a pipeline is picked.
    await user.click(within(form).getByTestId("repo-trigger-intake-point"));
    await user.click(await screen.findByRole("option", { name: "PR opened" }));
    expect(within(form).queryByTestId("repo-trigger-schedule-name")).not.toBeInTheDocument();

    await user.click(within(form).getByTestId("repo-trigger-pipeline"));
    await user.click(await screen.findByRole("option", { name: "dev" }));
    expect(within(form).getByTestId("repo-trigger-save")).toBeEnabled();

    // Switch to the schedule-kind point: schedule fields appear, submit gates on them.
    await user.click(within(form).getByTestId("repo-trigger-intake-point"));
    await user.click(await screen.findByRole("option", { name: "Schedule" }));
    expect(within(form).getByTestId("repo-trigger-schedule-name")).toBeInTheDocument();
    expect(within(form).getByTestId("repo-trigger-save")).toBeDisabled();

    await user.type(within(form).getByTestId("repo-trigger-schedule-name"), "nightly");
    await user.type(within(form).getByTestId("repo-trigger-schedule-cron"), "0 3 * * *");
    expect(within(form).getByTestId("repo-trigger-save")).toBeDisabled();

    await user.click(within(form).getByTestId("repo-trigger-schedule-notify"));
    await user.click(await screen.findByTestId("repo-trigger-schedule-notify-option-u1"));
    expect(within(form).getByTestId("repo-trigger-save")).toBeEnabled();
  });

  it("path-set rows render the set name and expose Edit and Delete buttons", async () => {
    server.use(http.get("/api/repos/config", () => HttpResponse.json(CONFIG_WITH_PATH_SET)));
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await expandRepoRow(user);

    const row = screen.getByTestId(`repo-path-set-row-${PATH_SET_ID}`);
    expect(within(row).getByText("Infra paths")).toBeInTheDocument();
    expect(row.querySelector(`[data-testid="repo-path-set-edit-${PATH_SET_ID}"]`)).not.toBeNull();
    expect(row.querySelector(`[data-testid="repo-path-set-delete-${PATH_SET_ID}"]`)).not.toBeNull();
  });

  it("Edit opens a Sheet; saving updates the row name in the draft", async () => {
    server.use(
      http.get("/api/repos/config", () => HttpResponse.json(CONFIG_WITH_PATH_SET)),
      http.put("/api/repos/settings", () => new HttpResponse(null, { status: 200 })),
    );
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await expandRepoRow(user);

    await user.click(screen.getByTestId(`repo-path-set-edit-${PATH_SET_ID}`));
    const editor = await screen.findByTestId("repo-path-set-editor");
    expect(editor).toBeInTheDocument();

    const nameInput = within(editor).getByTestId("repo-path-set-name");
    expect(nameInput).toHaveValue("Infra paths");

    await user.clear(nameInput);
    await user.type(nameInput, "Renamed set");

    await user.click(within(editor).getByTestId("repo-path-set-editor-save"));

    // The sheet should close and the new name appear in the row list.
    expect(screen.queryByTestId("repo-path-set-editor")).not.toBeInTheDocument();
    const row = screen.getByTestId(`repo-path-set-row-${PATH_SET_ID}`);
    expect(within(row).getByText("Renamed set")).toBeInTheDocument();
  });

  it("empty name disables the Sheet save button", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await expandRepoRow(user);

    // Open Add — starts with empty name.
    await user.click(screen.getByTestId("repo-add-path-set"));
    const editor = await screen.findByTestId("repo-path-set-editor");
    expect(within(editor).getByTestId("repo-path-set-editor-save")).toBeDisabled();

    // Typing a name enables save.
    await user.type(within(editor).getByTestId("repo-path-set-name"), "New set");
    expect(within(editor).getByTestId("repo-path-set-editor-save")).toBeEnabled();
  });

  it("Delete shows the confirm dialog; cancel leaves the row; confirm removes it", async () => {
    server.use(http.get("/api/repos/config", () => HttpResponse.json(CONFIG_WITH_PATH_SET)));
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await expandRepoRow(user);

    // Open delete confirm.
    await user.click(screen.getByTestId(`repo-path-set-delete-${PATH_SET_ID}`));
    const dialog = await screen.findByTestId("repo-path-set-delete-confirm");
    expect(within(dialog).getByRole("heading")).toHaveTextContent(/Delete/);
    expect(within(dialog).getByText("This can't be undone.")).toBeInTheDocument();

    // Cancel: row still there.
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("repo-path-set-delete-confirm")).not.toBeInTheDocument();
    expect(screen.getByTestId(`repo-path-set-row-${PATH_SET_ID}`)).toBeInTheDocument();

    // Confirm: row gone.
    await user.click(screen.getByTestId(`repo-path-set-delete-${PATH_SET_ID}`));
    const dialog2 = await screen.findByTestId("repo-path-set-delete-confirm");
    await user.click(within(dialog2).getByTestId("repo-path-set-delete-confirm-action"));
    expect(screen.queryByTestId("repo-path-set-delete-confirm")).not.toBeInTheDocument();
    expect(screen.queryByTestId(`repo-path-set-row-${PATH_SET_ID}`)).not.toBeInTheDocument();
  });
});
