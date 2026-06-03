import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import babel from "@rolldown/plugin-babel";
import react, { reactCompilerPreset } from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [tailwindcss(), react(), babel({ presets: [reactCompilerPreset()] })],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      "@core": path.resolve(__dirname, "./src/core"),
      "@domain": path.resolve(__dirname, "./src/domain"),
      "@shared": path.resolve(__dirname, "./src/shared"),
    },
  },
  build: {
    // Hidden source maps: emitted to the build output dir alongside the
    // minified bundle but not referenced from the bundle. Uploaded to Dash0
    // on deploy (keyed by release hash) for server-side symbolication of
    // minified client error stacks. Never served to the browser.
    sourcemap: "hidden",
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      // Forward API + OpenAPI + webhooks to FastAPI on :8080 during dev.
      // `/webhooks` is the same-origin path GitHub posts to; the SPA
      // never hits it directly but proxying keeps prod/dev shape identical.
      "/api": "http://localhost:8080",
      "/openapi.json": "http://localhost:8080",
      "/webhooks": "http://localhost:8080",
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    // Captain (RWX) requires source locations on each task to parse the
    // vitest JSON reporter output.
    includeTaskLocation: true,
  },
});
