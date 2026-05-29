"""Workflow harness for multi-module service tests.

A thin coordinator that wires up the full webhook-to-review pipeline
(intake → reviewer → vcs.post_review) against real Postgres and stub
plugins, so a single service test can exercise a complete workflow without
duplicating composition-root setup.
"""
