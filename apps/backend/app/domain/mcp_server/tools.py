"""FastMCP server instance + tool registry for `domain/mcp_server`.

The `mcp` FastMCP server object is the transport and tool-registry scaffold.
Auth is enforced via `YaaosTokenVerifier` — a `TokenVerifier` subclass that
hash-looks-up the bearer in `mcp_access_tokens` and puts the resolved principal
in the FastMCP `AccessToken.claims` dict.  Tool handlers read the principal via
`mcp.server.auth.middleware.auth_context.get_access_token()`.

Tool authoring rules:
  - Each tool is a 1:1 wrapper over a public service function.
  - Org comes from the principal, never from tool args.
  - Write tools (`create_ticket`, `add_attachment`, `start_run`) require builder+
    role (equivalent to `Action.REVIEWER_WRITE`).
  - Tool-level errors mirror the wrapped service's raised exceptions as JSON-RPC
    error data (code -32001 for not-found, -32002 for constraint violations,
    -32004 for auth / role failures).

FastMCP's `verify_token` hook:
  The `YaaosTokenVerifier.verify_token(token)` method is async-safe and opens
  its own DB session so it composes cleanly with FastMCP's middleware chain.
  The `McpPrincipal` is serialised into `AccessToken.claims` so tool handlers
  can reconstruct it without a second DB lookup.

Context requirement:
  Two service functions require contextvars beyond what the MCP middleware sets:
  - `pipelines.list_pipelines` calls `pipeline_referenced_by_call` → `require_org_context()`
  - `pipelines.get_run_overview` calls `require_org_context()` and, in the paused
    branch, `current_actor()` (reads `user_id_var`).
  Both are wrapped in `_mcp_tool_context(principal)` which sets `org_id_var` via
  `org_context()` and then sets `user_id_var` from `principal.user_id`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.auth import TokenVerifier
from mcp import McpError
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import ErrorData
from pydantic import SecretStr

from app.core.audit_log import Actor, ActorKind
from app.core.auth import Role, org_context, user_id_var
from app.core.database import session as db_session
from app.domain.artifacts import ArtifactNotFoundError
from app.domain.artifacts import get as artifacts_get
from app.domain.artifacts import list_for_ticket as artifacts_list_for_ticket
from app.domain.attachments import AttachmentTooLargeError
from app.domain.attachments import add_attachment as attachments_add
from app.domain.attachments import list_attachments as list_attachments_svc
from app.domain.findings import list_open_for_ticket as findings_list_open
from app.domain.mcp_server.auth import McpAuthError, McpPrincipal, authenticate
from app.domain.pipelines import RunInFlightError, start_manual_run
from app.domain.pipelines import get_run_overview as pipelines_get_run_overview
from app.domain.pipelines import list_pipelines as pipelines_list_pipelines
from app.domain.tickets import create_from_manual, get_by_branch
from app.domain.tickets import get as tickets_get

# ---------------------------------------------------------------------------
# Token verifier — the FastMCP ↔ yaaos auth bridge.
# ---------------------------------------------------------------------------


class YaaosTokenVerifier(TokenVerifier):
    """Verify a yaaos MCP bearer by hash-lookup in `mcp_access_tokens`.

    On success, the resolved `McpPrincipal` is serialised into
    `AccessToken.claims` (keys: `user_id`, `org_id`, `role`) so tool
    handlers can reconstruct the principal without a second DB hit.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        async with db_session() as s:
            try:
                principal = await authenticate(SecretStr(token), session=s)
            except McpAuthError:
                return None

        return AccessToken(
            token=token,
            client_id="",
            scopes=[],
            expires_at=None,
            claims={
                "user_id": str(principal.user_id),
                "org_id": str(principal.org_id),
                "role": principal.role,
            },
        )


def _get_principal() -> McpPrincipal | None:
    """Read the MCP principal from the current auth context (set by FastMCP middleware)."""
    at = get_access_token()
    if at is None or not at.claims:
        return None
    try:
        return McpPrincipal(
            user_id=UUID(at.claims["user_id"]),
            org_id=UUID(at.claims["org_id"]),
            role=at.claims["role"],
        )
    except KeyError, ValueError:
        return None


def _require_principal() -> McpPrincipal:
    """Return the resolved principal or raise JSON-RPC -32004 (unauthenticated)."""
    p = _get_principal()
    if p is None:
        raise McpError(ErrorData(code=-32004, message="unauthenticated"))
    return p


def _require_writer_role(principal: McpPrincipal) -> None:
    """Raise JSON-RPC -32004 unless the principal has builder+ role.

    Builder is the minimum role for write operations — equivalent to the
    `Action.REVIEWER_WRITE` HTTP gate. All current org roles satisfy this
    (OWNER, ADMIN, BUILDER are the only roles). The check is present for
    correctness when a finer-grained role is added in the future.
    """
    if not Role(principal.role).covers(Role.BUILDER):
        raise McpError(ErrorData(code=-32004, message="insufficient role: builder required"))


@asynccontextmanager
async def _mcp_tool_context(principal: McpPrincipal):
    """Set `org_id_var` + `user_id_var` for tools whose wrapped services read them.

    `org_context()` sets `org_id_var`, `actor_kind_var`, `actor_id_var`.
    We additionally set `user_id_var` because `org_context()` does not, and
    `current_actor()` (called by `get_run_overview` in the paused branch)
    reads it.
    """
    async with org_context(principal.org_id, ActorKind.USER, actor_id=principal.user_id):
        tok = user_id_var.set(principal.user_id)
        try:
            yield
        finally:
            user_id_var.reset(tok)


# ---------------------------------------------------------------------------
# FastMCP server — transport + tool registry.
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "yaaos",
    auth=YaaosTokenVerifier(),
    stateless_http=True,
)


# ---------------------------------------------------------------------------
# Read-only lookup tool (original canary).
# ---------------------------------------------------------------------------


@mcp.tool()
async def find_ticket(branch_name: str) -> dict[str, Any]:
    """Find a yaaos ticket by its Git branch name.

    Returns the most recently created ticket on `branch_name` in the caller's
    org (fixed at consent time).

    Args:
        branch_name: The exact Git branch name to look up.

    Returns:
        {ticket_id, title, status} — all null when no ticket is found.
    """
    principal = _require_principal()

    async with db_session() as s:
        ticket = await get_by_branch(branch_name, org_id=principal.org_id, session=s)

    if ticket is None:
        return {"ticket_id": None, "title": None, "status": None}
    return {
        "ticket_id": str(ticket.id),
        "title": ticket.title,
        "status": ticket.status,
    }


# ---------------------------------------------------------------------------
# Write tools — require builder+ role.
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_ticket(
    title: str,
    repo_external_id: str,
    branch_name: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Create a manual yaaos ticket.

    The ticket belongs to the caller's org (resolved from the MCP bearer at
    consent time). If `idempotency_key` is supplied and a ticket with that key
    already exists in the org, the existing ticket is returned with
    `created=false`.

    Args:
        title: Human-readable title for the ticket.
        repo_external_id: External repo id the ticket targets (e.g. `"org/repo"`).
        branch_name: Optional Git branch to associate with the ticket.
        idempotency_key: Optional dedup key — same key → same ticket.

    Returns:
        {ticket_id, created} where `created` is false on an idempotent hit.
    """
    principal = _require_principal()
    _require_writer_role(principal)
    actor = Actor.user(user_id=principal.user_id)

    async with db_session() as s:
        ticket_id, created = await create_from_manual(
            org_id=principal.org_id,
            title=title,
            repo_external_id=repo_external_id,
            actor=actor,
            session=s,
            branch_name=branch_name,
            idempotency_key=idempotency_key,
        )
        await s.commit()

    return {"ticket_id": str(ticket_id), "created": created}


@mcp.tool()
async def add_attachment(
    ticket_id: str,
    filename: str,
    body: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Attach a document to a yaaos ticket.

    Parses optional YAML frontmatter from `body` to extract skill metadata
    (`produced_by_skill`, `artifact_type`, etc.). Parse failure is silently
    ignored — the attachment is stored as a plain context document.

    Args:
        ticket_id: UUID string of the target ticket.
        filename: Logical filename for the attachment.
        body: Document body (text; max 2 MiB).
        note: Optional human-readable note about the document.

    Returns:
        {attachment_id, produced_by_skill, artifact_type} — skill fields are
        null when frontmatter is absent or parse fails.
    """
    principal = _require_principal()
    _require_writer_role(principal)

    try:
        tid = UUID(ticket_id)
    except ValueError:
        raise McpError(ErrorData(code=-32602, message=f"invalid ticket_id: {ticket_id!r}"))

    actor = Actor.user(user_id=principal.user_id)

    try:
        async with db_session() as s:
            attachment = await attachments_add(
                tid,
                org_id=principal.org_id,
                filename=filename,
                body=body,
                note=note,
                actor=actor,
                session=s,
            )
            await s.commit()
    except AttachmentTooLargeError:
        raise McpError(ErrorData(code=-32602, message="body exceeds 2 MiB limit"))
    except LookupError:
        # Covers attachments.TicketNotFoundError (LookupError subclass).
        raise McpError(ErrorData(code=-32001, message=f"ticket not found: {ticket_id}"))

    return {
        "attachment_id": str(attachment.id),
        "produced_by_skill": attachment.produced_by_skill,
        "artifact_type": attachment.artifact_type,
    }


@mcp.tool()
async def start_run(
    ticket_id: str,
    pipeline_id: str,
    prompt: str | None = None,
    replace_in_flight: bool = False,
) -> dict[str, Any]:
    """Start a pipeline run on a yaaos ticket.

    Raises an error when the ticket or pipeline does not exist, or when a run
    is already in flight and `replace_in_flight` is false.

    Args:
        ticket_id: UUID string of the ticket to run.
        pipeline_id: UUID string of the pipeline definition to execute.
        prompt: Optional free-text input for the first skill stage.
        replace_in_flight: When true, kill any existing running/paused run
            before starting the new one.

    Returns:
        {run_id} — UUID of the newly created run.
    """
    principal = _require_principal()
    _require_writer_role(principal)

    try:
        tid = UUID(ticket_id)
    except ValueError:
        raise McpError(ErrorData(code=-32602, message=f"invalid ticket_id: {ticket_id!r}"))
    try:
        pid = UUID(pipeline_id)
    except ValueError:
        raise McpError(ErrorData(code=-32602, message=f"invalid pipeline_id: {pipeline_id!r}"))

    actor = Actor.user(user_id=principal.user_id)

    try:
        async with db_session() as s:
            run_id = await start_manual_run(
                org_id=principal.org_id,
                ticket_id=tid,
                pipeline_id=pid,
                actor=actor,
                input_text=prompt,
                replace_in_flight=replace_in_flight,
                triggered_by_user_id=principal.user_id,
                session=s,
            )
            await s.commit()
    except LookupError as exc:
        # Covers TicketNotFoundError and PipelineNotFoundError (both LookupError).
        raise McpError(ErrorData(code=-32001, message=str(exc) or "not found"))
    except RunInFlightError as exc:
        raise McpError(ErrorData(code=-32002, message=str(exc)))

    return {"run_id": str(run_id)}


# ---------------------------------------------------------------------------
# Read tools.
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_ticket(ticket_id: str) -> dict[str, Any]:
    """Fetch a yaaos ticket by ID.

    Args:
        ticket_id: UUID string of the ticket.

    Returns:
        {id, title, status, branch_name, repo_external_id, created_at}
    """
    principal = _require_principal()

    try:
        tid = UUID(ticket_id)
    except ValueError:
        raise McpError(ErrorData(code=-32602, message=f"invalid ticket_id: {ticket_id!r}"))

    try:
        ticket = await tickets_get(tid, org_id=principal.org_id)
    except LookupError:
        raise McpError(ErrorData(code=-32001, message=f"ticket not found: {ticket_id}"))

    return {
        "id": str(ticket.id),
        "title": ticket.title,
        "status": ticket.status,
        "branch_name": ticket.branch_name,
        "repo_external_id": ticket.repo_external_id,
        "created_at": ticket.created_at.isoformat(),
    }


@mcp.tool()
async def get_run_overview(ticket_id: str) -> dict[str, Any] | None:
    """Get the current run's overview for a ticket.

    Returns a tagged-union payload — `status` is one of `paused`,
    `in_flight`, or `terminal`. Returns null when the ticket has no run yet.

    Args:
        ticket_id: UUID string of the ticket.

    Returns:
        RunOverview dict or null.
    """
    principal = _require_principal()

    try:
        tid = UUID(ticket_id)
    except ValueError:
        raise McpError(ErrorData(code=-32602, message=f"invalid ticket_id: {ticket_id!r}"))

    async with _mcp_tool_context(principal):
        async with db_session() as s:
            overview = await pipelines_get_run_overview(tid, session=s)

    if overview is None:
        return None
    return overview.model_dump(mode="json")


@mcp.tool()
async def list_findings(ticket_id: str) -> list[dict[str, Any]]:
    """List open findings for a ticket.

    Args:
        ticket_id: UUID string of the ticket.

    Returns:
        [{id, handle, severity, body, file, line}] — open findings only.
    """
    principal = _require_principal()

    try:
        tid = UUID(ticket_id)
    except ValueError:
        raise McpError(ErrorData(code=-32602, message=f"invalid ticket_id: {ticket_id!r}"))

    async with db_session() as s:
        findings = await findings_list_open(principal.org_id, tid, session=s)

    return [
        {
            "id": str(f.id),
            "handle": f.handle,
            "severity": f.severity,
            "body": f.body,
            "file": f.code_file,
            "line": f.code_line,
        }
        for f in findings
    ]


@mcp.tool()
async def list_artifacts(ticket_id: str) -> list[dict[str, Any]]:
    """List artifact groups (by stage) for a ticket.

    Each group covers one pipeline stage and contains version metadata (no
    body). Fetch body via `get_artifact`.

    Args:
        ticket_id: UUID string of the ticket.

    Returns:
        [{stage_name, versions: [{id, version, is_final, created_at, adopted_from_attachment_id}]}]
    """
    principal = _require_principal()

    try:
        tid = UUID(ticket_id)
    except ValueError:
        raise McpError(ErrorData(code=-32602, message=f"invalid ticket_id: {ticket_id!r}"))

    async with db_session() as s:
        groups = await artifacts_list_for_ticket(principal.org_id, tid, session=s)

    return [
        {
            "stage_name": g.stage_name,
            "versions": [
                {
                    "id": str(v.id),
                    "version": v.version,
                    "is_final": v.is_final,
                    "created_at": v.created_at.isoformat(),
                    "adopted_from_attachment_id": (
                        str(v.adopted_from_attachment_id) if v.adopted_from_attachment_id else None
                    ),
                }
                for v in g.versions
            ],
        }
        for g in groups
    ]


@mcp.tool()
async def get_artifact(artifact_id: str) -> dict[str, Any]:
    """Fetch one artifact version, including body.

    Args:
        artifact_id: UUID string of the artifact.

    Returns:
        {id, stage_name, version, is_final, body, created_at, adopted_from_attachment_id}
    """
    principal = _require_principal()

    try:
        aid = UUID(artifact_id)
    except ValueError:
        raise McpError(ErrorData(code=-32602, message=f"invalid artifact_id: {artifact_id!r}"))

    try:
        async with db_session() as s:
            artifact = await artifacts_get(aid, org_id=principal.org_id, session=s)
    except ArtifactNotFoundError:
        raise McpError(ErrorData(code=-32001, message=f"artifact not found: {artifact_id}"))

    return {
        "id": str(artifact.id),
        "stage_name": artifact.stage_name,
        "version": artifact.version,
        "is_final": artifact.is_final,
        "body": artifact.body,
        "created_at": artifact.created_at.isoformat(),
        "adopted_from_attachment_id": (
            str(artifact.adopted_from_attachment_id) if artifact.adopted_from_attachment_id else None
        ),
    }


@mcp.tool()
async def list_attachments(ticket_id: str) -> list[dict[str, Any]]:
    """List attachment metadata for a ticket (no bodies).

    Fetch body via the REST endpoint or `get_attachment` (not yet exposed as
    an MCP tool; bodies can be large).

    Args:
        ticket_id: UUID string of the ticket.

    Returns:
        [{id, filename, produced_by_skill, artifact_type, note, attached_at}]
    """
    principal = _require_principal()

    try:
        tid = UUID(ticket_id)
    except ValueError:
        raise McpError(ErrorData(code=-32602, message=f"invalid ticket_id: {ticket_id!r}"))

    async with db_session() as s:
        metas = await list_attachments_svc(tid, org_id=principal.org_id, session=s)

    return [
        {
            "id": str(m.id),
            "filename": m.filename,
            "produced_by_skill": m.produced_by_skill,
            "artifact_type": m.artifact_type,
            "note": m.note,
            "attached_at": m.attached_at.isoformat(),
        }
        for m in metas
    ]


@mcp.tool()
async def list_pipelines() -> list[dict[str, Any]]:
    """List pipeline definitions for the caller's org.

    Args:
        (none — org is inferred from the MCP bearer)

    Returns:
        [{id, name, description}] ordered by name.
    """
    principal = _require_principal()

    async with _mcp_tool_context(principal):
        async with db_session() as s:
            summaries = await pipelines_list_pipelines(principal.org_id, session=s)

    return [
        {
            "id": str(p.id),
            "name": p.name,
            "description": p.description,
        }
        for p in summaries
    ]
