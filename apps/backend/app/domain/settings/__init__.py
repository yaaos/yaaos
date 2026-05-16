"""domain/settings — onboarding status aggregator + onboarding-contributor registry."""

from app.domain.settings import web  # noqa: F401 — registers HTTP routes
from app.domain.settings.service import (
    OnboardingStatus,
    get_onboarding_status,
    register_onboarding_contributor,
)

__all__ = [
    "OnboardingStatus",
    "get_onboarding_status",
    "register_onboarding_contributor",
]
