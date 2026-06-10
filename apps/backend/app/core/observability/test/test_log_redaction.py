"""Placeholder — log redaction via _redact_secrets was removed.

Secrets self-mask via SecretStr at every module boundary; the recursive
key-name scrubber was redundant. See core/observability/service.py for
the current structlog processor chain.
"""
