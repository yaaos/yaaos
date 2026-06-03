import { http, HttpResponse } from "msw";

export const ORGS_MINE_FIXTURE = [
  { id: "o1", slug: "acme", name: "Acme", role: "admin", last_used_at: null },
];

export const CONFIG_STATUS_FIXTURE = {
  configured: true,
  missing: [],
  admins: [],
};

export const orgsHandlers = [
  http.get("/api/orgs/mine", () => {
    return HttpResponse.json(ORGS_MINE_FIXTURE);
  }),

  http.post("/api/orgs", async ({ request }) => {
    const body = (await request.json()) as { name: string; slug: string };
    return HttpResponse.json({ id: "o-new", slug: body.slug, name: body.name, role: "admin" });
  }),

  http.get("/api/orgs/config-status", () => {
    return HttpResponse.json(CONFIG_STATUS_FIXTURE);
  }),
];
