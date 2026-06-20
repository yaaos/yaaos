"""SecretsScan — pre-flight secrets detection LocalCommand.

Fetches the PR diff and runs `secrets_detection.detect_secrets`. On a match
returns `Outcome.success(label="skip")` and posts a warning Review comment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, ConfigDict

from app.core.workflow import CommandContext, Outcome

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("domain.reviewer.commands.secrets_scan")


class SecretsScanInputs(BaseModel):
    """Typed inputs for SecretsScan. Populated from the TicketSnapshot."""

    model_config = ConfigDict(frozen=True)
    org_id: UUID
    plugin_id: str
    pr_external_id: str | None = None


class SecretsScanOutputs(BaseModel):
    """Rule ID when a secret is detected; None otherwise."""

    model_config = ConfigDict(frozen=True)
    rule_id: str | None = None


class SecretsScan:
    """Pre-flight secrets gate.

    Fetches the PR diff and runs `secrets_detection.detect_secrets`. On a
    match returns `Outcome.success(label="skip")` and posts a warning Review.
    Reads all required fields from typed `SecretsScanInputs` — no provider
    lookups at execute time.
    """

    kind = "SecretsScan"
    restart_safe = True
    Inputs = SecretsScanInputs
    Outputs = SecretsScanOutputs

    async def execute(
        self,
        inputs: SecretsScanInputs,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> Outcome:
        del session
        if not inputs.pr_external_id:
            return Outcome.success(outputs=SecretsScanOutputs(rule_id=None))

        from app.core import vcs as _vcs  # noqa: PLC0415
        from app.domain.reviewer.secrets_detection import (  # noqa: PLC0415
            detect_secrets,
            secrets_warning_body,
        )

        try:
            diff = await _vcs.fetch_diff(inputs.plugin_id, inputs.org_id, inputs.pr_external_id)
        except Exception as exc:
            log.warning(
                "secrets_scan.diff_fetch_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return Outcome.success(outputs=SecretsScanOutputs(rule_id=None))

        rule_id = detect_secrets(diff)
        if rule_id is None:
            return Outcome.success(outputs=SecretsScanOutputs(rule_id=None))

        try:
            await _vcs.post_comment(
                inputs.plugin_id,
                inputs.org_id,
                inputs.pr_external_id,
                body=secrets_warning_body(rule_id),
            )
        except Exception as exc:
            span = trace.get_current_span()
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            log.exception(
                "secrets_scan.post_warning_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                rule_id=rule_id,
            )

        log.info(
            "secrets_scan.detected",
            workflow_execution_id=ctx.workflow_execution_id,
            ticket_id=ctx.ticket_id,
            rule_id=rule_id,
        )
        return Outcome.success(
            label="skip",
            outputs=SecretsScanOutputs(rule_id=rule_id),
        )
