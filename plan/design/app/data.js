// app/data.js — mock data for yaaof
window.YAAOF_DATA = (function () {
  const now = Date.now();
  const SEC = 1000, MIN = 60_000, HR = 60 * MIN, DAY = 24 * HR;

  const repos = [
    { id: 'r1', name: 'acme/web',         plugin: 'github', lang: 'TypeScript', status: 'active',          lessons_count: 4, last_review_age_ms: 4 * MIN },
    { id: 'r2', name: 'acme/api',         plugin: 'github', lang: 'Python',     status: 'active',          lessons_count: 3, last_review_age_ms: 12 * MIN },
    { id: 'r3', name: 'acme/infra',       plugin: 'github', lang: 'HCL',        status: 'install-missing', lessons_count: 0, last_review_age_ms: null },
    { id: 'r4', name: 'acme/mobile',      plugin: 'github', lang: 'Swift',      status: 'active',          lessons_count: 1, last_review_age_ms: 3 * HR },
    { id: 'r5', name: 'acme/ml-pipeline', plugin: 'github', lang: 'Python',     status: 'unreachable',     lessons_count: 0, last_review_age_ms: 2 * DAY },
  ];

  const agents = [
    {
      id: 'arch', name: 'Architecture', short: 'arch',
      coding_agent: 'claude-code', is_built_in: true,
      hue: 235,
      prompt:
`You are a senior staff engineer reviewing a pull request for architectural soundness.

Focus on:
- Module boundaries and dependency direction
- Coupling and cohesion of new code with existing surfaces
- Whether new abstractions earn their complexity
- Long-term maintainability and extension points
- Data ownership and state placement

Avoid:
- Style nits (the style agent handles those)
- Security findings (the security agent handles those)

Output your verdict as APPROVED, CHANGES_REQUESTED, or COMMENT.
Provide structured findings: file:line, severity, title, body.

Apply the lessons listed below before drafting your review.`,
      applied_to: 412,
    },
    {
      id: 'sec', name: 'Security', short: 'sec',
      coding_agent: 'claude-code', is_built_in: true,
      hue: 25,
      prompt:
`You are a security engineer reviewing a pull request.

Focus on:
- Authentication and authorization correctness
- Input validation, injection, deserialization
- Secret handling and credential exposure
- Cryptography misuse
- Dependency vulnerabilities (direct and transitive)

Be specific: cite the file and line, name the threat model, and either suggest the fix or explain why the code is safe.

Output your verdict as APPROVED, CHANGES_REQUESTED, or COMMENT.`,
      applied_to: 388,
    },
    {
      id: 'style', name: 'Style', short: 'style',
      coding_agent: 'claude-code', is_built_in: true,
      hue: 150,
      prompt:
`You are a style and consistency reviewer.

Focus on:
- Naming conventions used elsewhere in this codebase
- Local idioms (helper functions, error patterns) that should be reused
- Code organization within the file (where the new function/class should live)
- Comment quality (where, not whether)

Severity defaults to 'nit'. Reserve 'must-fix' for actual style errors that will fail lint.`,
      applied_to: 421,
    },
  ];

  const lessons = {
    r1: [
      { id: 'l1', title: "Don't suggest mocks in tests",
        body: "Our test pattern is to use lightweight factories from `test/factories/`, not mocks. When you see `jest.mock()`, suggest the factory equivalent. Mocks make our tests brittle to refactors and we've removed almost all of them this quarter.",
        source_pr: '#2418', created: now - 8 * DAY, applied_count: 217 },
      { id: 'l2', title: 'Use @/lib/queries for server state',
        body: 'Never inline `fetch()` or raw `useEffect`-based fetching. We standardized on TanStack Query wrappers in `@/lib/queries/`. Adding a new endpoint? Add a query hook there first.',
        source_pr: '#2401', created: now - 14 * DAY, applied_count: 412 },
      { id: 'l3', title: 'Storybook stories are mandatory for shadcn variants',
        body: 'Any new variant of a shadcn primitive needs a Storybook story showing all states (default/hover/disabled/loading). The CI check will catch this, but flag it early.',
        source_pr: '#2390', created: now - 22 * DAY, applied_count: 188 },
      { id: 'l4', title: 'No new global Zustand slices',
        body: 'We are consolidating on URL state and TanStack Query for server state. If you find yourself reaching for a new Zustand slice, push back — usually the state belongs in the URL or the query cache.',
        source_pr: '#2377', created: now - 30 * DAY, applied_count: 76 },
    ],
    r2: [
      { id: 'l5', title: 'All endpoints return Problem Details on error',
        body: 'RFC 7807. Use the `problem_response()` helper in `app/errors.py`. Do not return plain `{"error": "..."}` JSON — the client deserializer will choke on it.',
        source_pr: '#1188', created: now - 5 * DAY, applied_count: 142 },
      { id: 'l6', title: 'SQLAlchemy: use `select()`, not `query()`',
        body: 'We are fully on 2.0-style. New code that uses the legacy `Query` API gets rejected. Match the patterns in `app/repositories/`.',
        source_pr: '#1170', created: now - 11 * DAY, applied_count: 98 },
      { id: 'l7', title: 'Pydantic v2 model_config, not Config class',
        body: 'Inner `class Config:` is v1 idiom. We are on v2 — use `model_config = ConfigDict(...)`.',
        source_pr: '#1162', created: now - 19 * DAY, applied_count: 64 },
    ],
    r3: [],
    r4: [
      { id: 'l8', title: 'SwiftUI previews must include dark mode',
        body: 'Every `#Preview` macro needs at least the light + dark variant. Several screens shipped looking broken in dark mode because the preview only covered light.',
        source_pr: '#812', created: now - 9 * DAY, applied_count: 41 },
    ],
    r5: [],
  };

  const tickets = [
    {
      id: 't1', number: 2431, repo_id: 'r1', repo: 'acme/web',
      title: 'Add real-time SSE handling to ticket detail',
      pr: { number: 2431, state: 'open', is_draft: false, author: 'rachel-cohen',
            head: 'feat/ticket-sse', base: 'main', additions: 247, deletions: 38, files: 11,
            html_url: 'https://github.com/acme/web/pull/2431' },
      status: 'review',
      kind: 'new feature',
      source: 'github_pr',
      actor: 'rachel-cohen',
      created: now - 4 * MIN,
      updated: now - 8 * SEC,
      verdicts: { arch: 'running', sec: 'queued', style: 'APPROVED' },
      cost_usd: 0.32,
      tokens_total: 26_560,
      is_live: true,  // animate this ticket's review
    },
    {
      id: 't2', number: 2430, repo_id: 'r2', repo: 'acme/api',
      title: 'Refactor problem-details middleware to handle nested validation errors',
      pr: { number: 2430, state: 'open', is_draft: false, author: 'mark-i',
            head: 'fix/problem-nested', base: 'main', additions: 91, deletions: 64, files: 5,
            html_url: 'https://github.com/acme/api/pull/2430' },
      status: 'review',
      kind: 'bug fix',
      source: 'github_pr',
      actor: 'mark-i',
      created: now - 12 * MIN,
      updated: now - 2 * MIN,
      verdicts: { arch: 'APPROVED', sec: 'CHANGES_REQUESTED', style: 'APPROVED' },
      cost_usd: 0.51,
      tokens_total: 49_700,
    },
    {
      id: 't3', number: 2429, repo_id: 'r1', repo: 'acme/web',
      title: 'Wire up audit-log filter chips to URL state',
      pr: { number: 2429, state: 'open', is_draft: false, author: 'priya-shah',
            head: 'feat/audit-filters', base: 'main', additions: 132, deletions: 18, files: 4,
            html_url: 'https://github.com/acme/web/pull/2429' },
      status: 'review',
      kind: 'new feature',
      source: 'github_pr',
      actor: 'priya-shah',
      created: now - 22 * MIN,
      updated: now - 4 * MIN,
      verdicts: { arch: 'APPROVED', sec: 'APPROVED', style: 'COMMENT' },
      cost_usd: 0.27,
      tokens_total: 24_100,
    },
    {
      id: 't5', number: 2427, repo_id: 'r4', repo: 'acme/mobile',
      title: 'Onboarding flow — step 3 should preserve email on back-nav',
      pr: { number: 2427, state: 'open', is_draft: true, author: 'sam-ng',
            head: 'fix/onboarding-back', base: 'main', additions: 24, deletions: 8, files: 2,
            html_url: 'https://github.com/acme/mobile/pull/2427' },
      status: 'review',
      kind: 'bug fix',
      source: 'github_pr',
      actor: 'sam-ng',
      created: now - 3 * HR,
      updated: now - 3 * HR,
      verdicts: { arch: 'skipped', sec: 'skipped', style: 'skipped' },
      skip_reason: 'draft',
      cost_usd: 0,
      tokens_total: 0,
    },
    {
      id: 't4', number: 2428, repo_id: 'r2', repo: 'acme/api',
      title: 'Bump psycopg to 3.2.3 + fix connection pool sizing',
      pr: { number: 2428, state: 'merged', is_draft: false, author: 'dev-bot',
            head: 'deps/psycopg-3.2.3', base: 'main', additions: 18, deletions: 12, files: 3,
            html_url: 'https://github.com/acme/api/pull/2428' },
      status: 'done',
      kind: 'bug fix',
      source: 'github_pr',
      actor: 'dev-bot',
      created: now - 2 * HR,
      updated: now - 90 * MIN,
      verdicts: { arch: 'APPROVED', sec: 'APPROVED', style: 'APPROVED' },
      cost_usd: 0.18,
      tokens_total: 18_400,
    },
    {
      id: 't6', number: 2426, repo_id: 'r1', repo: 'acme/web',
      title: 'Lessons modal: tab-trap when textarea has focus',
      pr: { number: 2426, state: 'merged', is_draft: false, author: 'rachel-cohen',
            head: 'fix/lesson-tab-trap', base: 'main', additions: 14, deletions: 6, files: 1,
            html_url: 'https://github.com/acme/web/pull/2426' },
      status: 'done',
      kind: 'bug fix',
      source: 'github_pr',
      actor: 'rachel-cohen',
      created: now - 5 * HR,
      updated: now - 4 * HR,
      verdicts: { arch: 'APPROVED', sec: 'APPROVED', style: 'COMMENT' },
      cost_usd: 0.22,
    },
    {
      id: 't7', number: 2425, repo_id: 'r2', repo: 'acme/api',
      title: 'Worker: retry-on-429 with exponential backoff (+ jitter)',
      pr: { number: 2425, state: 'merged', is_draft: false, author: 'mark-i',
            head: 'feat/worker-retry', base: 'main', additions: 88, deletions: 19, files: 4,
            html_url: 'https://github.com/acme/api/pull/2425' },
      status: 'done',
      kind: 'new feature',
      source: 'github_pr',
      actor: 'mark-i',
      created: now - 7 * HR,
      updated: now - 6 * HR,
      verdicts: { arch: 'APPROVED', sec: 'COMMENT', style: 'APPROVED' },
      cost_usd: 0.38,
    },
    {
      id: 't8', number: 2424, repo_id: 'r4', repo: 'acme/mobile',
      title: 'iOS 17 deprecation warnings in PushKit handler',
      pr: { number: 2424, state: 'closed', is_draft: false, author: 'sam-ng',
            head: 'fix/pushkit-17', base: 'main', additions: 6, deletions: 14, files: 1,
            html_url: 'https://github.com/acme/mobile/pull/2424' },
      status: 'done',
      kind: 'bug fix',
      source: 'github_pr',
      actor: 'sam-ng',
      created: now - 9 * HR,
      updated: now - 8 * HR,
      verdicts: { arch: 'APPROVED', sec: 'APPROVED', style: 'CHANGES_REQUESTED' },
      cost_usd: 0.14,
    },
    {
      id: 't9', number: 2423, repo_id: 'r1', repo: 'acme/web',
      title: 'Use TanStack Query for repo allowlist hooks',
      pr: { number: 2423, state: 'merged', is_draft: false, author: 'priya-shah',
            head: 'refactor/repos-query', base: 'main', additions: 161, deletions: 92, files: 7,
            html_url: 'https://github.com/acme/web/pull/2423' },
      status: 'done',
      kind: 'new feature',
      source: 'github_pr',
      actor: 'priya-shah',
      created: now - 1 * DAY,
      updated: now - 22 * HR,
      verdicts: { arch: 'APPROVED', sec: 'APPROVED', style: 'APPROVED' },
      cost_usd: 0.41,
    },
    {
      id: 't10', number: 2422, repo_id: 'r2', repo: 'acme/api',
      title: 'Fix N+1 in /tickets list endpoint',
      pr: { number: 2422, state: 'merged', is_draft: false, author: 'mark-i',
            head: 'perf/tickets-list-n1', base: 'main', additions: 44, deletions: 22, files: 2,
            html_url: 'https://github.com/acme/api/pull/2422' },
      status: 'done',
      kind: 'bug fix',
      source: 'github_pr',
      actor: 'mark-i',
      created: now - 1.2 * DAY,
      updated: now - 1.1 * DAY,
      verdicts: { arch: 'COMMENT', sec: 'APPROVED', style: 'APPROVED' },
      cost_usd: 0.29,
    },
  ];

  // Per-job detail for ticket review tabs. Keyed by ticket id and agent id.
  const reviewJobs = {
    t1: {
      arch: {
        status: 'running', verdict: null,
        started: now - 90 * SEC,
        step: 'invoking_agent',
        step_label: 'Invoking coding agent (claude-code)',
        progress: 0.62,
        heartbeat_age_s: 4,
        tokens_in: 14_820,
        tokens_out: 1_240,
        cost_usd: 0.18,
        prompt_hash: 'p_arch_8c1a',
        lessons_applied: ['l1','l2','l3','l4'],
        findings: [],
      },
      sec: {
        status: 'queued',
        started: null,
        step: null,
        step_label: 'Queued · waiting for worker slot',
        progress: 0,
        heartbeat_age_s: null,
        tokens_in: 0, tokens_out: 0,
        cost_usd: 0,
        prompt_hash: null,
        lessons_applied: ['l1','l2','l3','l4'],
        findings: [],
      },
      style: {
        status: 'posted', verdict: 'APPROVED',
        started: now - 4 * MIN,
        posted: now - 35 * SEC,
        step: 'posted',
        step_label: 'Posted to GitHub',
        progress: 1,
        heartbeat_age_s: null,
        tokens_in: 11_840,
        tokens_out: 720,
        cost_usd: 0.14,
        prompt_hash: 'p_style_4f02',
        lessons_applied: ['l1','l2','l3','l4'],
        duration_s: 184,
        findings: [
          {
            id: 'f1',
            file: 'src/routes/ticket.tsx', line: 42,
            severity: 'nit',
            title: 'Prefer named export for route component',
            body: 'Other route components in `src/routes/` use named exports. Match the local convention so the lazy-route loader can do uniform discovery.',
          },
          {
            id: 'f2',
            file: 'src/lib/sse.ts', line: 88,
            severity: 'info',
            title: 'Reconnect backoff could reference the constant',
            body: "You hardcode `2000`. The same value lives in `src/lib/constants.ts` as `RECONNECT_BASE_MS`. Reusing it would make this self-documenting.",
            snippet: [
              { ln: 82, type: 'ctx', text: 'const stream = new EventSource(url);' },
              { ln: 83, type: 'ctx', text: 'stream.onerror = () => {' },
              { ln: 84, type: 'ctx', text: '  if (retries > MAX_RETRIES) return abort();' },
              { ln: 85, type: 'ctx', text: '  retries++;' },
              { ln: 86, type: 'del', text: '  setTimeout(connect, 2000);' },
              { ln: 86, type: 'add', text: '  setTimeout(connect, RECONNECT_BASE_MS);' },
              { ln: 87, type: 'ctx', text: '};' },
            ],
            rationale: "Reusing the constant means the reconnect base will track future tuning in constants.ts. Self-documenting; one less magic number in the diff.",
            applied_lesson: 'l2',
          },
        ],
      },
    },
    t2: {
      arch: {
        status: 'posted', verdict: 'APPROVED',
        started: now - 11 * MIN, posted: now - 9 * MIN,
        step: 'posted', step_label: 'Posted to GitHub', progress: 1,
        tokens_in: 18_200, tokens_out: 1_810, cost_usd: 0.21,
        prompt_hash: 'p_arch_8c1a',
        lessons_applied: ['l5','l6','l7'],
        duration_s: 198,
        findings: [
          {
            id: 'f3', file: 'app/middleware/problem.py', line: 24, severity: 'suggestion',
            title: 'Extract nested-error walker into a helper',
            body: 'The walker is buried in the dispatch function; pulling it out as `walk_validation_errors(exc)` would let you test it directly.',
          },
        ],
      },
      sec: {
        status: 'posted', verdict: 'CHANGES_REQUESTED',
        started: now - 11 * MIN, posted: now - 5 * MIN,
        step: 'posted', step_label: 'Posted to GitHub', progress: 1,
        tokens_in: 19_400, tokens_out: 2_640, cost_usd: 0.24,
        prompt_hash: 'p_sec_a91e',
        lessons_applied: ['l5','l6','l7'],
        duration_s: 362,
        findings: [
          {
            id: 'f4', file: 'app/middleware/problem.py', line: 72,
            severity: 'must-fix',
            title: 'Validation message reveals internal field path',
            body: "The nested-error formatter echoes the full Pydantic `loc` tuple back to the client. Internal field names like `__root__.user.internal_oauth_state` will leak through. Strip the path or map it to a public-safe field name before serializing.",
          },
          {
            id: 'f5', file: 'app/middleware/problem.py', line: 110,
            severity: 'suggestion',
            title: 'Consider rate-limiting noisy 422s',
            body: 'Repeated validation failures from one IP can be a probe. Worth piping these through the existing rate-limit middleware.',
          },
        ],
      },
      style: {
        status: 'posted', verdict: 'APPROVED',
        started: now - 11 * MIN, posted: now - 7 * MIN,
        step: 'posted', step_label: 'Posted to GitHub', progress: 1,
        tokens_in: 12_100, tokens_out: 480, cost_usd: 0.06,
        prompt_hash: 'p_style_4f02',
        lessons_applied: ['l5','l6','l7'],
        duration_s: 244,
        findings: [],
      },
    },
  };

  // Audit entries — keyed by ticket id, newest-first
  const audit = {
    t1: [
      { id: 'a1', kind: 'review_job.posted',       actor: { kind: 'agent', name: 'style' }, ts: now - 35 * SEC,
        payload: { agent: 'style', verdict: 'APPROVED', findings_count: 2, duration_ms: 184_000, tokens_in: 11_840, tokens_out: 720, cost_usd: 0.14 } },
      { id: 'a2', kind: 'review_job.step_changed', actor: { kind: 'system' }, ts: now - 45 * SEC,
        payload: { agent: 'style', from: 'posting', to: 'posted' } },
      { id: 'a3', kind: 'review_job.step_changed', actor: { kind: 'agent', name: 'arch' }, ts: now - 50 * SEC,
        payload: { agent: 'arch', from: 'awaiting_agent_output', to: 'invoking_agent', heartbeat_age_s: 2 } },
      { id: 'a4', kind: 'review_job.heartbeat',    actor: { kind: 'system' }, ts: now - 60 * SEC,
        payload: { agent: 'arch', heartbeat_age_s: 12, ok: true } },
      { id: 'a5', kind: 'review_job.prompt_sent',  actor: { kind: 'agent', name: 'arch' }, ts: now - 90 * SEC,
        payload: { agent: 'arch', prompt_hash: 'p_arch_8c1a', tokens_in: 14_820, lessons_count: 4, repo: 'acme/web', model: 'claude-sonnet-4-5' } },
      { id: 'a6', kind: 'review_job.started',      actor: { kind: 'system' }, ts: now - 92 * SEC,
        payload: { agent: 'arch', worker_id: 'w-3', queue_wait_ms: 1_840 } },
      { id: 'a7', kind: 'review_job.prompt_sent',  actor: { kind: 'agent', name: 'style' }, ts: now - 220 * SEC,
        payload: { agent: 'style', prompt_hash: 'p_style_4f02', tokens_in: 11_840, lessons_count: 4, repo: 'acme/web', model: 'claude-haiku-4-5' } },
      { id: 'a8', kind: 'lessons.read',            actor: { kind: 'system' }, ts: now - 222 * SEC,
        payload: { repo: 'acme/web', lessons_count: 4, lesson_ids: ['l1','l2','l3','l4'] } },
      { id: 'a9', kind: 'review_job.scheduled',    actor: { kind: 'system' }, ts: now - 240 * SEC,
        payload: { agents: ['arch','sec','style'], reason: 'pull_request.opened', pr_number: 2431, head_sha: '8c1a9f3' } },
      { id: 'a10', kind: 'ticket.created',          actor: { kind: 'github_user', login: 'rachel-cohen' }, ts: now - 240 * SEC,
        payload: { source: 'github_pr', pr_number: 2431, repo: 'acme/web', title: 'Add real-time SSE handling to ticket detail' } },
    ],
    t2: [
      { id: 'b1', kind: 'review_job.posted',       actor: { kind: 'agent', name: 'sec' }, ts: now - 5 * MIN,
        payload: { agent: 'sec', verdict: 'CHANGES_REQUESTED', findings_count: 2, must_fix: 1, duration_ms: 362_000, tokens_in: 19_400, tokens_out: 2_640, cost_usd: 0.24 } },
      { id: 'b2', kind: 'review_job.posted',       actor: { kind: 'agent', name: 'style' }, ts: now - 7 * MIN,
        payload: { agent: 'style', verdict: 'APPROVED', findings_count: 0, duration_ms: 244_000, cost_usd: 0.06 } },
      { id: 'b3', kind: 'review_job.posted',       actor: { kind: 'agent', name: 'arch' }, ts: now - 9 * MIN,
        payload: { agent: 'arch', verdict: 'APPROVED', findings_count: 1, duration_ms: 198_000, cost_usd: 0.21 } },
      { id: 'b4', kind: 'review_job.prompt_sent',  actor: { kind: 'agent', name: 'arch' }, ts: now - 11 * MIN,
        payload: { agent: 'arch', prompt_hash: 'p_arch_8c1a', lessons_count: 3 } },
      { id: 'b5', kind: 'review_job.scheduled',    actor: { kind: 'system' }, ts: now - 12 * MIN,
        payload: { agents: ['arch','sec','style'], reason: 'pull_request.opened', pr_number: 2430 } },
      { id: 'b6', kind: 'ticket.created',          actor: { kind: 'github_user', login: 'mark-i' }, ts: now - 12 * MIN,
        payload: { source: 'github_pr', pr_number: 2430, repo: 'acme/api' } },
    ],
  };

  const metrics = {
    reviews_24h: 47,
    reviews_24h_delta: +12,
    avg_latency_s: 184,
    avg_latency_delta_s: -8,
    cost_24h: 4.82,
    cost_24h_delta: +0.41,
    open_tickets: 4,
    failure_rate: 0.021,
    queue_depth: 1,
    workers_active: 2,
    workers_total: 4,
    spark_reviews_24h: [2,1,0,0,1,3,4,5,6,3,2,3,1,2,1,4,3,2,1,2,0,1,0,0],
  };

  const activity = [
    { id: 'act1', ts: now - 35 * SEC,  kind: 'review_posted', repo: 'acme/web', pr: 2431, agent: 'style', verdict: 'APPROVED' },
    { id: 'act2', ts: now - 2 * MIN,   kind: 'review_posted', repo: 'acme/api', pr: 2430, agent: 'sec',   verdict: 'CHANGES_REQUESTED' },
    { id: 'act3', ts: now - 4 * MIN,   kind: 'review_posted', repo: 'acme/web', pr: 2429, agent: 'style', verdict: 'COMMENT' },
    { id: 'act4', ts: now - 4 * MIN,   kind: 'pr_opened',     repo: 'acme/web', pr: 2431, actor: 'rachel-cohen' },
    { id: 'act5', ts: now - 12 * MIN,  kind: 'pr_opened',     repo: 'acme/api', pr: 2430, actor: 'mark-i' },
    { id: 'act6', ts: now - 22 * MIN,  kind: 'pr_opened',     repo: 'acme/web', pr: 2429, actor: 'priya-shah' },
    { id: 'act7', ts: now - 90 * MIN,  kind: 'pr_merged',     repo: 'acme/api', pr: 2428 },
    { id: 'act8', ts: now - 4 * HR,    kind: 'lesson_added',  repo: 'acme/web', actor: 'priya-shah', lesson: "Don't suggest mocks in tests" },
  ];

  const settings = {
    github_app: {
      installed: true,
      install_id: '4488213',
      installed_on: now - 41 * DAY,
      org: 'acme',
      app_url: 'https://github.com/apps/yaaof-acme',
    },
    api_key: {
      provider: 'anthropic',
      key_preview: 'sk-ant-…GqL7',
      added: now - 41 * DAY,
    },
    plugin_health: [
      { name: 'github',      status: 'healthy', latency_ms: 78,  last_check: now - 22 * SEC },
      { name: 'anthropic',   status: 'healthy', latency_ms: 412, last_check: now - 18 * SEC },
      { name: 'claude-code', status: 'healthy', latency_ms: null,last_check: now - 45 * SEC },
      { name: 'sse',         status: 'healthy', clients: 3,       last_check: now - 4  * SEC },
    ],
  };

  // Onboarding state — start with everything configured so the populated dashboard shows.
  // Setting to `false` lets the user see the onboarding empty state instead.
  const onboarding = {
    github_app: true,
    api_key: true,
    repos: true,
  };

  return { repos, agents, lessons, tickets, reviewJobs, audit, metrics, activity, settings, onboarding, now };
})();
