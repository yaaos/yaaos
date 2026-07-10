import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { duration } from "./duration";

describe("duration", () => {
  it("formats under a minute as compact seconds", () => {
    expect(duration("2026-01-01T00:00:00Z", "2026-01-01T00:00:42Z")).toBe("42s");
  });

  it("formats minutes + seconds, zero-padded", () => {
    expect(duration("2026-01-01T00:00:00Z", "2026-01-01T00:03:12Z")).toBe("3m 12s");
  });

  it("formats hours + zero-padded minutes, dropping seconds", () => {
    expect(duration("2026-01-01T00:00:00Z", "2026-01-01T01:04:30Z")).toBe("1h 04m");
  });

  it("returns 0s for a zero-length span", () => {
    expect(duration("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")).toBe("0s");
  });

  describe("when end is null (in-flight — elapsed against now)", () => {
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ["Date"] });
      vi.setSystemTime(new Date("2026-01-01T00:00:30Z"));
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it("computes elapsed against the current time", () => {
      expect(duration("2026-01-01T00:00:00Z", null)).toBe("30s");
    });
  });
});
