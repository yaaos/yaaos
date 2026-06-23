/**
 * Admin controls — section-level checkboxes + bulk-action buttons.
 *
 * Covers: admin sees controls, non-admin does not; select-all / deselect-all
 * mechanics; per-card checkbox toggles selection; shutdown and cancel-shutdown
 * buttons are disabled when selection is empty and enabled when non-empty.
 *
 * These tests pass props directly to AgentSections — no QueryClientProvider or
 * Suspense wrapper needed because AgentSections holds no React Query hooks.
 */

import type { AgentRow } from "@core/api/public/queries";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AgentSections } from "../AgentSections";

function makeAgent(instance_id: string, lifecycle: AgentRow["lifecycle"] = "active"): AgentRow {
  return {
    id: instance_id,
    instance_id,
    state: "reachable",
    lifecycle,
    last_heartbeat_at: "2026-01-01T12:00:00Z",
    os: null,
    cpu_count: null,
    memory_bytes: null,
    claimed_workspace_count: 0,
    version: null,
  };
}

describe("AgentSections — admin controls visibility", () => {
  it("admin sees per-card checkboxes on active agents", () => {
    render(
      <AgentSections
        agents={[makeAgent("pod-1"), makeAgent("pod-2")]}
        isAdmin={true}
        activeSelection={new Set()}
        setActiveSelection={() => {}}
        drainingSelection={new Set()}
        setDrainingSelection={() => {}}
        onShutdownClick={() => {}}
        onCancelShutdownClick={() => {}}
      />,
    );
    expect(screen.getByTestId("workspaces-agent-card-pod-1-select")).toBeInTheDocument();
    expect(screen.getByTestId("workspaces-agent-card-pod-2-select")).toBeInTheDocument();
  });

  it("admin sees per-card checkboxes on draining agents", () => {
    render(
      <AgentSections
        agents={[makeAgent("pod-1", "draining")]}
        isAdmin={true}
        activeSelection={new Set()}
        setActiveSelection={() => {}}
        drainingSelection={new Set()}
        setDrainingSelection={() => {}}
        onShutdownClick={() => {}}
        onCancelShutdownClick={() => {}}
      />,
    );
    expect(screen.getByTestId("workspaces-agent-card-pod-1-select")).toBeInTheDocument();
  });

  it("non-admin does not see per-card checkboxes", () => {
    render(<AgentSections agents={[makeAgent("pod-1")]} />);
    expect(screen.queryByTestId("workspaces-agent-card-pod-1-select")).not.toBeInTheDocument();
  });

  it("admin sees select-all checkbox in active section", () => {
    render(
      <AgentSections
        agents={[makeAgent("pod-1")]}
        isAdmin={true}
        activeSelection={new Set()}
        setActiveSelection={() => {}}
        drainingSelection={new Set()}
        setDrainingSelection={() => {}}
        onShutdownClick={() => {}}
        onCancelShutdownClick={() => {}}
      />,
    );
    expect(screen.getByTestId("workspaces-section-active-select-all")).toBeInTheDocument();
  });

  it("admin sees select-all checkbox in draining section", () => {
    render(
      <AgentSections
        agents={[makeAgent("pod-1", "draining")]}
        isAdmin={true}
        activeSelection={new Set()}
        setActiveSelection={() => {}}
        drainingSelection={new Set()}
        setDrainingSelection={() => {}}
        onShutdownClick={() => {}}
        onCancelShutdownClick={() => {}}
      />,
    );
    expect(screen.getByTestId("workspaces-section-draining-select-all")).toBeInTheDocument();
  });

  it("admin does NOT see select-all or card checkboxes in unconfigured section", () => {
    render(
      <AgentSections
        agents={[makeAgent("pod-1", "unconfigured")]}
        isAdmin={true}
        activeSelection={new Set()}
        setActiveSelection={() => {}}
        drainingSelection={new Set()}
        setDrainingSelection={() => {}}
        onShutdownClick={() => {}}
        onCancelShutdownClick={() => {}}
      />,
    );
    expect(screen.queryByTestId("workspaces-agent-card-pod-1-select")).not.toBeInTheDocument();
  });
});

describe("AgentSections — bulk action buttons", () => {
  it("Shut down button is disabled when active selection is empty", () => {
    render(
      <AgentSections
        agents={[makeAgent("pod-1")]}
        isAdmin={true}
        activeSelection={new Set()}
        setActiveSelection={() => {}}
        drainingSelection={new Set()}
        setDrainingSelection={() => {}}
        onShutdownClick={() => {}}
        onCancelShutdownClick={() => {}}
      />,
    );
    expect(screen.getByTestId("workspaces-section-active-shutdown")).toBeDisabled();
  });

  it("Shut down button is enabled when active selection is non-empty", () => {
    render(
      <AgentSections
        agents={[makeAgent("pod-1")]}
        isAdmin={true}
        activeSelection={new Set(["pod-1"])}
        setActiveSelection={() => {}}
        drainingSelection={new Set()}
        setDrainingSelection={() => {}}
        onShutdownClick={() => {}}
        onCancelShutdownClick={() => {}}
      />,
    );
    expect(screen.getByTestId("workspaces-section-active-shutdown")).toBeEnabled();
  });

  it("Cancel shutdown button is disabled when draining selection is empty", () => {
    render(
      <AgentSections
        agents={[makeAgent("pod-1", "draining")]}
        isAdmin={true}
        activeSelection={new Set()}
        setActiveSelection={() => {}}
        drainingSelection={new Set()}
        setDrainingSelection={() => {}}
        onShutdownClick={() => {}}
        onCancelShutdownClick={() => {}}
      />,
    );
    expect(screen.getByTestId("workspaces-section-draining-cancel-shutdown")).toBeDisabled();
  });

  it("Cancel shutdown button is enabled when draining selection is non-empty", () => {
    render(
      <AgentSections
        agents={[makeAgent("pod-1", "draining")]}
        isAdmin={true}
        activeSelection={new Set()}
        setActiveSelection={() => {}}
        drainingSelection={new Set(["pod-1"])}
        setDrainingSelection={() => {}}
        onShutdownClick={() => {}}
        onCancelShutdownClick={() => {}}
      />,
    );
    expect(screen.getByTestId("workspaces-section-draining-cancel-shutdown")).toBeEnabled();
  });

  it("clicking Shut down button calls onShutdownClick", async () => {
    const onShutdownClick = vi.fn();
    render(
      <AgentSections
        agents={[makeAgent("pod-1")]}
        isAdmin={true}
        activeSelection={new Set(["pod-1"])}
        setActiveSelection={() => {}}
        drainingSelection={new Set()}
        setDrainingSelection={() => {}}
        onShutdownClick={onShutdownClick}
        onCancelShutdownClick={() => {}}
      />,
    );
    await userEvent.click(screen.getByTestId("workspaces-section-active-shutdown"));
    expect(onShutdownClick).toHaveBeenCalledOnce();
  });
});

describe("AgentSections — select-all + per-card checkbox mechanics", () => {
  it("clicking select-all selects all agents", async () => {
    const setActiveSelection = vi.fn();
    render(
      <AgentSections
        agents={[makeAgent("pod-1"), makeAgent("pod-2")]}
        isAdmin={true}
        activeSelection={new Set()}
        setActiveSelection={setActiveSelection}
        drainingSelection={new Set()}
        setDrainingSelection={() => {}}
        onShutdownClick={() => {}}
        onCancelShutdownClick={() => {}}
      />,
    );
    await userEvent.click(screen.getByTestId("workspaces-section-active-select-all"));
    expect(setActiveSelection).toHaveBeenCalledWith(new Set(["pod-1", "pod-2"]));
  });

  it("clicking select-all when all selected deselects all", async () => {
    const setActiveSelection = vi.fn();
    render(
      <AgentSections
        agents={[makeAgent("pod-1"), makeAgent("pod-2")]}
        isAdmin={true}
        activeSelection={new Set(["pod-1", "pod-2"])}
        setActiveSelection={setActiveSelection}
        drainingSelection={new Set()}
        setDrainingSelection={() => {}}
        onShutdownClick={() => {}}
        onCancelShutdownClick={() => {}}
      />,
    );
    await userEvent.click(screen.getByTestId("workspaces-section-active-select-all"));
    expect(setActiveSelection).toHaveBeenCalledWith(new Set());
  });

  it("per-card checkbox adds agent to selection", async () => {
    const setActiveSelection = vi.fn();
    render(
      <AgentSections
        agents={[makeAgent("pod-1")]}
        isAdmin={true}
        activeSelection={new Set()}
        setActiveSelection={setActiveSelection}
        drainingSelection={new Set()}
        setDrainingSelection={() => {}}
        onShutdownClick={() => {}}
        onCancelShutdownClick={() => {}}
      />,
    );
    await userEvent.click(screen.getByTestId("workspaces-agent-card-pod-1-select"));
    expect(setActiveSelection).toHaveBeenCalledWith(new Set(["pod-1"]));
  });

  it("per-card checkbox removes agent from selection when already selected", async () => {
    const setActiveSelection = vi.fn();
    render(
      <AgentSections
        agents={[makeAgent("pod-1"), makeAgent("pod-2")]}
        isAdmin={true}
        activeSelection={new Set(["pod-1", "pod-2"])}
        setActiveSelection={setActiveSelection}
        drainingSelection={new Set()}
        setDrainingSelection={() => {}}
        onShutdownClick={() => {}}
        onCancelShutdownClick={() => {}}
      />,
    );
    await userEvent.click(screen.getByTestId("workspaces-agent-card-pod-1-select"));
    expect(setActiveSelection).toHaveBeenCalledWith(new Set(["pod-2"]));
  });
});
