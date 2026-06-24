/**
 * AgentSections — unit tests.
 *
 * Covers: section partitioning by (state, lifecycle), sort order within a
 * section (last_heartbeat_at desc NULLS LAST), hide-empty rule, and the
 * state/lifecycle status-pair label rendered on each card.
 */

import type { AgentRow } from "@core/api/public/queries";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AgentSections } from "../AgentSections";

function makeAgent(override: Partial<AgentRow> & { instance_id: string }): AgentRow {
  return {
    id: override.instance_id,
    state: "reachable",
    lifecycle: "active",
    last_heartbeat_at: "2026-01-01T12:00:00Z",
    os: null,
    cpu_count: null,
    memory_bytes: null,
    claimed_workspace_count: 0,
    version: null,
    ...override,
  };
}

describe("AgentSections — section partitioning", () => {
  it("places reachable+active agent in Active section", () => {
    const agents = [makeAgent({ instance_id: "a1", state: "reachable", lifecycle: "active" })];
    render(<AgentSections agents={agents} />);
    expect(screen.getByTestId("workspaces-section-active")).toBeInTheDocument();
    expect(screen.getByTestId("workspaces-agent-card-a1")).toBeInTheDocument();
  });

  it("places stale+active agent in Active section", () => {
    const agents = [makeAgent({ instance_id: "a2", state: "stale", lifecycle: "active" })];
    render(<AgentSections agents={agents} />);
    expect(screen.getByTestId("workspaces-section-active")).toBeInTheDocument();
    expect(screen.getByTestId("workspaces-agent-card-a2")).toBeInTheDocument();
  });

  it("places reachable+draining agent in Draining section", () => {
    const agents = [makeAgent({ instance_id: "d1", state: "reachable", lifecycle: "draining" })];
    render(<AgentSections agents={agents} />);
    expect(screen.getByTestId("workspaces-section-draining")).toBeInTheDocument();
    expect(screen.getByTestId("workspaces-agent-card-d1")).toBeInTheDocument();
  });

  it("places reachable+unconfigured agent in Unconfigured section", () => {
    const agents = [
      makeAgent({ instance_id: "u1", state: "reachable", lifecycle: "unconfigured" }),
    ];
    render(<AgentSections agents={agents} />);
    expect(screen.getByTestId("workspaces-section-unconfigured")).toBeInTheDocument();
    expect(screen.getByTestId("workspaces-agent-card-u1")).toBeInTheDocument();
  });

  it("places offline+active agent in Inactive section (offline wins)", () => {
    const agents = [makeAgent({ instance_id: "i1", state: "offline", lifecycle: "active" })];
    render(<AgentSections agents={agents} />);
    expect(screen.getByTestId("workspaces-section-inactive")).toBeInTheDocument();
    expect(screen.getByTestId("workspaces-agent-card-i1")).toBeInTheDocument();
    expect(screen.queryByTestId("workspaces-section-active")).not.toBeInTheDocument();
  });

  it("places reachable+shutdown agent in Inactive section (shutdown wins)", () => {
    const agents = [makeAgent({ instance_id: "i2", state: "reachable", lifecycle: "shutdown" })];
    render(<AgentSections agents={agents} />);
    expect(screen.getByTestId("workspaces-section-inactive")).toBeInTheDocument();
    expect(screen.getByTestId("workspaces-agent-card-i2")).toBeInTheDocument();
    expect(screen.queryByTestId("workspaces-section-active")).not.toBeInTheDocument();
  });

  it("places offline+draining agent in Inactive section (offline wins over draining)", () => {
    const agents = [makeAgent({ instance_id: "i3", state: "offline", lifecycle: "draining" })];
    render(<AgentSections agents={agents} />);
    expect(screen.getByTestId("workspaces-section-inactive")).toBeInTheDocument();
    expect(screen.queryByTestId("workspaces-section-draining")).not.toBeInTheDocument();
  });
});

describe("AgentSections — hide-empty rule", () => {
  it("does not render Active section when no active agents", () => {
    const agents = [
      makeAgent({ instance_id: "u1", state: "reachable", lifecycle: "unconfigured" }),
    ];
    render(<AgentSections agents={agents} />);
    expect(screen.queryByTestId("workspaces-section-active")).not.toBeInTheDocument();
    expect(screen.getByTestId("workspaces-section-unconfigured")).toBeInTheDocument();
  });

  it("renders only the sections that have agents", () => {
    const agents = [
      makeAgent({ instance_id: "d1", state: "reachable", lifecycle: "draining" }),
      makeAgent({ instance_id: "i1", state: "offline", lifecycle: "draining" }),
    ];
    render(<AgentSections agents={agents} />);
    expect(screen.queryByTestId("workspaces-section-active")).not.toBeInTheDocument();
    expect(screen.getByTestId("workspaces-section-draining")).toBeInTheDocument();
    expect(screen.queryByTestId("workspaces-section-unconfigured")).not.toBeInTheDocument();
    expect(screen.getByTestId("workspaces-section-inactive")).toBeInTheDocument();
  });

  it("renders nothing when agents list is empty", () => {
    render(<AgentSections agents={[]} />);
    expect(screen.queryByTestId("workspaces-section-active")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workspaces-section-draining")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workspaces-section-unconfigured")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workspaces-section-inactive")).not.toBeInTheDocument();
  });
});

describe("AgentSections — sort order within section (last_heartbeat_at desc NULLS LAST)", () => {
  it("sorts agents with a newer heartbeat before older within the same section", () => {
    const agents = [
      makeAgent({
        instance_id: "old",
        state: "reachable",
        lifecycle: "active",
        last_heartbeat_at: "2026-01-01T10:00:00Z",
      }),
      makeAgent({
        instance_id: "new",
        state: "reachable",
        lifecycle: "active",
        last_heartbeat_at: "2026-01-01T12:00:00Z",
      }),
    ];
    render(<AgentSections agents={agents} />);
    // Filter out the -status span testids; we only want the card container testids.
    const cards = screen
      .getAllByTestId(/^workspaces-agent-card-/)
      .filter((el) => !el.getAttribute("data-testid")?.endsWith("-status"));
    // "new" (12:00) should appear before "old" (10:00)
    expect(cards[0]).toHaveAttribute("data-testid", "workspaces-agent-card-new");
    expect(cards[1]).toHaveAttribute("data-testid", "workspaces-agent-card-old");
  });

  it("sorts agents with null last_heartbeat_at to the end of the section", () => {
    const agents = [
      makeAgent({
        instance_id: "null-hb",
        state: "reachable",
        lifecycle: "active",
        last_heartbeat_at: null,
      }),
      makeAgent({
        instance_id: "has-hb",
        state: "reachable",
        lifecycle: "active",
        last_heartbeat_at: "2026-01-01T12:00:00Z",
      }),
    ];
    render(<AgentSections agents={agents} />);
    const cards = screen
      .getAllByTestId(/^workspaces-agent-card-/)
      .filter((el) => !el.getAttribute("data-testid")?.endsWith("-status"));
    expect(cards[0]).toHaveAttribute("data-testid", "workspaces-agent-card-has-hb");
    expect(cards[1]).toHaveAttribute("data-testid", "workspaces-agent-card-null-hb");
  });
});

describe("AgentSections — status pair formatting", () => {
  it("renders Online / Active for reachable+active", () => {
    const agents = [makeAgent({ instance_id: "a1", state: "reachable", lifecycle: "active" })];
    render(<AgentSections agents={agents} />);
    expect(screen.getByTestId("workspaces-agent-card-a1-status")).toHaveTextContent(
      "Online / Active",
    );
  });

  it("renders Stale / Draining for stale+draining", () => {
    const agents = [makeAgent({ instance_id: "d1", state: "stale", lifecycle: "draining" })];
    render(<AgentSections agents={agents} />);
    expect(screen.getByTestId("workspaces-agent-card-d1-status")).toHaveTextContent(
      "Stale / Draining",
    );
  });

  it("renders Online / Unconfigured for reachable+unconfigured", () => {
    const agents = [
      makeAgent({ instance_id: "u1", state: "reachable", lifecycle: "unconfigured" }),
    ];
    render(<AgentSections agents={agents} />);
    expect(screen.getByTestId("workspaces-agent-card-u1-status")).toHaveTextContent(
      "Online / Unconfigured",
    );
  });

  it("renders Offline / Shutdown for offline+shutdown", () => {
    const agents = [makeAgent({ instance_id: "s1", state: "offline", lifecycle: "shutdown" })];
    render(<AgentSections agents={agents} />);
    expect(screen.getByTestId("workspaces-agent-card-s1-status")).toHaveTextContent(
      "Offline / Shutdown",
    );
  });
});
