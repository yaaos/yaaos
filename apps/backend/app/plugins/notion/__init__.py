"""plugins/notion — Notion hosted-MCP IntegrationProvider."""

from app.plugins.notion.service import NotionProvider, bootstrap, set_notion_provider_for_tests

__all__ = ["NotionProvider", "bootstrap", "set_notion_provider_for_tests"]

# Register at import time.
bootstrap()
