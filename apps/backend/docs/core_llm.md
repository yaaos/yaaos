# core/llm

> Mechanics for direct, single-shot, structured LLM calls. Prompts live in the calling domain module — this module owns call mechanics only.

## Purpose

Direct text-only LLM calls with prompts loaded from files and outputs validated against a Pydantic schema. Code-touching agent work goes through [`domain/coding_agent`](domain_coding_agent.md), not here. Owns: prompt-file parsing, jinja2 templating, LangChain runnable construction, structured-output validation, retries, Braintrust gateway routing. Does NOT own prompts, schemas, eval infrastructure, agent loops, RAG, or cost budgeting.

## Public interface

Exported from `app/core/llm/__init__.py`:

- Types — `FilePrompt`, `ParsedMessage`.
- Loading — `load_prompt(path)`.
- Invocation — `PromptRunnable[OutputT]` (constructed with a `FilePrompt` + Pydantic schema; exposes `async ainvoke(input_vars)`).
- Setup — `configure_gateway()` (call once from app startup).
- Exceptions — `LLMError`, `MalformedOutput`, `PromptParseError`.

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

### State machines

None.

## Data owned

None. No DB tables. Process env vars set by `configure_gateway()` are the only mutable state.

## How it's tested

- Unit tests in `app/core/llm/test/` — frontmatter parsing, message splitting, render-with-missing-var, retry-then-give-up, env patching.
- `PromptRunnable` tests substitute the chat model by subclassing and overriding `_build_model` — no `@patch`.
- Test caching: when a downstream caller exercises real LLM calls in tests, wire `langchain.cache.SQLiteCache` pointed at `apps/backend/test/.llm-cache.sqlite` (gitignored) in the harness; evals deliberately bypass the cache.
