import { http, HttpResponse } from "msw";

// BYOK handlers
export const BYOK_FIXTURE = [
  {
    provider: "anthropic",
    status: "not_set",
    last_validated_at: null,
    last_used_at: null,
    updated_at: null,
  },
];

// Coding agents handlers
export const CODING_AGENTS_FIXTURE = [
  {
    plugin_id: "claude_code",
    settings: {},
    created_at: "2026-05-20T00:00:00Z",
    updated_at: "2026-05-20T00:00:00Z",
  },
];

// VCS handlers
export const VCS_STATE_FIXTURE = {
  plugin_id: null as string | null,
  settings: {},
};

export const INTEGRATIONS_FIXTURE = [
  {
    provider: "linear",
    status: "not_set",
    enabled: null,
    upstream_identity: null,
    last_validated_at: null,
    last_refresh_failed_at: null,
    allowed_tools: [],
  },
];

export const PLUGINS_AVAILABLE_VCS = [
  {
    id: "github",
    type: "vcs",
    display_name: "GitHub",
    description: "GitHub App integration",
    docs_url: "https://docs.github.com",
  },
];

export const PLUGINS_AVAILABLE_CODING_AGENT = [
  {
    id: "claude_code",
    type: "coding_agent",
    display_name: "Claude Code",
    description: "Anthropic CLI",
    docs_url: null,
  },
];

export const GITHUB_INSTALLATION_FIXTURE = {
  app_configured: true,
  installed: false,
  slug: null,
  account_login: null,
  install_external_id: null,
  installed_at: null,
  installations_url: null,
};

export const GITHUB_REPOS_FIXTURE = {
  total_count: 0,
  repositories: [],
};

export const ORG_SETTINGS_FIXTURE = {
  slug: "acme",
  session_timeout_override: null,
  registered_iam_arn: null,
};

export const settingsHandlers = [
  // BYOK
  http.get("/api/api-keys", () => HttpResponse.json(BYOK_FIXTURE)),
  http.post("/api/api-keys/:provider", () => HttpResponse.json({ status: "ok" })),
  http.post("/api/api-keys/:provider/validate", () => HttpResponse.json({ valid: true })),
  http.delete("/api/api-keys/:provider", () => HttpResponse.json({ removed: true })),

  // Coding agents
  http.get("/api/coding-agents", () => HttpResponse.json(CODING_AGENTS_FIXTURE)),
  http.post("/api/coding-agents", async ({ request }) => {
    const body = (await request.json()) as { plugin_id: string; settings: Record<string, unknown> };
    return HttpResponse.json({
      plugin_id: body.plugin_id,
      settings: body.settings,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
  }),
  http.delete("/api/coding-agents/:pluginId", () => HttpResponse.json({ removed: true })),
  http.patch("/api/coding-agents/:pluginId", async ({ request, params }) => {
    const body = (await request.json()) as { settings: Record<string, unknown> };
    return HttpResponse.json({
      plugin_id: params.pluginId,
      settings: body.settings,
      created_at: "2026-05-20T00:00:00Z",
      updated_at: new Date().toISOString(),
    });
  }),

  // VCS
  http.get("/api/vcs", () => HttpResponse.json(VCS_STATE_FIXTURE)),
  http.post("/api/vcs", async ({ request }) => {
    const body = (await request.json()) as { plugin_id: string };
    return HttpResponse.json({
      state: { plugin_id: body.plugin_id, settings: {} },
      install_url: null,
    });
  }),
  http.delete("/api/vcs", () => HttpResponse.json({ plugin_id: null, settings: {} })),
  http.post("/api/github/install/start", () =>
    HttpResponse.json({ redirect_url: "https://github.com/install" }),
  ),

  // Integrations
  http.get("/api/mcp-proxy", () => HttpResponse.json(INTEGRATIONS_FIXTURE)),
  http.patch("/api/mcp-proxy/:provider", async ({ request, params }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({
      provider: params.provider,
      status: "configured",
      enabled: body.enabled ?? true,
      upstream_identity: null,
      last_validated_at: null,
      last_refresh_failed_at: null,
      allowed_tools: (body.allowed_tools as string[]) ?? [],
    });
  }),
  http.delete("/api/mcp-proxy/:provider", () => HttpResponse.json({ removed: true })),
  http.post("/api/mcp-proxy/:provider/validate", () => HttpResponse.json({ valid: true })),

  // Plugins available
  http.get("/api/plugins/available", ({ request }) => {
    const type = new URL(request.url).searchParams.get("type");
    if (type === "coding_agent")
      return HttpResponse.json({ plugins: PLUGINS_AVAILABLE_CODING_AGENT });
    return HttpResponse.json({ plugins: PLUGINS_AVAILABLE_VCS });
  }),

  // GitHub installation + repos
  http.get("/api/github/installation", () => HttpResponse.json(GITHUB_INSTALLATION_FIXTURE)),
  http.get("/api/github/repositories", () => HttpResponse.json(GITHUB_REPOS_FIXTURE)),

  // Org settings
  http.get("/api/orgs", () => HttpResponse.json(ORG_SETTINGS_FIXTURE)),
  http.patch("/api/orgs", async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ ...ORG_SETTINGS_FIXTURE, ...body });
  }),
];
