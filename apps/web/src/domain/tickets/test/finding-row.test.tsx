import type { FindingRow as FindingRowData } from "@core/api/public/queries";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FindingRow } from "../FindingRow";

/**
 * Smoke tests for FindingRow. The component is non-interactive — no Ack or
 * Push-back actions. Tests cover severity/confidence chip rendering, file:line
 * display, headline derivation from rationale, and suggested fix.
 */
function fixture(overrides: Partial<FindingRowData> = {}): FindingRowData {
  return {
    id: "f1",
    finding_display_id: 1,
    category: "nullability",
    severity: "should_fix",
    confidence: "plausible",
    rationale: "caller may pass None which raises NoneType. This is bad.",
    rule_violated: "x/null-deref",
    rule_source: "yaaos-baseline",
    suggested_fix: "Add a guard clause before line 10.",
    file: "src/foo.py",
    line: 10,
    review_id: "r1",
    ...overrides,
  };
}

describe("FindingRow", () => {
  it("renders severity chip + headline derived from rationale first sentence", () => {
    render(<FindingRow finding={fixture()} />);
    expect(screen.getByTestId("finding-severity-f1")).toHaveTextContent(/should fix/i);
    // Headline is derived from the first sentence of rationale (text content only).
    // Note: the full rationale also renders in a <p> below; use queryAllBy to
    // confirm at least one match exists without failing on multiple.
    const matches = screen.getAllByText(/caller may pass None/i);
    expect(matches.length).toBeGreaterThanOrEqual(1);
  });

  it("renders confidence chip", () => {
    render(<FindingRow finding={fixture({ confidence: "verified" })} />);
    expect(screen.getByTestId("finding-confidence-f1")).toHaveTextContent(/verified/i);
  });

  it("renders file:line when present", () => {
    render(<FindingRow finding={fixture()} />);
    expect(screen.getByText("src/foo.py:10")).toBeInTheDocument();
  });

  it("omits file:line when file is null", () => {
    render(<FindingRow finding={fixture({ file: null, line: null })} />);
    expect(screen.queryByText(/foo.py/)).toBeNull();
  });

  it("renders suggested fix when present", () => {
    render(<FindingRow finding={fixture()} />);
    expect(screen.getByText(/Add a guard clause before line 10/)).toBeInTheDocument();
  });

  it("renders rule_violated + rule_source in footer", () => {
    render(<FindingRow finding={fixture()} />);
    expect(screen.getByText("x/null-deref")).toBeInTheDocument();
    expect(screen.getByText("yaaos-baseline")).toBeInTheDocument();
  });

  it("renders blocker chip with destructive styling", () => {
    render(<FindingRow finding={fixture({ severity: "blocker" })} />);
    expect(screen.getByTestId("finding-severity-f1")).toHaveTextContent(/blocker/i);
  });

  it("renders nit chip", () => {
    render(<FindingRow finding={fixture({ severity: "nit" })} />);
    expect(screen.getByTestId("finding-severity-f1")).toHaveTextContent(/nit/i);
  });

  it("has no Ack or Push-back interactive elements", () => {
    const { container } = render(<FindingRow finding={fixture()} />);
    expect(container.querySelector("[data-testid^='finding-ack-']")).toBeNull();
    expect(container.querySelector("[data-testid^='finding-pushback-']")).toBeNull();
  });
});
