/**
 * Pure-function unit tests for the shutdown / cancel-shutdown toast message
 * computation. These verify all three outcome paths (all-success, mixed,
 * all-no-op) and the already_shutdown suffix for the cancel no-op case.
 */

import { cancelShutdownToastMessage, shutdownToastMessage } from "@core/api/public/queries";
import { describe, expect, it } from "vitest";

describe("shutdownToastMessage", () => {
  it("all success — plural", () => {
    expect(
      shutdownToastMessage([
        { agent_id: "a1", outcome: "draining" },
        { agent_id: "a2", outcome: "draining" },
      ]),
    ).toBe("Shut down 2 agents.");
  });

  it("all success — singular", () => {
    expect(shutdownToastMessage([{ agent_id: "a1", outcome: "draining" }])).toBe(
      "Shut down 1 agent.",
    );
  });

  it("mixed — some succeeded, some already draining", () => {
    expect(
      shutdownToastMessage([
        { agent_id: "a1", outcome: "draining" },
        { agent_id: "a2", outcome: "already_draining" },
      ]),
    ).toBe("Shut down 1 of 2 agents; 1 were already draining, shut down, or not found.");
  });

  it("mixed — some succeeded, some already_shutdown, some not_found", () => {
    expect(
      shutdownToastMessage([
        { agent_id: "a1", outcome: "draining" },
        { agent_id: "a2", outcome: "already_shutdown" },
        { agent_id: "a3", outcome: "not_found" },
      ]),
    ).toBe("Shut down 1 of 3 agents; 2 were already draining, shut down, or not found.");
  });

  it("all no-op — all already draining or shutdown", () => {
    expect(
      shutdownToastMessage([
        { agent_id: "a1", outcome: "already_draining" },
        { agent_id: "a2", outcome: "already_shutdown" },
      ]),
    ).toBe("No agents were shut down — all were already draining, shut down, or not found.");
  });
});

describe("cancelShutdownToastMessage", () => {
  it("all success — plural", () => {
    expect(
      cancelShutdownToastMessage([
        { agent_id: "a1", outcome: "active" },
        { agent_id: "a2", outcome: "active" },
      ]),
    ).toBe("Canceled shutdown for 2 agents.");
  });

  it("all success — singular", () => {
    expect(cancelShutdownToastMessage([{ agent_id: "a1", outcome: "active" }])).toBe(
      "Canceled shutdown for 1 agent.",
    );
  });

  it("mixed — some active, some not_draining", () => {
    expect(
      cancelShutdownToastMessage([
        { agent_id: "a1", outcome: "active" },
        { agent_id: "a2", outcome: "not_draining" },
      ]),
    ).toBe(
      "Canceled shutdown for 1 of 2 agents; 1 were not draining, already shut down, or not found.",
    );
  });

  it("mixed — some active, some not_found, some already_shutdown", () => {
    expect(
      cancelShutdownToastMessage([
        { agent_id: "a1", outcome: "active" },
        { agent_id: "a2", outcome: "not_found" },
        { agent_id: "a3", outcome: "already_shutdown" },
      ]),
    ).toBe(
      "Canceled shutdown for 1 of 3 agents; 2 were not draining, already shut down, or not found.",
    );
  });

  it("all no-op — none draining, no already_shutdown majority", () => {
    expect(
      cancelShutdownToastMessage([
        { agent_id: "a1", outcome: "not_draining" },
        { agent_id: "a2", outcome: "not_draining" },
      ]),
    ).toBe("No agents were canceled — already shut down or not draining.");
  });

  it("all no-op — already_shutdown ≥ 50% → appends restart hint", () => {
    expect(
      cancelShutdownToastMessage([
        { agent_id: "a1", outcome: "already_shutdown" },
        { agent_id: "a2", outcome: "already_shutdown" },
      ]),
    ).toBe(
      "No agents were canceled — already shut down or not draining. Restart the deployment to bring shut-down agents back.",
    );
  });

  it("all no-op — majority already_shutdown (> 50%) → appends restart hint", () => {
    expect(
      cancelShutdownToastMessage([
        { agent_id: "a1", outcome: "already_shutdown" },
        { agent_id: "a2", outcome: "already_shutdown" },
        { agent_id: "a3", outcome: "not_draining" },
      ]),
    ).toBe(
      "No agents were canceled — already shut down or not draining. Restart the deployment to bring shut-down agents back.",
    );
  });

  it("all no-op — exactly 50% already_shutdown → appends restart hint", () => {
    expect(
      cancelShutdownToastMessage([
        { agent_id: "a1", outcome: "already_shutdown" },
        { agent_id: "a2", outcome: "not_draining" },
      ]),
    ).toBe(
      "No agents were canceled — already shut down or not draining. Restart the deployment to bring shut-down agents back.",
    );
  });
});
