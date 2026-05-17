"""Gateway routing for direct LLM calls.

When Braintrust gateway settings are present, point LangChain's chat-model
factory at the gateway by setting the provider `_API_BASE` / `_API_KEY` env
vars. Called explicitly from app startup — not as a module-import side effect
— so test order can't accidentally rewrite the process env.
"""

from __future__ import annotations

import os

import structlog

from app.core.config import get_settings

log = structlog.get_logger("core.llm.gateway")


def configure_gateway() -> None:
    """Route `init_chat_model` calls at Braintrust if configured; no-op otherwise.

    Sets both `ANTHROPIC_*` and `OPENAI_*` because callers may use either
    provider; the gateway dispatches by model id. When settings are missing,
    LangChain falls back to its normal env-var resolution (direct provider).
    """
    settings = get_settings()
    if not (settings.braintrust_api_key and settings.braintrust_api_url):
        log.info("llm.gateway.skipped", reason="settings_missing")
        return

    os.environ["ANTHROPIC_API_BASE"] = settings.braintrust_api_url
    os.environ["ANTHROPIC_API_KEY"] = settings.braintrust_api_key
    os.environ["OPENAI_API_BASE"] = settings.braintrust_api_url
    os.environ["OPENAI_API_KEY"] = settings.braintrust_api_key
    log.info("llm.gateway.configured", base_url=settings.braintrust_api_url)
