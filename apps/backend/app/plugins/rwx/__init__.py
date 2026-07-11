"""plugins/rwx — RWX API key validator for core/api_keys."""

from app.plugins.rwx.service import bootstrap, validate_rwx_token

__all__ = [
    "bootstrap",
    "validate_rwx_token",
]

# Register the RWX validator at import time.
bootstrap()
