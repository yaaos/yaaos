"""domain/integrations — per-(org, provider) hosted-MCP OAuth credentials."""

from app.domain.integrations.service import (
    McpCredential,
    clear,
    connect_callback,
    create_credential,
    get,
    list_broken_credentials_for_org,
    update_allowlist,
    validate,
)
from app.domain.integrations.types import (
    _REGISTRY,
    BrokenCredentialsError,
    IntegrationError,
    IntegrationNotConnectedError,
    IntegrationProvider,
    ProviderConfig,
    ProviderNotRegisteredError,
    get_provider,
    known_providers,
    register_provider,
)

__all__ = [
    "_REGISTRY",
    "BrokenCredentialsError",
    "IntegrationError",
    "IntegrationNotConnectedError",
    "IntegrationProvider",
    "McpCredential",
    "ProviderConfig",
    "ProviderNotRegisteredError",
    "clear",
    "connect_callback",
    "create_credential",
    "get",
    "get_provider",
    "known_providers",
    "list_broken_credentials_for_org",
    "register_provider",
    "update_allowlist",
    "validate",
]
