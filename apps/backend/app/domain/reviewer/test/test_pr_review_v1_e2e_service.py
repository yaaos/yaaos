"""End-to-end composition test for the `pr_review_v1` workflow.

All Workspace-category steps dispatch over the wire (awaiting_agent). Tests
simulate each agent terminal event via `_advance_pending_agent_event`.

Asserts:
- CheckShouldReview (real body) reads admission signals from TicketSnapshot;
  non-draft non-fork PR advances past the skip gate.
- Workspace steps (ProvisionWorkspace, CodeReview, CleanupWorkspace) park in
  awaiting_agent; simulated agent events advance each one.
- PostFindings (real body, LOCAL) runs inline and persists FindingRows.
- Workflow terminates in `done` state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

import app.core.byok as byok
from app.core.audit_log import Actor
from app.core.tasks import drain_once, get_pending_task_names
from app.core.workflow import WorkflowState, get_execution_summary
from app.core.workspace import register_workspace_provider
from app.domain.orgs import create_org
from app.domain.reviewer.types import TicketSnapshot
from app.domain.reviewer.workflows import pr_review_v1
from app.domain.tickets import create_from_pr as create_ticket
from app.testing.e2e_setup import seed_agent as _seed_agent_for_tests
from app.testing.e2e_setup import seed_workspace as _seed_workspace_for_tests
from app.testing.workflow_harness import set_engine_for_tests


async def _seed_org_with_anthropic_key(db_session) -> UUID:  # type: ignore[no-untyped-def]
    """Seed a real orgs row + an anthropic byok key so CodeReview.dispatch finds one."""
    org = await create_org(db_session, slug=f"t-{uuid4().hex[:8]}", display_name="t")
    await byok.set(org.id, "anthropic", "sk-test-key", actor=Actor.system(), session=db_session)
    return org.id


class _StubWorkspaceProvider:
    """Doesn't clone anything — legacy stub; retained for test isolation."""

    plugin_id = "in_process"

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"working_dir": "/tmp/stub", "sha": spec.sha}

    async def destroy(self) -> None:  # type: ignore[no-untyped-def]
        return None

    async def health_check(self) -> None:  # type: ignore[no-untyped-def]
        return None

    async def run_coding_agent_cli(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def read_text(self, path):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, path, content):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture
def _registered_engine(workspace_providers_isolation):  # type: ignore[no-untyped-def]
    register_workspace_provider(_StubWorkspaceProvider())
    with set_engine_for_tests() as eng:
        eng.register_workflow(pr_review_v1)
        yield eng


async def _drain_workflow_outbox(db_session, *, max_iterations: int = 50) -> int:
    """Drain outbox until empty. Re-dispatches `taskiq_enqueue` rows into
    the matching task body via the broker's task registry."""
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None
        await decorated.original_func(**payload["args"])

    total = 0
    for _ in range(max_iterations):
        pending = await get_pending_task_names(db_session)
        if not pending:
            return total
        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        total += delivered
        if delivered == 0:
            break
    return total


async def _advance_pending_agent_event(  # type: ignore[no-untyped-def]
    db_session,
    wfx_id: str,
    outputs: dict[str, object] | None = None,
    *,
    outcome_label: str = "success",
):
    """Simulate the agent's terminal event for a Workspace step on
    `remote_agent` provider. Reads the workflow's `pending_agent_command_id`,
    enqueues `handle_agent_event` with the matching id + supplied outputs,
    then drains the outbox so the workflow advances to the next step (or
    DONE).
    """
    from app.core.tasks import enqueue  # noqa: PLC0415
    from app.core.workflow import HANDLE_AGENT_EVENT  # noqa: PLC0415

    wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
    assert wfx is not None
    assert wfx.state == WorkflowState.AWAITING_AGENT.value, (
        f"expected AWAITING_AGENT before agent event, got {wfx.state!r}"
    )
    assert wfx.pending_agent_command_id is not None
    await enqueue(
        HANDLE_AGENT_EVENT,
        args={
            "workflow_execution_id": wfx_id,
            "agent_command_id": str(wfx.pending_agent_command_id),
            "outcome_label": outcome_label,
            "outputs": outputs or {},
            "traceparent": None,
        },
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)


@pytest.mark.service
async def test_pr_review_v1_with_findings_persists_to_db(db_session, workspace_providers_isolation) -> None:  # type: ignore[no-untyped-def]
    """Full workflow walk verifying PostFindings (LOCAL) persists FindingRows.

    All Workspace steps park at AWAITING_AGENT. Each is advanced by a
    simulated agent event:
    - ProvisionWorkspace: event returns the seeded workspace_id.
    - CodeReview: event returns a CodeReviewResponse JSON in outputs; handle_response validates it.
    - CleanupWorkspace: event returns empty outputs.

    PostFindings (LOCAL) runs inline, validates the canonical finding, and
    persists a FindingRow with `finding_display_id=1`.
    """
    from app.core.vcs import VCSPullRequest as _VCSPullRequest  # noqa: PLC0415
    from app.domain.reviewer.models import FindingRow  # noqa: PLC0415
    from app.domain.tickets import create_from_pr as create_ticket2  # noqa: PLC0415
    from app.domain.tickets import upsert as upsert_pr  # noqa: PLC0415

    class _StubProviderWithFiles:
        plugin_id = "in_process"

        async def provision(self, spec):  # type: ignore[no-untyped-def]
            return {}

        async def destroy(self) -> None:  # type: ignore[no-untyped-def]
            return None

        async def health_check(self) -> None:  # type: ignore[no-untyped-def]
            return None

        async def run_coding_agent_cli(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def read_text(self, path):  # type: ignore[no-untyped-def]
            return None

        async def write_text(self, path, content):  # type: ignore[no-untyped-def]
            return None

    register_workspace_provider(_StubProviderWithFiles())

    org_id = await _seed_org_with_anthropic_key(db_session)

    # 2. Real ticket + PR rows so findings FK has somewhere to land.
    ext_id = f"e2e-{uuid4().hex[:6]}"
    ticket_id, _ = await create_ticket2(
        org_id=org_id,
        source_external_id=ext_id,
        title="t",
        description=None,
        repo_external_id="me/repo",
        plugin_id="github",
        idempotency_key=ext_id,
        payload={
            "is_draft": False,
            "is_fork": False,
            "labels": [],
            "author_login": "alice",
            "head_sha": "deadbeef",
            "base_sha": "babecafe",
        },
        session=db_session,
    )
    pr = await upsert_pr(
        _VCSPullRequest(
            plugin_id="github",
            repo_external_id="me/repo",
            external_id=f"pr-{ext_id}",
            number=42,
            title="t",
            body=None,
            author_login="alice",
            author_type="user",
            base_branch="main",
            head_branch="feature",
            base_sha="babecafe",
            head_sha="deadbeef",
            is_draft=False,
            is_fork=False,
            state="open",
            html_url="http://test",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        ticket_id=ticket_id,
        org_id=org_id,
        session=db_session,
    )
    pr_id = pr.id

    # CodeReview.dispatch requires owning_agent_id — seed a real agent row (FK constraint).
    agent_row = await _seed_agent_for_tests(org_id=org_id)
    seeded_ws_id = await _seed_workspace_for_tests(
        org_id=org_id,
        provider_id="in_process",
        sha="deadbeef",
        agent_id=agent_row["id"],
    )
    await db_session.commit()

    # CodeReview agent event returns a JSON string matching CodeReviewResponse shape.
    # `handle_response` calls `CodeReviewResponse.model_validate_json(output)` so the
    # output must be direct JSON, not stream-json. The run-sink extracts the `result`
    # field from stream-json in the real flow; the test simulates the already-extracted
    # output by passing `json.dumps(spy_finding_payload)` directly.
    spy_finding_payload = {
        "findings": [
            {
                "file": "src/foo.py",
                "line": 2,
                "category": "correctness",
                "severity": "blocker",
                "confidence": "verified",
                "rationale": "Unvalidated input passed to SQL query.",
                "rule_violated": "spy_rule",
                "rule_source": "yaaos",
                "suggested_fix": "Use parameterized queries.",
            }
        ]
    }
    spy_output = json.dumps(spy_finding_payload)

    from app.testing.stub_vcs import register_stub_vcs  # noqa: PLC0415

    snapshot = TicketSnapshot(
        ticket_id=ticket_id,
        org_id=org_id,
        plugin_id="github",
        repo_external_id="me/repo",
        pr_id=pr_id,
        pr_external_id=f"pr-{ext_id}",
        head_sha="deadbeef",
        base_sha="babecafe",
        is_draft=False,
        is_fork=False,
    )

    with set_engine_for_tests() as eng:
        eng.register_workflow(pr_review_v1)

        with register_stub_vcs(plugin_id="github"):
            wfx_id = await eng.start(
                workflow_name="pr_review_v1",
                ticket_id=str(ticket_id),
                workflow_input=snapshot,
                session=db_session,
            )
            await db_session.commit()
            # CheckShouldReview + SecretsScan (LOCAL) run inline; ProvisionWorkspace parks.
            await _drain_workflow_outbox(db_session)

            # ProvisionWorkspace: return the pre-seeded workspace_id.
            await _advance_pending_agent_event(db_session, wfx_id, outputs={"workspace_id": seeded_ws_id})
            # CodeReview: handle_response validates the JSON against CodeReviewResponse;
            # PostFindings receives typed findings from the Outcome.
            await _advance_pending_agent_event(
                db_session,
                wfx_id,
                outputs={"output": spy_output},
            )
            # PostFindings (LOCAL) ran inline. CleanupWorkspace parks.
            await _advance_pending_agent_event(db_session, wfx_id, outputs={})

        wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
        assert wfx.state == WorkflowState.DONE.value

        # FindingRow lands with canonical schema fields.
        rows = (
            (
                await db_session.execute(
                    select(FindingRow).where(FindingRow.pr_id == pr_id, FindingRow.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].severity == "blocker"
        assert rows[0].confidence == "verified"
        assert rows[0].category == "correctness"
        assert rows[0].finding_display_id == 1


@pytest.mark.service
async def test_pr_review_v1_runs_end_to_end_remote_agent(db_session, _registered_engine) -> None:  # type: ignore[no-untyped-def]
    """Full `pr_review_v1` walk to DONE. Workspace-category commands
    (`ProvisionWorkspace`, `CodeReview`, `CleanupWorkspace`) park at
    AWAITING_AGENT; the test simulates each terminal AgentEvent via
    `_advance_pending_agent_event`. Local commands (`CheckShouldReview`,
    `PostFindings`) execute inline on the control plane. Workflow ends DONE
    with no pending agent command.

    Uses `register_stub_vcs` so `ProvisionWorkspace.dispatch` can call
    `get_install_credentials` without a real GitHub App installation.
    """
    from app.testing.stub_vcs import register_stub_vcs  # noqa: PLC0415

    org_id = await _seed_org_with_anthropic_key(db_session)
    ticket_id, _ = await create_ticket(
        org_id=org_id,
        source_external_id="42",
        title="real-ticket",
        description=None,
        repo_external_id="me/repo",
        plugin_id="github",
        idempotency_key=f"e2e-remote-{uuid4()}",
        payload={
            "is_draft": False,
            "is_fork": False,
            "labels": ["enhancement"],
            "author_login": "alice",
            "pr_external_id": "42",
            "head_sha": "deadbeefcafef00d",
            "base_sha": "babecafe",
        },
        session=db_session,
    )

    snapshot = TicketSnapshot(
        ticket_id=ticket_id,
        org_id=org_id,
        plugin_id="github",
        repo_external_id="me/repo",
        pr_external_id="42",
        head_sha="deadbeefcafef00d",
        base_sha="babecafe",
        is_draft=False,
        is_fork=False,
        labels=("enhancement",),
        author_login="alice",
    )

    with register_stub_vcs(plugin_id="github"):
        wfx_id = await _registered_engine.start(
            workflow_name="pr_review_v1",
            ticket_id=str(ticket_id),
            workflow_input=snapshot,
            session=db_session,
        )
        await db_session.commit()

        # Initial drain — CheckShouldReview (Local) executes inline; then
        # ProvisionWorkspace (Workspace) dispatches and parks at AWAITING_AGENT.
        await _drain_workflow_outbox(db_session)

        # CodeReview.dispatch requires owning_agent_id — seed a real agent row (FK constraint).
        agent_row = await _seed_agent_for_tests(org_id=org_id)
        sim_workspace_id = str(
            await _seed_workspace_for_tests(
                org_id=org_id,
                provider_id="in_process",
                sha="deadbeefcafef00d",
                agent_id=agent_row["id"],
            )
        )
        await db_session.commit()
        await _advance_pending_agent_event(db_session, wfx_id, outputs={"workspace_id": sim_workspace_id})

        # CodeReview parks. Simulate zero-findings response — handle_response validates
        # the JSON against CodeReviewResponse; empty findings list → PostFindings no-op.
        await _advance_pending_agent_event(
            db_session,
            wfx_id,
            outputs={"output": '{"findings": []}'},
        )

        # PostFindings (Local) ran inline with empty findings list → success-no-op;
        # CleanupWorkspace then parked. Simulate its terminal event.
        await _advance_pending_agent_event(db_session, wfx_id, outputs={})

    wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
    assert wfx.state == WorkflowState.DONE.value
