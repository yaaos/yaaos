# Backend modularity

How backend code is organised, what depends on what, and how the rules are enforced in CI.

## Layers

| Layer | Path | Purpose | May depend on |
|---|---|---|---|
| `core` | `app/core/` | Infrastructure / technical primitives. | (nothing in `app/*`) |
| `domain` | `app/domain/` | Business logic; defines plugin Protocols. Vendor-neutral. | `core` |
| `plugins` | `app/plugins/` | Vendor-specific implementations. | `core`, `domain` |
| `testing` | `app/testing/` | Test-only scaffolding. Excluded from prod wheels. | `core`, `domain`, `plugins` |

Layering rule: each layer may depend on lower layers, never higher. Production code cannot import `testing`.

Nuance: `core` modules may define domain-aware *data types* (e.g., `Actor` references the agent concept) — but never *behaviour* that encodes business decisions.

### No module-name collisions across `core`, `domain`, `plugins`

Module names are globally unique across the three layers. Reusing a name (e.g. `core/auth` *and* `domain/auth`) makes import sites ambiguous to read, breaks unique RouteSpec keys, and produces confusing audit-kind prefixes. When a domain shim needs to live alongside its core primitive, rename the domain module so the relationship is one-way and the names don't collide (e.g. `core/auth` is the middleware; `domain/sessions` is the FastAPI dep + `/api/auth/*` routes that bind it to identity + orgs). Same for `core/byok`: its HTTP shim lives inside `domain/orgs/byok_routes.py`, not in a new `domain/byok` module.

## Module shape

Each subdirectory of a layer is a module. Conventional files:

- `__init__.py` — public interface: re-exports + `__all__` + registration side effects.
- `module.py` — exports `get_module_name()`, used in registrations and audit kinds.
- `service.py` — business-logic functions (split as the module grows).
- `models.py` — SQLAlchemy + Pydantic types owned by the module.
- `web.py` — FastAPI router + handlers (only if exposing HTTP routes).
- `test/` — tests live inside the module.

### `__init__.py` rules

- `__all__` is always present. Tach uses it to compute the module's interface.
- All implementation lives in named submodules. No business logic in `__init__.py`.
- Order: re-exports, then `__all__`, then registration calls.
- No lazy/conditional imports (rare heavy-ML case marked `# noqa: PLC0415`).
- No self-imports — internal files use direct submodule paths, not `from app.domain.foo import bar`.

### `web.py`

- Router carries NO prefix. `RouteSpec.url_prefix` (defaulting to `/api/{module_name}`) is applied by `core/webserver`.
- `register_routes(RouteSpec(...))` called at the bottom; one-prefix-per-module enforced at registration time. Misconfigurations fail boot in the offending module's traceback.
- See [core_webserver.md](core_webserver.md) for the full registry contract.

### Imports

- Absolute imports only. No relative imports across module boundaries.
- Module-level imports only.
- Other modules import only what's in `__init__.py`'s `__all__`. Importing internals across boundaries is Tach-rejected.

## Plugin Protocols

Protocols are hosted in `core/` or `domain/` depending on whether their primitives are infrastructure or business.

| Protocol | Host | Implemented in |
|---|---|---|
| `VCSPlugin` | `domain/vcs` | `plugins/github` |
| `CodingAgentPlugin` | `domain/coding_agent` | `plugins/claude_code` |
| `WorkspaceProvider` | `core/workspace` | `plugins/in_memory_workspace` |

Plugins register themselves at import time (in their `__init__.py`). `app/main.py` imports every plugin package that should be active.

Each Protocol exposes a `meta: PluginMeta` (`id`, `type`, `display_name`, `description`, `docs_url`). The `id` is the registry key, URL prefix, and canonical accessor.

## Table ownership

`apps/backend/bin/check_table_access` enforces that a module reads/writes only its own tables. Ownership auto-derived from each SQLAlchemy model's `__module__` — moving a model moves ownership. The scanner flags ORM calls, raw `text("...")` strings, and `Table("name", ...)` definitions outside the owning module. Plugin tables are owned by the plugin.

Runs as part of `bin/ci` and as a pre-commit hook.

## `bin/sync_modules`

Runs the full modularity workflow:

1. Discover modules under each layer.
2. Sync `tach.toml` — write module entries and interface exports from `__all__`.
3. Check internal imports (no relative-imports across boundaries, no `__init__` self-imports).
4. Check layering.
5. Run `tach check`.
6. Run `bin/check_table_access`.

Never hand-edit `tach.toml` — re-run `bin/sync_modules` after changing a module interface.

## Adding a new module

1. Create the directory under the appropriate layer.
2. Add `__init__.py` (re-exports + `__all__`) and `module.py` (`get_module_name`).
3. If exposing HTTP routes: add `web.py`, call `register_routes` at bottom, ensure `__init__.py` imports `web` so the side effect runs.
4. For a new plugin: ensure `app/main.py` imports the plugin package.
5. Run `bin/sync_modules`.
6. Add `apps/backend/docs/<layer>_<module>.md` following the per-module template.
