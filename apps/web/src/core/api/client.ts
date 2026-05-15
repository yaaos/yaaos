/**
 * Typed API client.
 *
 * For the skeleton, the OpenAPI schema lives at the backend's /openapi.json
 * and types would normally be generated into `./generated/openapi.d.ts`. Until
 * `pnpm generate:api` runs against a live backend, we declare the /api/health
 * shape inline.
 */

import createClient from "openapi-fetch";

export type HealthResponse = {
  status: "ok" | "degraded";
  db_ok: boolean;
  version: string;
};

// Minimal typed paths until the generator wires up. Replace with:
//   import type { paths } from "./generated/openapi";
//   export const apiClient = createClient<paths>({ baseUrl: "/" });
type Paths = {
  "/api/health": {
    get: {
      responses: {
        200: { content: { "application/json": HealthResponse } };
      };
    };
  };
};

// Resolve to the page's origin so the URL is always absolute (jsdom and
// browsers both need this; relative baseUrls fail in jsdom). In dev, Vite's
// proxy forwards /api/* to FastAPI on :8080. In prod, FastAPI serves both.
const baseUrl = typeof window !== "undefined" ? window.location.origin : "http://localhost";

export const apiClient = createClient<Paths>({ baseUrl });
