"""Isolation fixtures for service tests.

Provides pytest fixtures that reset per-module singletons to a clean state
before each test. All resets are performed by calling each module's production
registration/deregistration APIs — no direct submodule attribute access.
"""
