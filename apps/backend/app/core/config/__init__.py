"""core/config — boot-time configuration via pydantic-settings."""

from app.core.config.service import Settings, get_settings

__all__ = ["Settings", "get_settings"]
