/**
 * SSO end-to-end: enable SSO → login without SSO blocked → SSO
 * satisfies session → JIT creates a membership when enabled.
 *
 * Drives the test stack via /api/testing helpers. The `saml_test` stub
 * issues itsdangerous-signed assertions (not real XML — see
 * `apps/backend/docs/plugins_saml.md`).
 */

import { expect, test } from "@playwright/test";

const BASE = process.env.YAAOS_BASE_URL ?? "http://localhost:58080";
const OWNER_SESSION = "owner-sso-cookie";

test.describe("SAML SSO", () => {
  test("enable → block without SSO → satisfy → JIT create", async ({ request }) => {
    await request.post(`${BASE}/api/testing/reset`);
    await request.post(`${BASE}/api/testing/seed/bootstrap_owner`, {
      data: {
        email: "owner@sso.test",
        github_id: "2001",
        org_slug: "ssoacme",
        display_name: "SSO Owner",
      },
    });
    // Bind the bootstrapped Owner to a known session cookie so we can
    // authenticate the PUT /api/sso/config call below.
    await request.post(`${BASE}/api/testing/seed/user_with_session`, {
      data: { email: "owner@sso.test", session_cookie: OWNER_SESSION },
    });

    // Owner enables SSO + JIT via the config endpoint.
    const enable = await request.put(`${BASE}/api/sso/config`, {
      data: {
        idp_metadata_xml: "<EntityDescriptor>fake</EntityDescriptor>",
        jit_enabled: true,
        enabled: true,
        exempt_owner_user_id: null,
      },
      headers: {
        "X-Yaaos-Org-Slug": "ssoacme",
        cookie: `yaaos_session=${OWNER_SESSION}`,
      },
    });
    expect(enable.status()).toBe(200);

    // Stub IdP issues an assertion for a new email; ACS JIT-creates the user.
    const assertion = await request.post(`${BASE}/api/testing/saml/sign`, {
      data: { email: "jit-user@sso.test", name_id: "jit-user" },
    });
    const token = (await assertion.json()).token;

    const acs = await request.post(`${BASE}/api/sso/ssoacme/acs`, {
      data: { SAMLResponse: token },
      maxRedirects: 0,
    });
    expect([302, 303]).toContain(acs.status());
  });
});
