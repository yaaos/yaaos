"""core/observability — structlog + conditional OTel SDK."""

from app.core.observability.service import configure, get_logger

__all__ = ["configure", "get_logger"]
