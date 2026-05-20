import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PluginPicker } from "../PluginPicker";
import type { PluginMeta } from "../types";

const PLUGINS: PluginMeta[] = [
  {
    id: "github",
    type: "vcs",
    display_name: "GitHub",
    description: "GitHub App integration",
    docs_url: "https://docs.github.com",
  },
  {
    id: "demo",
    type: "vcs",
    display_name: "Demo",
    description: null,
    docs_url: null,
  },
];

describe("PluginPicker", () => {
  it("renders one card per plugin with description + docs link when present", () => {
    render(<PluginPicker plugins={PLUGINS} onPick={() => {}} />);
    expect(screen.getByTestId("plugin-picker-card-github")).toBeInTheDocument();
    expect(screen.getByTestId("plugin-picker-card-demo")).toBeInTheDocument();
    expect(screen.getByTestId("plugin-picker-docs-github")).toBeInTheDocument();
    expect(screen.queryByTestId("plugin-picker-docs-demo")).toBeNull();
  });

  it("calls onPick when Add is clicked", () => {
    const onPick = vi.fn();
    render(<PluginPicker plugins={PLUGINS} onPick={onPick} />);
    fireEvent.click(screen.getByTestId("plugin-picker-add-github"));
    expect(onPick).toHaveBeenCalledWith(PLUGINS[0]);
  });

  it("disables Add and shows Installed when isInstalled matches", () => {
    render(
      <PluginPicker plugins={PLUGINS} onPick={() => {}} isInstalled={(p) => p.id === "github"} />,
    );
    expect(screen.getByTestId("plugin-picker-add-github")).toBeDisabled();
    expect(screen.getByTestId("plugin-picker-add-github")).toHaveTextContent("Installed");
    expect(screen.getByTestId("plugin-picker-add-demo")).not.toBeDisabled();
  });

  it("renders loading / error / empty states", () => {
    const { rerender } = render(<PluginPicker plugins={[]} loading onPick={() => {}} />);
    expect(screen.getByTestId("plugin-picker-loading")).toBeInTheDocument();
    rerender(<PluginPicker plugins={[]} error={new Error("boom")} onPick={() => {}} />);
    expect(screen.getByTestId("plugin-picker-error")).toHaveTextContent("boom");
    rerender(<PluginPicker plugins={[]} onPick={() => {}} />);
    expect(screen.getByTestId("plugin-picker-empty")).toBeInTheDocument();
  });
});
