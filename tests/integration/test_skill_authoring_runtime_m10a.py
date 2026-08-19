"""Durable skill-authoring journey: approval parking, restart, and provenance.

The Milestone 10A gates prove authoring behavior in memory; this file walks
the same governed create through PostgreSQL and real workers: the proposal
parks the run for approval, a restarted composition resolves it, and the
persisted revision carries complete authoring provenance from the
tool-driven write rather than from a direct repository call.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import yaml

from agent_core.bootstrap import build
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.policies import ActionKind
from agent_core.domain.runs import RunStatus
from agent_core.domain.skills import SkillRef, SkillSource
from agent_core.runtime.worker import DurableWorker
from tests.integration.m2_support import database_settings


def _skill_markdown(name: str) -> str:
    metadata = yaml.safe_dump(
        {
            "name": name,
            "version": "1.0.0",
            "description": f"Procedure for {name}.",
            "required_tools": [],
        },
        sort_keys=False,
    )
    return f"---\n{metadata}---\nApply the durable authoring procedure."


async def test_postgres_authoring_approval_parks_resumes_and_records_provenance(
    tmp_path: Path,
) -> None:
    settings = replace(
        database_settings(),
        skill_authoring_enabled=True,
        artifact_root=tmp_path / "artifacts",
    )
    proposed = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="skill.manage",
                        arguments={
                            "operation": "create",
                            "name": "durable-authoring",
                            "skill_markdown": _skill_markdown("durable-authoring"),
                        },
                        call_id="durable-authoring-create",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
    )

    async with build(
        settings=settings,
        storage="postgres",
        script=proposed,
        enabled_tools=["skill.manage"],
    ) as composition:
        run_id = await composition.runs.submit("Capture this procedure as a durable skill.")
        first_worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="authoring-worker-before-approval",
        )
        assert await first_worker.run_once()
        parked = await composition.runs.get(run_id)
        assert parked.status is RunStatus.WAITING_FOR_APPROVAL
        assert parked.lease_owner is None

    resumed = FakeModelScript(turns=[ScriptedTurn(text="The skill proposal was approved.")])
    async with build(
        settings=settings,
        storage="postgres",
        script=resumed,
        enabled_tools=["skill.manage"],
    ) as composition:
        approval = (await composition.approvals.list_pending(run_id=run_id))[0]
        assert approval.action_kind is ActionKind.SKILL_AUTHORING
        assert "canonical_diff" in approval.arguments
        assert "skill_markdown" not in approval.arguments
        assert approval.arguments["current_revision"] == 0
        assert approval.arguments["proposed_revision"] == 1
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)

        second_worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=composition.clock,
            worker_id="authoring-worker-after-approval",
        )
        assert await second_worker.run_once()
        completed = await composition.runs.get(run_id)
        async with composition.uow_factory() as uow:
            revision = await uow.skills.resolve(
                composition.principal.tenant_id, SkillRef.parse("durable-authoring")
            )

    assert completed.status is RunStatus.COMPLETED
    assert completed.final_message == "The skill proposal was approved."
    assert revision.source is SkillSource.AGENT
    assert revision.revision == 1
    assert revision.authored_by_run_id == run_id
    assert revision.authored_by_principal_id == composition.principal.principal_id
    assert revision.authored_by_invocation_id is not None
    assert revision.authoring_idempotency_key is not None
