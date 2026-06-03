import { http, HttpResponse } from "msw";

export const AUTH_PROVIDERS_FIXTURE = { providers: ["github", "test"] };

export const AUTH_ME_FIXTURE = {
  user: {
    id: "u1",
    display_name: "Jane Doe",
    primary_email: "j@x.test",
    emails: [],
  },
  memberships: [
    { org_id: "o1", slug: "acme", display_name: "Acme", role: "admin", handle: "jane" },
  ],
};

export const authHandlers = [
  http.get("/api/auth/providers", () => {
    return HttpResponse.json(AUTH_PROVIDERS_FIXTURE);
  }),

  http.get("/api/auth/me", () => {
    return HttpResponse.json(AUTH_ME_FIXTURE);
  }),

  http.post("/api/sso/discover", () => {
    return HttpResponse.json({ provider: "github" });
  }),
];
