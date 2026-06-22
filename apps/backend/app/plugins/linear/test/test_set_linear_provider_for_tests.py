"""Tests for set_linear_provider_for_tests isolation seam."""

import app.plugins.linear.service as _svc
from app.plugins.linear.service import LinearProvider, set_linear_provider_for_tests


def test_set_linear_provider_for_tests_swaps_for_block():
    """Inside the block the singleton is replaced with the provided instance."""
    replacement = LinearProvider()
    with set_linear_provider_for_tests(replacement) as got:
        assert got is replacement
        assert _svc._provider is replacement


def test_set_linear_provider_for_tests_restores_after_exit():
    """The original singleton is restored after block exit."""
    original = _svc._provider
    replacement = LinearProvider()

    with set_linear_provider_for_tests(replacement):
        assert _svc._provider is replacement

    assert _svc._provider is original


def test_set_linear_provider_for_tests_restores_on_exception():
    """The original singleton is restored even when the block raises."""
    original = _svc._provider

    try:
        with set_linear_provider_for_tests(LinearProvider()):
            raise RuntimeError("intentional")
    except RuntimeError:
        pass

    assert _svc._provider is original


def test_set_linear_provider_for_tests_default_creates_fresh():
    """Omitting the argument creates a fresh LinearProvider instance."""
    original = _svc._provider
    with set_linear_provider_for_tests() as got:
        assert isinstance(got, LinearProvider)
        assert got is not original
