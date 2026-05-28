# core/llm

> Mechanics for direct, single-shot, structured LLM calls. Prompts live in the calling domain module — this module owns call mechanics only.

## Scope

- **Owns:** prompt-file parsing, jinja2 templating, LangChain runnable construction, structured-output validation + retries, Braintrust gateway routing, file-colocated LLM test cache, thin `braintrust.Eval` wrapper.
- **Does not own:** prompts, output schemas, agent loops, RAG, cost budgeting, eval fixtures/scorers (all live in the owner module under `<module>/llm/`).

## Why / invariants

- **`PromptRunnable` retries once on `MalformedOutput`** then raises. The audit-log line on failure is the caller's responsibility.
- **Gateway routing is automatic** — `_build_model` reads `BRAINTRUST_API_KEY` and injects `base_url` / `api_key`. When unset, LangChain falls back to its normal env-var resolution.
- **`base_url` has NO `/v1` suffix** — both Anthropic and OpenAI SDKs append their own canonical paths.
- **Braintrust project is derived from the prompt path** (`apps/backend/app/domain/<module>/llm/prompts/…` → project name = `<module>`). Auto-created by Braintrust on first request; no Braintrust-side setup needed for new modules.
- **`LLMTestCache` keys exclude `base_url` and `api_key`** — toggling the gateway does not invalidate cached responses.
- **Test cache is file-colocated and committed to git** — every CI run and contributor gets the same responses without real LLM calls.

## Gotchas

- **Cache miss with `allow_real_calls=False` raises a loud `RuntimeError`** — re-run with `--allow-llm-calls` to populate.
- **Prompt body uses `StrictUndefined`** — missing template variables raise at render time, not silently produce empty strings.
- **`create_eval` pulls datasets from Braintrust UI** — nothing stored locally; dataset must exist before running an eval.
- **pytest-xdist:** workers read the committed `.langchain_cache.json` plus a per-worker `_gwN.json` overlay.

## Vocabulary

- **FilePrompt** — immutable parsed prompt: name, version, model, model params, ordered message templates. Identity = source path on disk.
- **PromptRunnable[OutputT]** — stateless call object combining a `FilePrompt` + Pydantic output schema. Construct once; call `ainvoke({...})` per request.

## Data owned

None.

## How it's tested

Unit tests in `app/core/llm/test/` — frontmatter parsing, message splitting, render-with-missing-var, retry-then-give-up, env patching, `LLMTestCache` key derivation + JSON round-trip + cache-miss-loud-failure. `PromptRunnable` tests override `_build_model` directly — no `@patch`.
