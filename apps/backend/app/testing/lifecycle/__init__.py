"""testing/lifecycle — session-end aggregator for graceful shutdown."""

from app.testing.lifecycle.service import shutdown_runtime

__all__ = ["shutdown_runtime"]
