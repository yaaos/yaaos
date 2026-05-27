import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  // JSON reporter feeds RWX's Tests tab (Captain parses Playwright JSON).
  reporter: [["list"], ["json", { outputFile: "tmp/playwright.json" }]],
  use: {
    baseURL: process.env.YAAOS_BASE_URL ?? "http://localhost:58080",
    extraHTTPHeaders: { Accept: "application/json,text/html" },
    // 30s — covers /api/testing/reset under heavy worker background load
    // (reaper + heartbeat hit every 1s in the test compose; on Linux CI
    // they keep DB connections busy enough that the per-table DELETE in
    // truncate_all_tables can take several seconds).
    actionTimeout: 30_000,
    navigationTimeout: 15_000,
  },
});
