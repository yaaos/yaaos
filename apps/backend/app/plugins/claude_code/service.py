"""Claude Code CLI wrapper. Implements `domain/coding_agent.CodingAgentPlugin`.

Vendor-only: this module talks to Anthropic's Claude Code CLI and nothing else.
It owns prompt assembly (system framing + persona + diff/lessons/comments),
the output JSON schema appendix, subprocess invocation via the workspace, and
parsing the agent's response into vendor-neutral `vcs.Finding`s.

Test-mode (stub/replay) wrapping is handled by the `testing/` layer's
`StubCodingAgentPlugin` — see `app.testing.stub_coding_agent`. The bootstrap
in `app/main.py` swaps real plugins for stubs when `YAAOF_CODING_AGENT_STUB`
is set; this file never branches on it.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import httpx
import structlog
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.primitives import PluginMeta
from app.core.workspace import Workspace, WorkspaceExecError
from app.domain.coding_agent import (
    HealthStatus,
    InvocationStatus,
    InvocationTelemetry,
    ReplyContext,
    ReplyResult,
    ReviewContext,
    ReviewResult,
    ValidationResult,
    register_coding_agent_plugin,
)
from app.domain.vcs import Finding, FindingSnippetLine
from app.plugins.claude_code.models import ClaudeCodeSettingsRow

log = structlog.get_logger("claude_code")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ── Plugin-internal response schemas ──────────────────────────────────────────
# These describe the JSON shape we ask Claude Code to emit. They never leak out
# of this plugin — the public Protocol returns `vcs.Finding`/`ReviewResult`.


class _FindingSnippetLine(BaseModel):
    line_number: int
    kind: Literal["context", "add", "del"]
    text: str


class _FindingDto(BaseModel):
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    severity: Literal["must-fix", "nit", "suggestion", "info"]
    title: str
    body: str
    rationale: str | None = None
    snippet: list[_FindingSnippetLine] | None = None
    applied_lesson_ids: list[UUID] = []


class _FindingList(BaseModel):
    findings: list[_FindingDto]


class _ReplyResponse(BaseModel):
    body: str


# ── Prompt assembly ───────────────────────────────────────────────────────────


def _assemble_review_prompt(ctx: ReviewContext) -> str:
    parts: list[str] = [
        f"# Agent: {ctx.agent_name}",
        "",
        ctx.persona.strip(),
        "",
    ]
    if ctx.language_hint:
        parts.extend(
            [
                "## Repository language",
                f"This repository is primarily {ctx.language_hint}.",
                "",
            ]
        )
    parts.extend(
        [
            "## Pull request",
            f"### Title\n{ctx.pr.title}",
            f"### Description\n{ctx.pr.body or '(no description)'}",
            "",
            "## Diff",
            "```diff",
            ctx.diff.raw.strip() or "(no diff)",
            "```",
        ]
    )
    if ctx.lessons:
        parts.extend(
            [
                "",
                "## Lessons learned from past reviews",
                "Apply these when reviewing this PR.",
                "",
            ]
        )
        for lesson in ctx.lessons:
            parts.append(f"### {lesson.title}\n_lesson_id: {lesson.id}_\n{lesson.body}")
    if ctx.prior_yaaof_comment_bodies:
        parts.extend(
            [
                "",
                "## Prior comments from sibling review agents",
                "Don't duplicate them; build on or disagree.",
                "",
            ]
        )
        for body in ctx.prior_yaaof_comment_bodies[:20]:
            parts.append(f"- {body[:200]}")
    return "\n".join(parts)


def _assemble_reply_prompt(ctx: ReplyContext) -> str:
    return (
        f"# Agent: {ctx.agent_name}\n\n"
        f"{ctx.persona.strip()}\n\n"
        f"A human replied to your earlier comment:\n\n> {ctx.reply_body}\n\n"
        f"## Diff (for context)\n```diff\n{ctx.diff.raw}\n```\n"
        "Reply with a short follow-up in JSON form."
    )


def _schema_appendix(response_model: type[BaseModel]) -> str:
    return (
        "\n\n## Output Format (STRICT)\n\n"
        "Respond with EXACTLY a JSON object matching this schema. No markdown fences. "
        "No commentary. No preamble. Your response must start with `{` and end with `}`.\n\n"
        f"{json.dumps(response_model.model_json_schema(), indent=2)}\n"
    )


# ── Verdict ───────────────────────────────────────────────────────────────────


def _compute_state(findings: list[_FindingDto]) -> Literal["APPROVED", "CHANGES_REQUESTED", "COMMENT"]:
    if not findings:
        return "APPROVED"
    if any(f.severity == "must-fix" for f in findings):
        return "CHANGES_REQUESTED"
    return "COMMENT"


def _dto_to_finding(dto: _FindingDto) -> Finding:
    snippet: list[FindingSnippetLine] | None = None
    if dto.snippet:
        snippet = [
            FindingSnippetLine(line_number=s.line_number, kind=s.kind, text=s.text) for s in dto.snippet
        ]
    return Finding(
        file=dto.file,
        line_start=dto.line_start,
        line_end=dto.line_end,
        severity=dto.severity,
        title=dto.title,
        body=dto.body,
        rationale=dto.rationale,
        snippet=snippet,
        applied_lesson_ids=dto.applied_lesson_ids,
    )


# ── Plugin ────────────────────────────────────────────────────────────────────


class ClaudeCodePlugin:
    meta = PluginMeta(
        id="claude_code",
        type="coding_agent",
        display_name="Claude Code",
        description="Wraps Anthropic's Claude Code CLI to run code reviews and replies.",
        docs_url="https://docs.claude.com/en/docs/claude-code",
    )

    async def _load_settings_for_invocation(self) -> tuple[str | None, str | None, int]:
        """Returns (decrypted_api_key, cli_path, timeout_seconds)."""
        async with db_session() as s:
            row = (await s.execute(select(ClaudeCodeSettingsRow).limit(1))).scalar_one_or_none()
        if row is None:
            return None, None, 600
        api_key: str | None = None
        if row.encrypted_anthropic_api_key:
            try:
                fernet = Fernet(get_settings().yaaof_encryption_key.encode())
                api_key = fernet.decrypt(row.encrypted_anthropic_api_key).decode()
            except InvalidToken:
                log.warning("claude_code.api_key_decrypt_failed")
        return api_key, row.cli_path, row.default_timeout_seconds

    async def review(self, workspace: Workspace, context: ReviewContext) -> ReviewResult:
        prep = await self._prepare_invocation(context.agent_config)
        if isinstance(prep, ReviewResult):
            return prep
        argv, env, timeout = prep

        full_prompt = _assemble_review_prompt(context) + _schema_appendix(_FindingList)

        envelope = await self._run_and_parse_envelope(workspace, argv, env, full_prompt, timeout)
        if isinstance(envelope, ReviewResult):
            return envelope
        agent_text, telemetry = envelope

        try:
            parsed_dict = json.loads(agent_text)
            parsed = _FindingList.model_validate(parsed_dict)
        except (json.JSONDecodeError, ValidationError) as e:
            return ReviewResult(
                status=InvocationStatus.PARSE_FAILURE,
                telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
                error_message=f"agent response didn't match _FindingList: {e}",
            )

        return ReviewResult(
            status=InvocationStatus.SUCCESS,
            findings=[_dto_to_finding(f) for f in parsed.findings],
            state=_compute_state(parsed.findings),
            summary_body=None,
            lesson_ids_consulted=[lesson.id for lesson in context.lessons],
            telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
        )

    async def reply(self, workspace: Workspace, context: ReplyContext) -> ReplyResult:
        prep = await self._prepare_invocation(context.agent_config)
        if isinstance(prep, ReviewResult):
            return ReplyResult(
                status=prep.status,
                telemetry=prep.telemetry,
                error_message=prep.error_message,
            )
        argv, env, timeout = prep

        full_prompt = _assemble_reply_prompt(context) + _schema_appendix(_ReplyResponse)

        envelope = await self._run_and_parse_envelope(workspace, argv, env, full_prompt, timeout)
        if isinstance(envelope, ReviewResult):
            return ReplyResult(
                status=envelope.status,
                telemetry=envelope.telemetry,
                error_message=envelope.error_message,
            )
        agent_text, telemetry = envelope

        try:
            parsed_dict = json.loads(agent_text)
            parsed = _ReplyResponse.model_validate(parsed_dict)
        except (json.JSONDecodeError, ValidationError) as e:
            return ReplyResult(
                status=InvocationStatus.PARSE_FAILURE,
                telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
                error_message=f"agent response didn't match _ReplyResponse: {e}",
            )

        return ReplyResult(
            status=InvocationStatus.SUCCESS,
            body=parsed.body,
            telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
        )

    async def _prepare_invocation(
        self, agent_config: dict[str, Any]
    ) -> tuple[list[str], dict[str, str], int] | ReviewResult:
        """Load settings, build argv + env. Returns ReviewResult on early failure.

        (Reply path coerces the result; same error shape applies.)
        """
        api_key, cli_path_setting, default_timeout = await self._load_settings_for_invocation()
        if not api_key:
            return ReviewResult(
                status=InvocationStatus.AGENT_ERROR,
                error_message="ANTHROPIC_API_KEY not set in claude_code_settings",
            )
        cli_path = cli_path_setting or shutil.which("claude")
        if not cli_path:
            return ReviewResult(
                status=InvocationStatus.AGENT_ERROR,
                error_message="claude binary not found on PATH or in claude_code_settings.cli_path",
            )

        env = os.environ.copy()
        env["ANTHROPIC_API_KEY"] = api_key
        timeout = agent_config.get("timeout_seconds") or default_timeout
        argv = [
            cli_path,
            "--print",
            "--output-format=json",
            "--permission-mode=bypassPermissions",
            "--allowed-tools=Read,Glob,Grep,LS,NotebookRead,TodoWrite,WebFetch,WebSearch",
        ]
        if agent_config.get("model"):
            argv += [f"--model={agent_config['model']}"]
        if agent_config.get("max_turns"):
            argv += [f"--max-turns={agent_config['max_turns']}"]
        return argv, env, timeout

    async def _run_and_parse_envelope(
        self,
        workspace: Workspace,
        argv: list[str],
        env: dict[str, str],
        full_prompt: str,
        timeout: int,
    ) -> tuple[str, InvocationTelemetry] | ReviewResult:
        """Run the CLI via the workspace; parse the wrapper envelope.

        Returns (agent_text, telemetry) on success, or a `ReviewResult` carrying
        the failure status. Reply path adapts the ReviewResult shape.
        """
        try:
            result = await workspace.run_coding_agent_cli(
                argv=argv,
                env=env,
                stdin=full_prompt.encode("utf-8"),
                timeout_seconds=timeout,
            )
        except WorkspaceExecError as e:
            return ReviewResult(
                status=InvocationStatus.AGENT_ERROR,
                error_message=f"could not spawn claude: {e}",
            )

        telemetry = InvocationTelemetry(latency_ms=result.duration_ms, raw_stderr=result.stderr)

        if result.timed_out:
            return ReviewResult(
                status=InvocationStatus.TIMEOUT,
                telemetry=telemetry,
                error_message=f"claude did not return within {timeout}s",
            )

        if result.exit_code != 0:
            first_line = result.stderr.splitlines()[0] if result.stderr else ""
            return ReviewResult(
                status=InvocationStatus.AGENT_ERROR,
                telemetry=telemetry.model_copy(update={"raw_output": result.stdout}),
                error_message=f"claude exited {result.exit_code}: {first_line}",
            )

        try:
            envelope = json.loads(result.stdout)
            agent_text = envelope.get("result", "")
            usage = envelope.get("usage", {})
            tokens_in = usage.get("input_tokens")
            tokens_out = usage.get("output_tokens")
            cost = envelope.get("total_cost_usd")
            cost_usd = Decimal(str(cost)) if cost is not None else None
        except (json.JSONDecodeError, AttributeError, KeyError) as e:
            return ReviewResult(
                status=InvocationStatus.AGENT_ERROR,
                telemetry=telemetry.model_copy(update={"raw_output": result.stdout}),
                error_message=f"could not parse claude wrapper output: {e}",
            )

        telemetry = telemetry.model_copy(
            update={"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost_usd}
        )
        return agent_text, telemetry

    async def validate_config(self, agent_config: dict[str, Any]) -> ValidationResult:
        errors: list[str] = []
        if "timeout_seconds" in agent_config:
            v = agent_config["timeout_seconds"]
            if not isinstance(v, int) or v <= 0:
                errors.append("timeout_seconds must be a positive int")
        if "max_turns" in agent_config:
            v = agent_config["max_turns"]
            if not isinstance(v, int) or v <= 0:
                errors.append("max_turns must be a positive int")
        if "model" in agent_config:
            v = agent_config["model"]
            if not isinstance(v, str) or not v:
                errors.append("model must be a non-empty string")
        unknown = set(agent_config) - {"timeout_seconds", "max_turns", "model"}
        errors.extend(f"unknown config key: {k}" for k in unknown)
        return ValidationResult(valid=not errors, errors=errors)

    async def health_check(self) -> HealthStatus:
        api_key, cli_path_setting, _ = await self._load_settings_for_invocation()
        if not api_key:
            return HealthStatus(healthy=False, message="anthropic api key not set", checked_at=_utcnow())
        cli_path = cli_path_setting or shutil.which("claude")
        if not cli_path:
            return HealthStatus(healthy=False, message="claude binary not found", checked_at=_utcnow())
        # Cached probe — verifies the key actually authenticates against Anthropic.
        # Cache TTL keeps the cost low (~1 request per 5min per running process).
        ok, message = await _probe_anthropic_auth(api_key)
        return HealthStatus(healthy=ok, message=message, checked_at=_utcnow())


_plugin = ClaudeCodePlugin()


# ── Anthropic auth probe ──────────────────────────────────────────────────────
# Keyed by sha256(key) so cache survives across re-reads of the same value and
# automatically resets when the key changes. `_set_anthropic_key` also flushes
# explicitly so a rotation never serves a stale cached result.

_AUTH_TTL = timedelta(minutes=5)
_AUTH_CACHE: dict[str, tuple[bool, str, datetime]] = {}


def _key_fingerprint(key: str) -> str:
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(key.encode()).hexdigest()


async def _probe_anthropic_auth(api_key: str) -> tuple[bool, str]:
    """Return (healthy, message). Cached for `_AUTH_TTL` per key fingerprint."""
    fp = _key_fingerprint(api_key)
    now = _utcnow()
    cached = _AUTH_CACHE.get(fp)
    if cached and cached[2] > now:
        return cached[0], cached[1]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
        if r.status_code == 200:
            result = (True, "ok")
        elif r.status_code in (401, 403):
            result = (False, "anthropic api key is invalid")
        else:
            result = (False, f"anthropic api returned HTTP {r.status_code}")
    except (httpx.HTTPError, OSError) as e:
        result = (False, f"could not reach anthropic api: {e}")
    _AUTH_CACHE[fp] = (result[0], result[1], now + _AUTH_TTL)
    return result


def _invalidate_auth_cache() -> None:
    _AUTH_CACHE.clear()


async def _onboarding_anthropic_key_set(org_id: UUID) -> bool:
    """Settings contributor — returns True iff a working key is present.

    "Set" means: there's an encrypted row in the DB AND the key actually
    authenticates against Anthropic. A saved-but-invalid key (e.g., a typo or a
    rotated/revoked credential) does not satisfy the prereq — the onboarding
    stepper would otherwise stay green when reviews would fail.

    The auth probe is cached by `_probe_anthropic_auth` (5-minute TTL per key
    fingerprint), so a 5-second polling dashboard makes at most one HTTP call
    per 5 minutes per key.
    """
    async with db_session() as s:
        row = (
            await s.execute(select(ClaudeCodeSettingsRow).where(ClaudeCodeSettingsRow.org_id == org_id))
        ).scalar_one_or_none()
    if row is None or row.encrypted_anthropic_api_key is None:
        return False
    try:
        fernet = Fernet(get_settings().yaaof_encryption_key.encode())
        api_key = fernet.decrypt(row.encrypted_anthropic_api_key).decode()
    except InvalidToken:
        return False
    healthy, _ = await _probe_anthropic_auth(api_key)
    return healthy


async def _set_anthropic_key(org_id: UUID, raw_key: str) -> None:
    """Encrypt + upsert the Anthropic key on `claude_code_settings`."""
    from uuid import uuid4  # noqa: PLC0415

    fernet = Fernet(get_settings().yaaof_encryption_key.encode())
    enc = fernet.encrypt(raw_key.encode())
    async with db_session() as s:
        row = (
            await s.execute(select(ClaudeCodeSettingsRow).where(ClaudeCodeSettingsRow.org_id == org_id))
        ).scalar_one_or_none()
        if row is None:
            row = ClaudeCodeSettingsRow(
                id=uuid4(),
                org_id=org_id,
                encrypted_anthropic_api_key=enc,
                default_timeout_seconds=600,
            )
            s.add(row)
        else:
            row.encrypted_anthropic_api_key = enc
        await s.commit()
    # Rotation should never serve a stale "healthy" verdict from the previous key.
    _invalidate_auth_cache()


def bootstrap() -> None:
    from app.domain.settings import register_onboarding_contributor  # noqa: PLC0415

    register_coding_agent_plugin(_plugin)
    register_onboarding_contributor("anthropic_key_set", _onboarding_anthropic_key_set)


def get_plugin() -> ClaudeCodePlugin:
    return _plugin


def get_set_anthropic_key():
    """Public accessor for the credential setter — used by the plugin's web routes."""
    return _set_anthropic_key
