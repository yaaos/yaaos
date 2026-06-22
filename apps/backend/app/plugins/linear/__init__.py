"""plugins/linear — Linear hosted-MCP IntegrationProvider."""

from app.plugins.linear.service import LinearProvider, bootstrap, set_linear_provider_for_tests

__all__ = ["LinearProvider", "bootstrap", "set_linear_provider_for_tests"]

# Register at import time.
bootstrap()
