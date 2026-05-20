# domain/settings

> Cross-cutting aggregator over plugin state — onboarding readiness and plugin discovery for the Settings UI.

## Purpose

Small cross-cutting view over yaaos's plugins. Owns no credentials, no health checks, no plugin-specific state — each plugin owns its URL namespace at `/api/<plugin_id>/...` plus its own credential setter and `/health`. This module aggregates "is everything wired up?" via a tiny contributor registry and walks the three plugin registries to expose a single discovery endpoint. Both respect layering — plugins depend on domain, not the reverse — by having plugins push into the registries at boot.

## Public interface

Exported from `app/domain/settings/__init__.py`:

- `OnboardingStatus` — Pydantic model with `github_app_installed: bool`, `anthropic_key_set: bool`, `all_ready` property.
- `get_onboarding_status(*, org_id)` — asks each registered contributor; returns aggregated `OnboardingStatus`.
- `register_onboarding_contributor(name, check)` — plugins (`github`, `claude_code`) register their `"github_app_installed"` and `"anthropic_key_set"` checks at boot.

HTTP routes (`/api/settings`):

- `GET /api/settings/onboarding` — returns `OnboardingStatus` for the current org.
- `GET /api/settings/plugins` — returns `list[PluginMeta]` across all three registries. Synchronous (registries populated at bootstrap; reads are pure in-memory).

`list_plugins()` lives in `service.py` and backs the route; intentionally not in `__all__`.

## Module architecture

### Files

- `service.py` — contributor registry, `OnboardingStatus`, `get_onboarding_status`, the cross-registry `list_plugins` walker.
- `web.py` — the two FastAPI routes and `register_routes`.
- `module.py` — `get_module_name() -> "settings"`.

### Onboarding contributor registry

Module-level dict `_CONTRIBUTORS` maps name → `async (org_id) -> bool`. Plugins call `register_onboarding_contributor` during their own module import (before `app.main` calls `create_app`), so by the time `/api/settings/onboarding` is hit the registry is populated.

`get_onboarding_status` awaits the two contributor keys (`"github_app_installed"`, `"anthropic_key_set"`). A missing contributor reads as `False` — the UI shows that card not-yet-done. `_reset_contributors_for_tests` clears the registry between tests.

Keys are hardcoded in `OnboardingStatus` because the model is the canonical shape. Adding a third gate means a new field + new contributor key; the registry indirection means plugins own readiness logic without `settings` importing them.

### Plugin discovery

`list_plugins()` walks the three plugin registries directly:

- `app.domain.vcs.registry._PLUGINS` — VCS plugins (`github`).
- `app.domain.coding_agent.service._PLUGINS` — coding-agent plugins (`claude_code`).
- `app.core.workspace.service._PROVIDERS` — workspace providers (`in_process`).

For each entry it appends `meta` — a `PluginMeta` from `core/plugin_meta` (`id`, `type`, `display_name`, `description`, `docs_url`). Return order VCS → coding-agent → workspace, stable across reloads. The Settings UI pairs each row with the plugin's own `/api/<id>/health` endpoint for live status.

Reading registries directly works because plugins are populated by bootstrap time — `core/webserver`'s lifespan runs `on_startup` after every module's import-time `register_routes` calls.

### URL ownership

Plugin endpoints don't live under `/api/settings/`. Each plugin owns its namespace:

- Claude Code — `POST /api/claude_code/api_key`, `GET /api/claude_code/health`.
- GitHub — `GET /api/github/installation`, `GET /api/github/health`, `POST /api/github/credentials`, `GET /api/github/manifest-callback`, `/api/github/webhook`.
- In-process workspace — `GET /api/in_process/health`.

`domain/settings` only carries cross-cutting aggregates. Plugin credential setters live with plugin code.

### Layering

The module imports private symbols from three plugin registries (`_PLUGINS`, `_PROVIDERS`). The price of putting the discovery endpoint in `domain/settings` rather than in each plugin: one cross-module read, no inverse dependency. Plugins still depend only on `core` and `domain` — they don't import `domain/settings`. The onboarding registry uses the inverse direction (plugins push in) because readiness is plugin-defined logic.

### What the module does not do

- Doesn't own credentials or secrets — `plugins/claude_code` and `plugins/github` own their own settings tables and setters.
- Doesn't perform health checks — each plugin owns its `/api/<id>/health`. The UI pairs the discovery list with those endpoints client-side.
- Doesn't enforce an onboarding order — `all_ready` is a simple AND; the FE renders both cards in parallel.
- Doesn't persist contributor registrations — `_CONTRIBUTORS` is rebuilt on every process boot.

## Data owned

None. The contributor registry is in-memory and repopulated on every boot.

## How it's tested

`app/domain/settings/test/` is a placeholder. Aggregation behaviour covered by backend integration tests in `app/test/` and by the e2e onboarding flow in `apps/e2e/`, which drives Settings through a real GitHub App install + Anthropic-key setup and asserts `GET /api/settings/onboarding` flips to `all_ready` and `GET /api/settings/plugins` returns the expected three entries.
