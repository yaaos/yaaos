/**
 * Dialog copy + wiring tests for ShutdownDialog and CancelShutdownDialog.
 *
 * Verifies the exact title, body, and confirm-button copy mandated by the
 * design, and that the onConfirm callback fires on confirm and the dialog
 * closes on Cancel.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CancelShutdownDialog } from "../CancelShutdownDialog";
import { ShutdownDialog } from "../ShutdownDialog";

describe("ShutdownDialog", () => {
  it("renders title, body, and testids when open", () => {
    render(
      <ShutdownDialog
        open={true}
        onOpenChange={() => {}}
        onConfirm={() => {}}
        selectionCount={2}
      />,
    );
    expect(screen.getByTestId("workspaces-shutdown-dialog")).toBeInTheDocument();
    expect(screen.getByText("Shut down selected agents?")).toBeInTheDocument();
    expect(
      screen.getByText(
        "In-flight reviews finish before each agent exits. You can cancel before the agent exits.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByTestId("workspaces-shutdown-dialog-confirm")).toHaveTextContent("Shut down");
  });

  it("does not render when open=false", () => {
    render(
      <ShutdownDialog
        open={false}
        onOpenChange={() => {}}
        onConfirm={() => {}}
        selectionCount={0}
      />,
    );
    expect(screen.queryByTestId("workspaces-shutdown-dialog")).not.toBeInTheDocument();
  });

  it("calls onConfirm when confirm button is clicked", async () => {
    const onConfirm = vi.fn();
    render(
      <ShutdownDialog
        open={true}
        onOpenChange={() => {}}
        onConfirm={onConfirm}
        selectionCount={1}
      />,
    );
    await userEvent.click(screen.getByTestId("workspaces-shutdown-dialog-confirm"));
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("calls onOpenChange(false) when Cancel is clicked", async () => {
    const onOpenChange = vi.fn();
    render(
      <ShutdownDialog
        open={true}
        onOpenChange={onOpenChange}
        onConfirm={() => {}}
        selectionCount={1}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});

describe("CancelShutdownDialog", () => {
  it("renders title, body, and testids when open", () => {
    render(
      <CancelShutdownDialog
        open={true}
        onOpenChange={() => {}}
        onConfirm={() => {}}
        selectionCount={3}
      />,
    );
    expect(screen.getByTestId("workspaces-cancel-shutdown-dialog")).toBeInTheDocument();
    expect(screen.getByText("Cancel shutdown?")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Selected agents resume accepting new review work on their next intake cycle.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByTestId("workspaces-cancel-shutdown-dialog-confirm")).toHaveTextContent(
      "Cancel shutdown",
    );
  });

  it("does not render when open=false", () => {
    render(
      <CancelShutdownDialog
        open={false}
        onOpenChange={() => {}}
        onConfirm={() => {}}
        selectionCount={0}
      />,
    );
    expect(screen.queryByTestId("workspaces-cancel-shutdown-dialog")).not.toBeInTheDocument();
  });

  it("calls onConfirm when confirm button is clicked", async () => {
    const onConfirm = vi.fn();
    render(
      <CancelShutdownDialog
        open={true}
        onOpenChange={() => {}}
        onConfirm={onConfirm}
        selectionCount={1}
      />,
    );
    await userEvent.click(screen.getByTestId("workspaces-cancel-shutdown-dialog-confirm"));
    expect(onConfirm).toHaveBeenCalledOnce();
  });
});
