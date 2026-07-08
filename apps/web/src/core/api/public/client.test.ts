import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "../../../test/msw/server";
import { apiFetch } from "./client";

describe("apiFetch", () => {
  it("resolves 204 with no body to undefined", async () => {
    server.use(http.delete("/api/test/empty-204", () => new HttpResponse(null, { status: 204 })));
    await expect(apiFetch("/api/test/empty-204", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it("resolves a bare 200 with an empty body to undefined (no JSON parse error)", async () => {
    server.use(http.put("/api/test/empty-200", () => new HttpResponse(null, { status: 200 })));
    await expect(apiFetch("/api/test/empty-200", { method: "PUT" })).resolves.toBeUndefined();
  });

  it("still parses a 200 response that does carry a JSON body", async () => {
    server.use(http.get("/api/test/json-200", () => HttpResponse.json({ ok: true })));
    await expect(apiFetch("/api/test/json-200")).resolves.toEqual({ ok: true });
  });
});
