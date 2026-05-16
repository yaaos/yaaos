# Testing infrastructure — Internal Architecture

> The self-contained Docker test stack: fake-github, pre-seeded Postgres, coding-agent CLI cache, time controls, secret generation. This doc spells out the contracts that `patterns.md § Testing` summarizes.

## Purpose

Tests run anywhere, offline, deterministically, with no external credentials and no rate-limit exposure. Production code paths are exercised end-to-end — only the *hosts* change (yaaof talks to a fake GitHub instead of `api.github.com`, replays cached coding-agent invocations instead of spawning real CLI processes).

## Components

```
┌─────────────────────────┐    ┌──────────────────────┐
│  apps/backend  (yaaof)  │───▶│  apps/fake-github    │
│                         │    │  (Python FastAPI)    │
│  GITHUB_API_BASE_URL    │    │  http://fake-github  │
└────────┬────────────────┘    └──────────┬───────────┘
         │                                │
         │  (HMAC-signed webhook POST)    │  POST /__test/dispatch_webhook
         │◀───────────────────────────────│  (control surface for tests)
         ▼                                ▼
┌─────────────────────────┐    ┌──────────────────────┐
│  Postgres 16            │    │  Coding-agent cache  │
│  (pre-seeded for e2e;   │    │  on-disk JSON files  │
│   empty for integration)│    │  in test source tree │
└─────────────────────────┘    └──────────────────────┘
```

Three artifacts:

- **`apps/fake-github/`** — peer Python service. Fakes every GitHub endpoint the plugin calls. Verifies the App JWT signed by a test PEM. HMAC-signs outbound webhooks with a shared test secret. In-memory state.
- **`apps/backend/bin/seed_test_data`** — Python script using ORM models to populate the e2e Postgres.
- **`docker/docker-compose.test.yml`** — brings up Postgres + fake-github + yaaof with the right env wiring + secrets sharing.

Plus two in-repo concerns:

- **CLI cache** — file-colocated JSON cache (`<test_dir>/.coding_agent_cache.json`) plus a pytest fixture that swaps the `claude_code` plugin instance for a caching wrapper. Lives in the project's pytest plugin (`apps/backend/app/testing/stub_coding_agent/`).
- **Stub workspace** — wraps each registered `WorkspaceProvider` with a `StubWorkspaceProvider` that creates an empty tempdir on `provision()` (no git clone, no vcs lookup) and returns a canned no-op `CodingAgentCliResult` on `run_coding_agent_cli`. Lives at `apps/backend/app/testing/stub_workspace/`. Activated alongside the stub coding-agent when `YAAOF_CODING_AGENT_STUB` is set.

---

## 1. `apps/fake-github/` — fake GitHub service

### Public surface

The service implements the union of endpoints listed in `internals/plugins-github.md § API client`. Reproduced here for autonomy:

| Method + path | Behavior |
|---|---|
| `POST /app/installations/{id}/access_tokens` | Verify App JWT in `Authorization`. Return `{ "token": "ghs_fake_<install_id>_<nonce>", "expires_at": "<+1h>" }`. |
| `GET /app` | Verify App JWT. Return `{ "id": <app_id>, "slug": "yaaof-test" }`. (Used by `health_check`.) |
| `GET /repos/{owner}/{repo}/pulls/{number}` | Verify installation token. Return seeded PR JSON. |
| `GET /repos/{owner}/{repo}/pulls/{number}` (with `Accept: application/vnd.github.v3.diff`) | Return seeded raw-diff text. |
| `GET /repos/{owner}/{repo}/pulls/{number}/comments` | Return list of inline comments yaaof has posted (from in-memory state). |
| `GET /repos/{owner}/{repo}/issues/{number}/comments` | Return list of top-level comments yaaof has posted. |
| `POST /repos/{owner}/{repo}/pulls/{number}/reviews` | Record the posted review in in-memory state. Return `{ "id": <generated>, "html_url": "...", "node_id": "..." }`. |
| `GET /repos/{owner}/{repo}/pulls?state=open` | Return seeded open PRs. (catch-up poller) |
| `GET /repos/{owner}/{repo}/compare/{base}...{head}` | Return canned compare response from seeded fixtures. |

### Test-control endpoints

These are **not** GitHub-compatible; they're how tests drive the service.

| Method + path | Behavior |
|---|---|
| `POST /__test/reset` | Clears in-memory state. Called between e2e tests. |
| `POST /__test/seed_pr` | Body: a full `VCSPullRequest`-shaped JSON. Adds it to seeded PRs that subsequent `/repos/.../pulls/{number}` calls return. |
| `POST /__test/seed_diff` | Body: `{ "owner", "repo", "number", "diff": "..." }`. Adds a seeded diff. |
| `POST /__test/dispatch_webhook` | Body: `{ "event": "pull_request", "action": "opened", "payload": {...full GitHub webhook payload...}, "target_url": "http://yaaof:8080/api/github/webhook" }`. The service HMAC-signs the payload body with the shared `GITHUB_WEBHOOK_SECRET` and POSTs it to the target URL with the standard `X-Hub-Signature-256` + `X-GitHub-Event` headers. Returns yaaof's response. |
| `GET /__test/posted_reviews` | Returns the in-memory list of reviews yaaof has posted. Used by tests to assert on what got posted. |
| `GET /__test/posted_comments` | Same, for comments. |

### Auth model

Two shared test secrets, generated once and committed (or generated by `bin/generate_test_secrets`):

- `GITHUB_APP_PRIVATE_KEY` — a self-signed RSA private key in PEM format. Used by yaaof to sign JWTs. fake-github verifies with the matching public key embedded in its own source.
- `GITHUB_WEBHOOK_SECRET` — a 40-byte hex string. yaaof verifies inbound webhook signatures with this; fake-github signs outbound dispatches with the same.

Both are hardcoded `apps/fake-github/test_secrets.py` (committed to the repo) AND set as env vars in `docker-compose.test.yml`. They are **obviously fake** values, marked as such in comments.

### Tech stack

- Python 3.13 + FastAPI (consistent with yaaof itself, simplest to maintain).
- Own `pyproject.toml`; member of the uv workspace.
- Single-file Dockerfile.
- ~400 LOC including all endpoints + JWT verify + HMAC sign.

### Layout

```
apps/fake-github/
├── pyproject.toml
├── Dockerfile
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app + endpoints
│   ├── auth.py              # JWT verify + HMAC sign helpers
│   ├── state.py             # in-memory state (singleton)
│   ├── test_secrets.py      # committed test PEM + HMAC secret
│   └── seeds.py             # default seeded data (matches seed_test_data)
└── bin/ci                   # ruff + own tests
```

`apps/fake-github` is **not** a yaaof backend module. It does not appear in `apps/backend/tach.toml`, the module map, or layering rules. It is a peer service that exists only for testing.

---

## 2. `apps/backend/bin/seed_test_data` — e2e fixture seeding

### Invocation

A Python script that:

1. Connects to Postgres using `DATABASE_URL`.
2. Runs migrations (`core/database.migrate()`).
3. Inserts the fixture rows using SQLAlchemy ORM (same `Base.metadata` as production).
4. Encrypts the Anthropic API key fixture with the test `YAAOF_ENCRYPTION_KEY` before inserting (`claude_code_settings` row).
5. Idempotent — checks for existing rows by natural key before inserting.

Invoked from `docker-compose.test.yml` as an init container that runs after Postgres is healthy and before yaaof starts.

### Fixture content

| Table | Rows | Detail |
|---|---|---|
| `github_app_installations` | 1 | `installation_id=fake-install-1`, `org=acme`, status=`active` |
| `github_settings` | 1 | App id=fake-app-1, encrypted PEM + webhook secret (encrypted with test key) |
| `claude_code_settings` | 1 | Provider=anthropic, encrypted key (placeholder; cache replaces real calls anyway) |
| `repos` | 2 | `acme/web` (TypeScript), `acme/api` (Python). Both active. |
| `reviewer_agents` | 3 | architecture / security / style. Same default prompts the production seed migration installs. |
| `lessons` | 4 | 2 for `acme/web`, 2 for `acme/api`. Realistic-looking team lessons. |
| `tickets` | 5 | Mix of `in_review` (with in-flight review_jobs) and `complete`. |
| `pull_requests` | 5 | Mirror each ticket's source PR. |
| `review_jobs` | ~12 | Realistic mix across statuses: `queued`, `running` (with heartbeat), `posted`, `failed`, `cancelled`. |
| `posted_comments` | ~8 | Linked to the `posted` review_jobs. |
| `audit_entries` | ~30 | The full chain for each ticket: created, scheduled, prompt_sent, posted/failed/cancelled. |

The fixture content lives in `apps/backend/bin/seed_test_data` as Python literals (constructor calls), not a YAML file — schema changes are caught by the type system.

### Why not the production seed migration

The production seed installs the 3 reviewer agent rows (via Alembic data migration; see `internals/reviewer.md § Seeding`). `seed_test_data` includes those PLUS the test-only fixtures (repos, tickets, etc.). Conceptually: prod-seed = a strict subset of test-seed. The script imports and reuses the prod-seed constants.

---

## 3. `docker/docker-compose.test.yml` — test stack shape

```yaml
name: yaaof-test

services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: yaaof
      POSTGRES_PASSWORD: yaaof
      POSTGRES_DB: yaaof
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U yaaof -d yaaof"]
      interval: 1s
      timeout: 2s
      retries: 30

  seed:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    environment:
      DATABASE_URL: postgresql+asyncpg://yaaof:yaaof@postgres:5432/yaaof
      YAAOF_ENCRYPTION_KEY: ${TEST_ENCRYPTION_KEY}
    depends_on:
      postgres: { condition: service_healthy }
    command: ["uv", "run", "python", "-m", "apps.backend.bin.seed_test_data"]
    restart: "no"

  fake-github:
    build:
      context: ..
      dockerfile: apps/fake-github/Dockerfile
    environment:
      GITHUB_WEBHOOK_SECRET: ${TEST_WEBHOOK_SECRET}
    ports:
      - "8081:8080"

  yaaof:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    environment:
      DATABASE_URL: postgresql+asyncpg://yaaof:yaaof@postgres:5432/yaaof
      YAAOF_ENCRYPTION_KEY: ${TEST_ENCRYPTION_KEY}
      YAAOF_ENV: dev
      GITHUB_API_BASE_URL: http://fake-github:8080
      # Time controls — all set to fast values for tests.
      YAAOF_REVIEW_DEBOUNCE_SECONDS: "0"
      YAAOF_REAPER_INTERVAL_SECONDS: "1"
      YAAOF_HEARTBEAT_INTERVAL_SECONDS: "1"
      YAAOF_CATCHUP_DELAY_SECONDS: "0"
    depends_on:
      postgres: { condition: service_healthy }
      seed: { condition: service_completed_successfully }
      fake-github: { condition: service_started }
    ports:
      - "8080:8080"
```

`TEST_ENCRYPTION_KEY` and `TEST_WEBHOOK_SECRET` come from `apps/e2e/.env.test` (committed; values are obviously fake). Same values used by `apps/fake-github/test_secrets.py` constants.

---

## 4. Coding-agent CLI cache

### Integration point — `CachingCodingAgentPlugin` wrapper via pytest fixture

Production code is unaware of the cache. The integration happens via a pytest fixture that **replaces the registered `claude_code` plugin instance** in the `domain/coding_agent` registry with a `CachingCodingAgentPlugin` wrapper for the duration of the test session.

The wrapper:

```python
# apps/backend/app/testing/caching_coding_agent.py
class CachingCodingAgentPlugin(CodingAgentPlugin):
    plugin_id: str  # mirrors the wrapped plugin's id ("claude_code")

    def __init__(self, wrapped: CodingAgentPlugin, cache_path: Path, allow_calls: bool):
        self._wrapped = wrapped
        self._cache_path = cache_path
        self._allow_calls = allow_calls
        self._cache: dict[str, CachedEntry] = self._load_cache()

    async def review(self, workspace, context):
        key = self._key("review", context)
        if key in self._cache:
            return self._reconstruct_review(self._cache[key])
        if not self._allow_calls:
            raise CodingAgentCacheMiss(
                f"No cached review for key {key[:16]}... "
                f"(context sha). Re-run pytest with --allow-coding-agent-calls to populate."
            )
        result = await self._wrapped.review(workspace, context)
        self._cache[key] = self._serialize_review(result)
        self._save_cache()
        return result

    # reply(): symmetric.
    # validate_config + health_check pass through unchanged.
```

This satisfies the DI-over-patch ban: no `mock.patch`, no monkeypatching of subprocess. The wrapper is a real `CodingAgentPlugin` Protocol implementation, swapped in via the existing registry.

### Cache file format

One JSON file per test module: `<test_module_dir>/.coding_agent_cache.json`.

```json
{
  "version": 1,
  "entries": {
    "<sha256(method + canonical_json(context))>": {
      "method": "review",
      "context_preview": "first 120 chars of the context for human grepping",
      "result": {
        "status": "success",
        "findings": [ /* vcs.Finding shape */ ],
        "state": "COMMENT",
        "summary_body": null,
        "telemetry": { "tokens_in": 14820, "tokens_out": 1240, "cost_usd": 0.18, "latency_ms": 18200, ... }
      },
      "recorded_at": "2026-05-15T22:31:20Z"
    }
  }
}
```

**Keying:** `sha256(method || "\x00" || canonical_json(context.model_dump()))`. Any change in context (PR, diff, lessons, persona, …) produces a new key → cache miss.

**Partial hits don't exist.** Either the exact key is cached or it's not.

**Test failure on cache miss:** the wrapper raises `CodingAgentCacheMiss` with the key prefix and instructions, which surfaces as a clear pytest failure. The developer reruns with `--allow-coding-agent-calls` to populate.

### `--allow-coding-agent-calls` flag

A pytest CLI flag registered by the in-repo pytest plugin via `pytest_addoption`:

```python
# apps/backend/app/testing/plugin.py
def pytest_addoption(parser):
    parser.addoption(
        "--allow-coding-agent-calls",
        action="store_true",
        default=False,
        help="Permit cache misses to invoke the real coding-agent CLI. Requires a real Anthropic API key in env. Used to populate/regenerate caches.",
    )
```

When set, the `CachingCodingAgentPlugin` wrapper delegates to the real plugin on cache miss and records the result. When unset, cache miss → test fails.

### Pytest fixture wiring

```python
# apps/backend/app/testing/plugin.py
@pytest.fixture(autouse=True, scope="session")
def _swap_coding_agent_plugin(request):
    from app.domain.coding_agent import _PLUGINS  # the module-level registry dict
    from app.plugins.claude_code import ClaudeCodePlugin
    from app.testing.caching_coding_agent import CachingCodingAgentPlugin

    cache_dir = Path(request.node.fspath).parent
    cache_path = cache_dir / ".coding_agent_cache.json"
    allow_calls = request.config.getoption("--allow-coding-agent-calls")

    real_plugin = _PLUGINS["claude_code"]
    _PLUGINS["claude_code"] = CachingCodingAgentPlugin(real_plugin, cache_path, allow_calls)
    yield
    _PLUGINS["claude_code"] = real_plugin
```

This is the only place that touches the registry dict directly — production code uses `register_coding_agent_plugin` + `get_plugin`. The fixture lives in `app/testing/` — the **fourth backend layer** (see `modularity.md` § Backend specifics). The layer is tach-tracked like any other; the layering rule `core < domain < plugins < testing` ensures nothing in production code can import from it. The wheel build excludes `app/testing/` so distribution artifacts physically cannot enable test-mode behavior.

> **As built in M01:** the realized form is `app/testing/stub_coding_agent/` — a wrapper plugin whose `review()` returns a canned `ReviewResult` (empty findings, APPROVED, fake telemetry) and whose `reply()` returns a canned `ReplyResult`, both without touching a real CLI or cache file. The `CachingCodingAgentPlugin` described above (record/replay against a real CLI) is the natural sibling for when realistic outputs are needed; it would live at `app/testing/caching_coding_agent/` and follow the same wrap-via-registry pattern.

---

## 5. Test secrets

Three secrets are shared between yaaof + fake-github + the seed script in the test stack:

| Secret | Where it's generated | Where it's used |
|---|---|---|
| `TEST_ENCRYPTION_KEY` | Hardcoded in `apps/e2e/.env.test` (Fernet-format, obviously fake) | yaaof's `YAAOF_ENCRYPTION_KEY`; seed script's encryption of plugin credentials |
| `TEST_GITHUB_APP_PEM` | Hardcoded in `apps/fake-github/app/test_secrets.py` (self-signed RSA key, committed) | yaaof signs JWTs with it (seed script inserts into `github_settings`); fake-github verifies with the matching public key |
| `TEST_WEBHOOK_SECRET` | Hardcoded in `apps/e2e/.env.test` and `apps/fake-github/app/test_secrets.py` (40-byte hex) | yaaof verifies inbound webhook signatures; fake-github signs outbound dispatches |

All three are **obviously fake** — every value contains the literal string `"TEST-FAKE-NOT-FOR-PROD"` or similar. No accidental production reuse possible.

A `bin/generate_test_secrets` helper exists to regenerate them if needed (rare), but the values are committed and stable; tests don't generate them per-run.

---

## 6. Pytest plugin entry-point

A single in-repo pytest plugin auto-loads via `pyproject.toml`:

```toml
# apps/backend/pyproject.toml
[project.optional-dependencies]
dev = [..., "pytest>=8.3", ...]

[project.entry-points."pytest11"]
yaaof = "app.testing.plugin"
```

The plugin wires up:

1. `pytest_addoption` for `--allow-coding-agent-calls`.
2. The session-scoped `_swap_coding_agent_plugin` fixture above.
3. A `db_session` fixture that begins a transaction, yields, rolls back.
4. A `fake_github_url` fixture that returns `os.environ.get("GITHUB_API_BASE_URL", "http://localhost:8081")`.
5. Any other cross-cutting fixtures.

For backend integration tests, the fake-github service is started **as a subprocess by the pytest plugin's session-start hook** (using `uv run --package fake-github uvicorn ...`) and torn down at session end. Tests don't need docker for integration; docker is only needed for e2e (where the full network of services matters).

---

## Open questions for implementation

- **Cache size growth.** Each `review_job.prompt_sent` worth of cache is potentially 100KB+ (raw_output includes the full agent text). For a few hundred tests this is fine; for thousands we'd need a more careful storage choice. Acceptable at M01 scale.
- **Cache key collisions.** SHA256 of three concatenated values is statistically safe; if a collision ever happens, the test author would see "wrong response" rather than "missing response" and re-record. Acceptable.
- **fake-github concurrent requests.** In-memory state is per-process and not thread-safe. If two e2e tests run in parallel against the same fake-github, they'll see each other's state. M01 runs e2e serially; M02+ may need a per-test fake-github instance or state isolation.

## Decisions

### 2026-05-15 — Tests run against a self-contained Docker stack; no real external services
See [patterns.md § 2026-05-15 — Tests run entirely against a self-contained Docker stack](../patterns.md#decisions) for the policy decision. This doc spells out the implementation.

### 2026-05-16 — `testing/stub_workspace` mirrors `testing/stub_coding_agent` for the workspace layer
The workspace Protocol now exposes operations (`run_coding_agent_cli`), not paths. Tests that skip the real coding-agent (`YAAOF_CODING_AGENT_STUB`) also skip the real git clone — otherwise integration tests would still hit the network. `wrap_all_registered_workspace_providers()` walks `core.workspace._PROVIDERS` and swaps each entry for a `StubWorkspaceProvider` (idempotent), in lockstep with the coding-agent wrap. The stub's `run_coding_agent_cli` is a no-op because stub coding-agent short-circuits before any workspace call; the method is implemented for Protocol completeness.

### 2026-05-15 — CLI cache integrates via `CachingCodingAgentPlugin` wrapper + pytest fixture
The cache lives outside of production code. A pytest fixture replaces the registered `claude_code` plugin instance in the `domain/coding_agent` registry with a caching wrapper for the duration of the test session. On cache miss with `--allow-coding-agent-calls`, the wrapper delegates to the real plugin and records. On cache miss without the flag, the wrapper raises `CodingAgentCacheMiss`.
**Why:** the DI-over-patch ban (`patterns.md § DI over @patch`) forbids monkeypatching the plugin's subprocess invocation. A wrapper plugin satisfies the rule via pure DI and generalizes to future coding-agent plugins (codex, aider) without code changes in their plugin code.

### 2026-05-15 — `apps/fake-github` is a Python FastAPI peer service, not a yaaof module
Lives in `apps/fake-github/` as a member of the uv workspace. Implements the union of GitHub endpoints yaaof's plugin calls plus `/__test/*` control endpoints. Test secrets (PEM, HMAC) committed in `apps/fake-github/app/test_secrets.py` and shared with yaaof via `docker-compose.test.yml` env wiring.
**Why:** Python is the lowest-friction language (matches the rest of the backend; reuses uv workspace). FastAPI is what yaaof's plugin expects to talk to via the same `httpx` client.

### 2026-05-15 — Time-control env vars; defaults are production values; tests set short
`YAAOF_REVIEW_DEBOUNCE_SECONDS`, `YAAOF_REAPER_INTERVAL_SECONDS`, `YAAOF_HEARTBEAT_INTERVAL_SECONDS`, `YAAOF_CATCHUP_DELAY_SECONDS`. Each code site that sleeps reads from `core/config.Settings`, never hardcodes. `docker-compose.test.yml` sets each to a fast value.
**Why:** prod wants reasonable batching; tests can't afford 30-second waits. Env vars are the lowest-friction abstraction (no clock-control library, no test-only branch in production code).
