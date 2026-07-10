import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type React from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { server } from "../../../test/msw/server";
import { PipelinesSettingsPage } from "../public/PipelinesSettingsPage";

/**
 * Component tests for the Pipelines settings page: list rendering, lazy
 * expand-to-edit, stage-editor per-kind field rendering, boundary-condition
 * visibility rules, "New from template", and the 400/409 error banners.
 */

const CODING_AGENTS = [
  {
    plugin_id: "claude_code",
    settings: {},
    created_at: "2026-05-20T00:00:00Z",
    updated_at: "2026-05-20T00:00:00Z",
  },
];
const CLAUDE_DEFAULTS = {
  models: ["claude-sonnet-5", "claude-opus-5"],
  efforts: ["low", "medium", "high"],
};
const ACTIONS = [
  { action_id: "github:create_pr", label: "Open pull request", plugin_id: "github" },
];

const BOUNDARY_ALWAYS_HITL = {
  mode: "always_hitl",
  on_blocker_residuals: false,
  on_should_fix_residuals: false,
  on_nit_residuals: false,
  on_protected_code: false,
  on_confidence_below: null,
};

const DEV_SUMMARY = {
  id: "p1",
  name: "dev",
  stage_count: 2,
  updated_at: "2026-05-23T00:00:00Z",
  updated_by_login: "alice",
  referenced: false,
};

const IMPLEMENTATION_SUMMARY = {
  id: "p2",
  name: "implementation",
  stage_count: 1,
  updated_at: "2026-05-23T00:00:00Z",
  updated_by_login: "alice",
  referenced: true,
};

const DEV_DETAIL = {
  id: "p1",
  name: "dev",
  description: "",
  updated_at: "2026-05-23T00:00:00Z",
  updated_by_login: "alice",
  referenced: false,
  stages: [
    {
      kind: "skill",
      id: "s1",
      name: "requirements",
      description: "",
      skill_name: "requirements",
      coding_agent_plugin_id: "claude_code",
      model: "claude-sonnet-5",
      effort: "medium",
      review: null,
      context_stages: null,
      wallclock_seconds: 3600,
      boundary: BOUNDARY_ALWAYS_HITL,
    },
    {
      kind: "call",
      id: "s2",
      description: "Implement the plan",
      pipeline_id: "p2",
    },
  ],
};

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
}

function withBaseHandlers() {
  server.use(
    http.get("/api/coding-agents", () => HttpResponse.json(CODING_AGENTS)),
    http.get("/api/claude_code/defaults", () => HttpResponse.json(CLAUDE_DEFAULTS)),
    http.get("/api/actions", () => HttpResponse.json({ actions: ACTIONS })),
    http.get("/api/pipelines/templates", () => HttpResponse.json({ templates: [] })),
  );
}

describe("PipelinesSettingsPage (MSW)", () => {
  beforeEach(() => withBaseHandlers());

  it("renders the empty state with zero pipelines", async () => {
    server.use(http.get("/api/pipelines", () => HttpResponse.json({ pipelines: [] })));
    render(wrap(<PipelinesSettingsPage />));
    expect(await screen.findByText("No pipelines yet.")).toBeInTheDocument();
  });

  it("renders a download link for the pipeline skills bundle", async () => {
    server.use(http.get("/api/pipelines", () => HttpResponse.json({ pipelines: [] })));
    render(wrap(<PipelinesSettingsPage />));
    await screen.findByText("No pipelines yet.");

    const link = screen.getByTestId("pipelines-download-skills");
    expect(link).toHaveAttribute("href", "/yaaos-pipeline-skills.zip");
    expect(link).toHaveAttribute("download");
  });

  it("lists pipelines with stage count and referenced badge", async () => {
    server.use(
      http.get("/api/pipelines", () =>
        HttpResponse.json({ pipelines: [DEV_SUMMARY, IMPLEMENTATION_SUMMARY] }),
      ),
    );
    render(wrap(<PipelinesSettingsPage />));
    expect(await screen.findByTestId("pipelines-list")).toBeInTheDocument();
    const devRow = screen.getByTestId("pipeline-row-p1");
    expect(within(devRow).getByText("dev")).toBeInTheDocument();
    expect(within(devRow).getByText("2 stages")).toBeInTheDocument();
    const implRow = screen.getByTestId("pipeline-row-p2");
    expect(within(implRow).getByText("referenced")).toBeInTheDocument();
  });

  it("expanding a row lazily fetches the definition and renders its stages", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    let fetchedDetail = false;
    server.use(
      http.get("/api/pipelines", () =>
        HttpResponse.json({ pipelines: [DEV_SUMMARY, IMPLEMENTATION_SUMMARY] }),
      ),
      http.get("/api/pipelines/p1", () => {
        fetchedDetail = true;
        return HttpResponse.json(DEV_DETAIL);
      }),
    );
    render(wrap(<PipelinesSettingsPage />));
    const devRow = await screen.findByTestId("pipeline-row-p1");
    expect(fetchedDetail).toBe(false);

    await user.click(within(devRow).getByText("dev"));

    await waitFor(() => expect(fetchedDetail).toBe(true));
    // Stage row testids key on a client-only React key, not the server stage
    // id — assert on rendered content instead.
    await waitFor(() => expect(within(devRow).getByText("requirements")).toBeInTheDocument());
    expect(devRow.querySelectorAll('[data-testid^="pipeline-stage-row-"]')).toHaveLength(2);
    // Call stage summary resolves the target pipeline's name from the list.
    expect(within(devRow).getByText("implementation")).toBeInTheDocument();
  });

  it("New pipeline: adding a skill stage opens the editor with skill fields", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    server.use(http.get("/api/pipelines", () => HttpResponse.json({ pipelines: [] })));
    render(wrap(<PipelinesSettingsPage />));
    await screen.findByText("No pipelines yet.");

    await user.click(screen.getByTestId("pipeline-new"));
    expect(await screen.findByTestId("pipeline-new-card")).toBeInTheDocument();

    await user.click(screen.getByTestId("pipeline-add-stage"));
    await user.click(await screen.findByTestId("pipeline-add-stage-skill"));

    const sheet = await screen.findByTestId("stage-editor");
    expect(within(sheet).getByTestId("stage-name")).toBeInTheDocument();
    expect(within(sheet).getByTestId("stage-skill-name")).toBeInTheDocument();
    expect(within(sheet).getByTestId("stage-agent")).toBeInTheDocument();
    expect(within(sheet).getByTestId("stage-review-enabled")).toBeInTheDocument();
    expect(within(sheet).getByTestId("stage-context-all-upstream")).toBeInTheDocument();
    expect(within(sheet).getByTestId("stage-boundary-mode")).toBeInTheDocument();
    // Conditional-only fields are hidden by default (mode defaults to always_hitl).
    expect(within(sheet).queryByTestId("stage-boundary-on-blocker")).not.toBeInTheDocument();
  });

  it("boundary conditional mode reveals condition checkboxes + confidence picker", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    server.use(http.get("/api/pipelines", () => HttpResponse.json({ pipelines: [] })));
    render(wrap(<PipelinesSettingsPage />));
    await screen.findByText("No pipelines yet.");
    await user.click(screen.getByTestId("pipeline-new"));
    await user.click(screen.getByTestId("pipeline-add-stage"));
    await user.click(await screen.findByTestId("pipeline-add-stage-skill"));
    const sheet = await screen.findByTestId("stage-editor");

    expect(within(sheet).queryByTestId("stage-boundary-on-blocker")).not.toBeInTheDocument();

    await user.click(within(sheet).getByRole("radio", { name: "Conditional" }));

    expect(within(sheet).getByTestId("stage-boundary-on-blocker")).toBeInTheDocument();
    expect(within(sheet).getByTestId("stage-boundary-on-should-fix")).toBeInTheDocument();
    expect(within(sheet).getByTestId("stage-boundary-on-nit")).toBeInTheDocument();
    expect(within(sheet).getByTestId("stage-boundary-on-protected")).toBeInTheDocument();
    expect(within(sheet).getByTestId("stage-boundary-confidence")).toBeInTheDocument();

    await user.click(within(sheet).getByRole("radio", { name: "Always proceed automatically" }));
    expect(within(sheet).queryByTestId("stage-boundary-on-blocker")).not.toBeInTheDocument();
  });

  it("adding an action stage renders the action picker only", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    server.use(http.get("/api/pipelines", () => HttpResponse.json({ pipelines: [] })));
    render(wrap(<PipelinesSettingsPage />));
    await screen.findByText("No pipelines yet.");
    await user.click(screen.getByTestId("pipeline-new"));
    await user.click(screen.getByTestId("pipeline-add-stage"));
    await user.click(await screen.findByTestId("pipeline-add-stage-action"));
    const sheet = await screen.findByTestId("stage-editor");

    expect(within(sheet).getByTestId("stage-action")).toBeInTheDocument();
    expect(within(sheet).queryByTestId("stage-name")).not.toBeInTheDocument();
    expect(within(sheet).queryByTestId("stage-boundary-mode")).not.toBeInTheDocument();
  });

  it("adding a call stage renders the pipeline picker only", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    server.use(http.get("/api/pipelines", () => HttpResponse.json({ pipelines: [DEV_SUMMARY] })));
    render(wrap(<PipelinesSettingsPage />));
    await screen.findByTestId("pipelines-list");
    await user.click(screen.getByTestId("pipeline-new"));
    await user.click(screen.getByTestId("pipeline-add-stage"));
    await user.click(await screen.findByTestId("pipeline-add-stage-call"));
    const sheet = await screen.findByTestId("stage-editor");

    expect(within(sheet).getByTestId("stage-call-pipeline")).toBeInTheDocument();
    expect(within(sheet).queryByTestId("stage-name")).not.toBeInTheDocument();
  });

  it("400 invalid_definition on save renders an inline error banner", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    server.use(
      http.get("/api/pipelines", () => HttpResponse.json({ pipelines: [] })),
      http.post("/api/pipelines", () =>
        HttpResponse.json({ detail: { error: "invalid_definition" } }, { status: 400 }),
      ),
    );
    render(wrap(<PipelinesSettingsPage />));
    await screen.findByText("No pipelines yet.");
    await user.click(screen.getByTestId("pipeline-new"));
    const card = await screen.findByTestId("pipeline-new-card");
    await user.type(within(card).getByTestId("pipeline-name"), "cyclic");

    await user.click(within(card).getByTestId("pipeline-add-stage"));
    await user.click(await screen.findByTestId("pipeline-add-stage-action"));
    const sheet = await screen.findByTestId("stage-editor");
    // Pick the only available action so the stage is valid enough to submit.
    await user.click(within(sheet).getByTestId("stage-action"));
    await user.click(await screen.findByText("Open pull request"));
    await user.click(within(sheet).getByTestId("stage-editor-save"));

    await user.click(within(card).getByTestId("pipeline-new-save"));

    expect(await screen.findByText(/Invalid pipeline definition/)).toBeInTheDocument();
  });

  it("409 referenced on delete shows the in-use message", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    server.use(
      http.get("/api/pipelines", () => HttpResponse.json({ pipelines: [IMPLEMENTATION_SUMMARY] })),
      http.get("/api/pipelines/p2", () =>
        HttpResponse.json({
          id: "p2",
          name: "implementation",
          description: "",
          updated_at: "2026-05-23T00:00:00Z",
          updated_by_login: "alice",
          referenced: true,
          stages: [
            {
              kind: "action",
              id: "a1",
              description: "Open pull request",
              action_id: "github:create_pr",
            },
          ],
        }),
      ),
      http.delete("/api/pipelines/p2", () =>
        HttpResponse.json({ detail: { error: "referenced" } }, { status: 409 }),
      ),
    );
    render(wrap(<PipelinesSettingsPage />));
    const row = await screen.findByTestId("pipeline-row-p2");
    await user.click(within(row).getByText("implementation"));
    await waitFor(() => expect(within(row).getByTestId("pipeline-delete")).toBeInTheDocument());

    await user.click(within(row).getByTestId("pipeline-delete"));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    expect(
      await within(row).findByText("In use by a repo trigger or another pipeline."),
    ).toBeInTheDocument();
  });

  it("New from template creates a pipeline from the picked template", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    let requestedId: string | null = null;
    server.use(
      http.get("/api/pipelines", () => HttpResponse.json({ pipelines: [] })),
      http.get("/api/pipelines/templates", () =>
        HttpResponse.json({
          templates: [{ id: "t1", name: "dev", description: "Spec, plan, implement.", stages: [] }],
        }),
      ),
      http.post("/api/pipelines/from-template", async ({ request }) => {
        const body = (await request.json()) as { template_id: string };
        requestedId = body.template_id;
        return HttpResponse.json({ id: "new-id" }, { status: 201 });
      }),
    );
    render(wrap(<PipelinesSettingsPage />));
    await screen.findByText("No pipelines yet.");

    await user.click(screen.getByTestId("pipeline-new-from-template"));
    const dialog = await screen.findByTestId("pipeline-template-dialog");
    await user.click(within(dialog).getByTestId("pipeline-template-dev"));

    await waitFor(() => expect(requestedId).toBe("t1"));
  });
});
