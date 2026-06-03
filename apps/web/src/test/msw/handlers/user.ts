import { http, HttpResponse } from "msw";

export const USER_ME_FIXTURE = {
  user_id: "u1",
  display_name: "Jane Doe",
  github_username: null as string | null,
  emails: [
    { id: "e1", email: "jane@x.test", is_primary: true, verified: true },
    { id: "e2", email: "alt@x.test", is_primary: false, verified: true },
  ],
  memberships: [
    {
      org_id: "00000000-0000-0000-0000-000000000001",
      slug: "acme",
      display_name: "Acme",
      role: "owner",
      handle: "jane",
    },
    {
      org_id: "00000000-0000-0000-0000-000000000002",
      slug: "beta",
      display_name: "Beta",
      role: "builder",
      handle: "jdoe",
    },
  ],
};

export const userHandlers = [
  http.get("/api/user/me", () => {
    return HttpResponse.json(USER_ME_FIXTURE);
  }),

  http.patch("/api/user/me", async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({
      ...USER_ME_FIXTURE,
      display_name: (body.display_name as string) ?? USER_ME_FIXTURE.display_name,
    });
  }),

  http.patch("/api/memberships/me/:orgId", () => {
    return HttpResponse.json({});
  }),

  http.post("/api/auth/logout-all", () => {
    return HttpResponse.json({});
  }),

  http.post("/api/auth/totp/enroll", () => {
    return HttpResponse.json({
      seed: "JBSWY3DPEHPK3PXP",
      otpauth_uri: "otpauth://totp/yaaos:jane?secret=JBSWY3DPEHPK3PXP&issuer=yaaos",
    });
  }),
];
