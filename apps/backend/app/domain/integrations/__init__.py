"""domain/integrations — per-(org, provider) hosted-MCP OAuth credentials.

Skeleton at Phase 0. Phase 1 ships the IntegrationProvider Protocol +
service surface (connect / callback / refresh / clear / validate / update_allowlist).
"""

from app.domain.integrations.models import McpCredentialRow
from app.domain.integrations.types import IntegrationProvider

__all__ = ["IntegrationProvider", "McpCredentialRow"]
