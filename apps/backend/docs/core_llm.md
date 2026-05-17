# core/llm

> Mechanics for direct, single-shot, structured LLM calls. Prompts live in the calling domain module — this module owns call mechanics only.

## Purpose

Direct text-only LLM calls with prompts loaded from files and outputs validated against a Pydantic schema. Code-touching agent work goes through [`domain/coding_agent`](domain_coding_agent.md), not here. Owns: prompt-file parsing, jinja2 templating, LangChain runnable construction, structured-output validation, retries, Braintrust gateway routing, file-colocated LLM test cache, thin `braintrust.Eval` wrapper. Does NOT own prompts, schemas, agent loops, RAG, cost budgeting, or eval fixtures/scorers (those live in the owner module under `<module>/eval/`).

## Public interface

Exported from `app/core/llm/__init__.py`:

- Types — `FilePrompt`, `ParsedMessage`.
- Loading — `load_prompt(path)`.
- Invocation — `PromptRunnable[OutputT]` (constructed with a `FilePrompt` + Pydantic schema; exposes `async ainvoke(input_vars)`).
- Setup — `configure_gateway()` (call once from app startup).
- Test cache — `LLMTestCache` (file-colocated JSON, committed to git). Auto-installed by the pytest plugin; no caller wiring needed.
- Eval helper — `create_eval(experiment_name, module_name, task, scores, dataset_name, max_concurrency=None)` — thin `braintrust.Eval(...)` wrapper. Owner modules supply task + scorers + dataset; eval files live under `<module>/eval/*.eval.py`.
- Exceptions — `LLMError`, `MalformedOutput`, `PromptParseError`.

Pytest plugin auto-loaded via `[project.entry-points."pytest11"]`. Provides `--allow-llm-calls` CLI flag, autouse `setup_llm_cache` session fixture, and an `allow_llm_calls` fixture for opt-in tests.

No HTTP routes.

## Module architecture

### Entities

None. The module is stateless mechanics — no persisted entities of its own.

### Key value objects

- `FilePrompt` — immutable parsed prompt: name, version, model, model params, ordered message templates. Identity = the source path on disk.
- `ParsedMessage` — one message slot with a role (`system`/`user`/`assistant`) and a raw jinja2 template body.
- `PromptRunnable[OutputT]` — call object. Stateless after construction; combines a `FilePrompt` + Pydantic output schema.

### Core user flows

1. **App startup** — `configure_gateway()` reads `BRAINTRUST_API_KEY` + `BRAINTRUST_API_URL` from settings and points provider `*_API_BASE`/`*_API_KEY` env vars at the gateway. If either is missing, no-op (direct provider keys take over).
2. **One-shot classification** — caller does `prompt = load_prompt(path)` once, builds `PromptRunnable(prompt, Schema)`, then `await runnable.ainvoke({...})` per call. Returns the parsed Pydantic instance.
3. **On malformed output** — `PromptRunnable` retries the same input once; raises `MalformedOutput` if the second attempt also fails validation. The audit-log line is the caller's responsibility.

### Prompt file format

One file per prompt. Extension `.prompt.md`. YAML frontmatter required: `name`, `version`, `model`. Other keys (`temperature`, `max_tokens`, ...) pass straight to `init_chat_model`. Body is split into messages by `<system>`, `<user>`, `<assistant>` tags; bodies are jinja2 templates rendered with `StrictUndefined` (missing variables raise). Markdown editors render the file natively.

### Gateway routing

`configure_gateway()` sets `ANTHROPIC_API_BASE` / `OPENAI_API_BASE` and the matching keys to the Braintrust gateway when configured. Per-call `user` tag = `f"{prompt.name}.v{prompt.version}"`; Braintrust groups rows by it without span wrapping. Called explicitly from `app/main.py` — never as an import side effect.

### LLM test cache (`LLMTestCache`)

File-colocated cache for LangChain LLM responses. One `.langchain_cache.json` per test directory, committed to git so every CI / contributor runs against the same responses.

- Key = `md5(json.dumps(semantic_fields, sort_keys=True))` where `semantic_fields` is a whitelist of prompt + LLM-config fields (message `role`/`content`/`type`/`tool_calls`/`name`, model name, temperature, top_p, frequency/presence_penalty, max_tokens, n, plus a `params` blob from the model's own serialization). Environment churn (UUIDs, API keys, base URLs, timeouts) does not invalidate the cache.
- HTML-unescape on `content` before hashing so Mustache-style `{{var}}` templating is stable across renderers.
- pytest-xdist aware: workers read the committed `.langchain_cache.json` AND a per-worker `_gw0.json` overlay.
- Cache miss with `allow_real_calls=False` raises a loud `RuntimeError` telling the dev to re-run with `--allow-llm-calls`. With the flag, real call runs and the response is appended.
- Serialization: langchain's own `dumps()`; deserialization uses `Reviver("all", valid_namespaces=["app", "langchain", "langchain_core"])` with `allowed_class_paths=None` so domain subclasses round-trip past the langchain 1.3+ class-path allowlist.

To populate or update a cache file:

1. `pytest --allow-llm-calls path/to/test_file.py` — real LLM calls run; responses get appended to the colocated `.langchain_cache.json`.
2. Commit the updated file.

### Eval helper (`create_eval`)

Thin wrapper around `braintrust.Eval(...)`. Owner modules call it from `<module>/eval/*.eval.py`. The wrapper:

- Pulls the dataset from Braintrust via `init_dataset(project=module_name, name=dataset_name)`. Datasets live in the Braintrust UI; nothing is stored locally.
- Wraps the caller's task with a `BraintrustCallbackHandler` rooted at each row's `hooks.span`, so the LangChain trace (prompt, response, any intermediate chain steps) attaches as a child span of the experiment row. Without that, you'd only see `(input, output, scores)` on the row and would have to correlate the gateway's flat log by timestamp + `user` tag to debug "why did this row score badly".
- Does NOT register prompts as Braintrust parameters. yaaof prompts are file-based (`<module>/llm/prompts/*.prompt.md`); the Braintrust prompt-A/B-test UI isn't used here.

Evals run locally; the experiment + traces ship to Braintrust via the standard `BRAINTRUST_API_KEY` env path. Each scorer is either an `autoevals` builtin or a hand-written `(output, expected) -> Score` function.

### State machines

None.

## Data owned

None. No DB tables. Process env vars set by `configure_gateway()` are the only mutable state.

## How it's tested

- Unit tests in `app/core/llm/test/` — frontmatter parsing, message splitting, render-with-missing-var, retry-then-give-up, env patching, `LLMTestCache` key derivation + JSON round-trip + cache-miss-loud-failure.
- `PromptRunnable` tests substitute the chat model by subclassing and overriding `_build_model` — no `@patch`.
- The pytest plugin's session-scoped `setup_llm_cache` autouse fixture wires `LLMTestCache` globally; tests that intentionally make LLM calls declare the `allow_llm_calls` fixture (skipped without the CLI flag).
