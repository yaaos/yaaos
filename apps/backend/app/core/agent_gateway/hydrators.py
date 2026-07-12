"""Claim-time credential hydration registry for agent commands.

`CommandHydrator` is the async callable registered per command kind.
`claim_next` calls the registered hydrator (if any) after selecting a row,
replacing the persisted payload with the hydrator's returned COMPLETE payload
before building the outbound `AgentCommand` DTO.

Credential values inside the hydrated payload MUST be `SecretStr` — they are
unwrapped only at the claim response's JSON encode boundary via the
`@field_serializer(when_used="json")` pattern used by `AgentConfig.api_keys`
and `InvokeCodexCommand.auth_json`.

Canonical import direction:
  core/coding_agent → core/agent_gateway  (registers ConfigUpdate hydrator)
  plugins/codex     → core/agent_gateway  (registers InvokeCodex hydrator)
  core/agent_gateway → core/agent_gateway.hydrators  (calls hydrators at claim)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class HydrationContext:
    """Gateway-supplied context passed to every `CommandHydrator` at claim time.

    Carries org_id (and any future gateway context) without polluting the
    persisted payload dict with magic `_`-prefixed keys.
    """

    org_id: UUID


# CommandHydrator:
#   First arg  — the persisted payload dict (no gateway context keys).
#   Second arg — HydrationContext carrying org_id and other gateway facts.
#   Third arg  — the claim-time AsyncSession (read-only use recommended).
#   Returns    — the COMPLETE outbound payload dict; credential values are
#                SecretStr, unwrapped by the relevant field_serializer at
#                wire-encode time.
#   Raises     — CredentialHydrationError on unresolvable credentials.
CommandHydrator = Callable[[dict[str, Any], HydrationContext, AsyncSession], Awaitable[dict[str, Any]]]


class CredentialHydrationError(Exception):
    """Raised by a `CommandHydrator` when credentials cannot be resolved.

    `user_message` is a user-facing string that rides the synthesized
    `completed_failure` event's `failure_reason` for run-bearing kinds
    so the pipeline UI surfaces a meaningful error instead of a bare
    "claim error".
    """

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


# ── Single-slot registry keyed by AgentCommandKind string ────────────────────

_HYDRATORS: dict[str, CommandHydrator] = {}


def register_command_hydrator(kind: str, hydrator: CommandHydrator) -> None:
    """Register a per-kind claim-time credential hydrator.

    Raises `ValueError` on a duplicate `kind` — double-wiring is a boot-time
    defect, not a runtime condition.  Tests reset via
    `clear_command_hydrators_for_tests`.
    """
    if kind in _HYDRATORS:
        raise ValueError(f"CommandHydrator already registered for kind {kind!r}")
    _HYDRATORS[kind] = hydrator


def get_command_hydrator(kind: str) -> CommandHydrator | None:
    """Return the registered hydrator for `kind`, or None when absent."""
    return _HYDRATORS.get(kind)


def clear_command_hydrators_for_tests() -> None:
    """Clear all registered hydrators.

    Used by test fixtures to reset between tests so registered production
    hydrators and stub hydrators do not bleed across test boundaries.
    Production code never calls this.
    """
    _HYDRATORS.clear()
