"""One immutable proposal and one approval install a complete widget (ADR-0270)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.adapters.cockpit.git import FakeWidgetRepository
from agent_core.application.cockpit import CommitOutboxWorker
from agent_core.bootstrap import build
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
from agent_core.domain.policies import SideEffectClass
from agent_core.domain.runs import RunStatus
from agent_core.tools.cockpit_installation import CockpitInstallTool
from agent_core.tools.registry import RegisteredTool
from tests.contract.support import tool_context
from tests.gates.cockpit_publication_support import ScriptedBuilder, package
from tests.integration.m2_support import memory_settings
from tests.unit.test_job_execution import Runner
from tests.unit.test_job_publication import Screen


async def test_widget_workflow_offers_separate_preparation_and_final_installation() -> None:
    async with build(
        settings=replace(
            memory_settings(),
            jobs_enabled=True,
            cockpit_enabled=True,
            schedule_api_enabled=True,
            schedule_worker_enabled=True,
        )
    ) as composition:
        registry = composition.tool_pipeline._registry
        prepare = registry.get("cockpit.prepare")
        install = registry.get("cockpit.install")
        assert prepare.spec.side_effect is SideEffectClass.NONE
        assert install.spec.side_effect is SideEffectClass.PUBLICATION


def proposal(*, job: bool = True) -> dict[str, Any]:
    files = package()
    files["widget.json"] = '{"schema":1,"slug":"weather","feeds":[{"id":"agent.counts"}]}'
    body: dict[str, Any] = {"slug": "weather", "files": files}
    if job:
        body["job"] = {
            "slug": "count-items",
            "source": "def main(inputs, job): return len(inputs['items'])",
            "manifest": {
                "title": "Count items",
                "calls": ["feed.publish"],
                "feeds": ["agent.counts"],
            },
            "access": {
                "inputs": {"items": ["private-repo"]},
                "hosts": {"api.github.com": "github-reader"},
                "feeds": ["agent.counts"],
            },
            "every_minutes": 5,
        }
    return body


@asynccontextmanager
async def prepared(*, job: bool = True, change: Any = None) -> AsyncIterator[Any]:
    arguments = proposal(job=job)
    if change:
        change(arguments)
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="job.manage",
                        arguments={"operation": "read"},
                        call_id="read-jobs",
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="job.manage",
                        call_id="trial-job",
                        arguments={
                            "operation": "trial",
                            "source": "def main(inputs, job): return len(inputs['items'])",
                            "manifest": {"title": "Test counting"},
                            "inputs": {"items": [1, 2]},
                        },
                    )
                ]
            ),
            ScriptedTurn(
                tool_calls=[ScriptedToolCall(name="cockpit.prepare", arguments=arguments)]
            ),
            ScriptedTurn(text="Preparation finished."),
        ]
    )
    builder = ScriptedBuilder()
    screen = Screen()
    async with build(
        settings=replace(
            memory_settings(),
            jobs_enabled=True,
            cockpit_enabled=True,
            schedule_api_enabled=True,
            schedule_worker_enabled=True,
        ),
        script=script,
        enabled_tools=["job.manage", "cockpit.prepare", "cockpit.install"],
        job_runner_override=Runner(),
        cockpit_builder_override=builder,
        cockpit_local_branch=True,
        job_screen_override=screen,
    ) as composition:
        installer = cast(
            CockpitInstallTool,
            cast(
                RegisteredTool, composition.tool_pipeline._registry.get("cockpit.install")
            ).implementation,
        )
        installer.cockpit = replace(installer.cockpit, commit_wait_seconds=0)
        run_id = await composition.runs.submit("Prepare a widget with one approval at the end.")
        assert (await composition.runs.get(run_id)).status is RunStatus.COMPLETED
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
            [invocation] = [item for item in invocations if item.tool_name == "cockpit.prepare"]
        yield composition, script, invocation, builder, screen


def add_install(script: FakeModelScript, arguments: dict[str, Any]) -> None:
    script.turns.extend(
        [
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="cockpit.install",
                        arguments=arguments,
                        call_id=f"install-{len(script.turns)}",
                    )
                ]
            ),
            ScriptedTurn(text="Installation status reported."),
        ]
    )


def reference(invocation: Any) -> dict[str, str]:
    return {"preparation_run_id": str(invocation.run_id), "preparation_id": str(invocation.id)}


async def test_preparation_is_private_and_screens_new_private_inputs() -> None:
    async with prepared() as (composition, _, invocation, builder, screen):
        assert invocation.structured_result["ready"] is True
        assert "private-repo" in screen.seen[-1].settings_values
        assert "private-repo" in builder.screens[-1].settings_values
        assert await composition.approvals.list_pending() == []
        async with composition.uow_factory() as uow:
            assert await uow.jobs.list(composition.principal) == []
            assert (
                await uow.cockpit_widgets.list_widgets(composition.principal, include_deleted=True)
                == []
            )
        assert (await composition.schedules.list(composition.principal, 10, None)).items == []


@pytest.mark.parametrize("with_job", [True, False])
async def test_one_approval_installs_and_resumes_without_duplicate_components(
    with_job: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with prepared(job=with_job) as (composition, script, invocation, _, _):
        arguments = reference(invocation)
        add_install(script, arguments)
        run_id = await composition.runs.submit(
            "Install the prepared widget.", session_id=invocation.session_id
        )
        assert (await composition.runs.get(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        assert approval.tool_name == "cockpit.install"
        assert "Install widget weather" in approval.action_summary
        assert approval.arguments["widget"] == "weather"
        if with_job:
            assert approval.arguments["private_access"]["hosts"] == {
                "api.github.com": "github-reader"
            }
            assert "5 minutes" in approval.arguments["refresh"]
            assert "private-repo" in approval.action_summary
            assert "model budget USD 0" in approval.action_summary
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        finished = await composition.runs.get(run_id)
        assert finished.status is RunStatus.COMPLETED, (
            finished.failure,
            [r.__dict__.get("error_frames") for r in caplog.records],
        )
        # Production's normal outbox worker records the widget commit between calls.
        await CommitOutboxWorker(
            uow_factory=composition.uow_factory,
            clock=composition.clock,
            ids=composition.ids,
            principal=composition.principal,
            repository=FakeWidgetRepository(),
        ).run_once()
        for _ in range(2):
            add_install(script, arguments)
            replay_run = await composition.runs.submit(
                "Resume this same approved installation.", session_id=invocation.session_id
            )
            assert (await composition.runs.get(replay_run)).status is RunStatus.COMPLETED
            assert await composition.approvals.list_pending(run_id=replay_run) == []
            async with composition.uow_factory() as uow:
                [replay] = await uow.invocations.list_for_run(replay_run, composition.principal)
            assert replay.structured_result["complete"] is True, replay.result_item
        async with composition.uow_factory() as uow:
            widgets = await uow.cockpit_widgets.list_widgets(
                composition.principal, include_deleted=True
            )
            assert len(widgets) == 1
            assert (
                len(
                    await uow.cockpit_widgets.list_revisions(
                        composition.principal, "weather", limit=10
                    )
                )
                == 1
            )
            if with_job:
                [job] = await uow.jobs.list(composition.principal)
                assert len(job.revisions) == len(job.grants) == 1
                assert next(iter(job.grants.values())).approval_id == approval.id
        schedules = (await composition.schedules.list(composition.principal, 10, None)).items
        assert len(schedules) == int(with_job)
        if with_job:
            assert schedules[0].revision.limits.max_model_calls == 0


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["files"].update({"widget.tsx": "REJECT"}),
        lambda p: p["job"].update({"source": "def main(:"}),
        lambda p: p["job"]["access"].update({"maximum_response_bytes": 2097152}),
        lambda p: p["job"]["access"].update({"feeds": ["agent.undeclared"]}),
        lambda p: p.update({"expected_revision": str(UUID(int=991))}),
        lambda p: p["files"].update({"widget.json": '{"feeds": [null]}'}),
    ],
)
async def test_invalid_preparation_returns_repair_feedback_without_approval(change: Any) -> None:
    async with prepared(change=change) as (composition, _, invocation, _, _):
        assert invocation.outcome.status.value == "failed"
        assert invocation.result_item.trust.value == "external_untrusted"
        assert len(invocation.result_item.content) == 2
        assert await composition.approvals.list_pending() == []
        async with composition.uow_factory() as uow:
            assert await uow.jobs.list(composition.principal) == []


async def test_unapproved_and_cross_session_installation_have_no_effect() -> None:
    async with prepared() as (composition, _, invocation, _, _):
        install = composition.tool_pipeline._registry.get("cockpit.install")
        context = replace(
            tool_context(),
            principal=composition.principal,
            tenant_id=composition.principal.tenant_id,
            session_id=invocation.session_id,
            run_id=invocation.run_id,
        )
        refused = await install.execute(reference(invocation), context)
        assert not refused.ok
        assert refused.failure.reason_code == "cockpit.install_approval_required"
        refused = await install.execute(
            reference(invocation), replace(context, session_id=UUID(int=999))
        )
        assert not refused.ok
        async with composition.uow_factory() as uow:
            assert await uow.jobs.list(composition.principal) == []


async def test_declining_installation_leaves_every_component_unpublished() -> None:
    async with prepared() as (composition, script, invocation, _, _):
        add_install(script, reference(invocation))
        run_id = await composition.runs.submit("Install it.", session_id=invocation.session_id)
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.DENY)
        async with composition.uow_factory() as uow:
            assert await uow.jobs.list(composition.principal) == []
            assert (
                await uow.cockpit_widgets.list_widgets(composition.principal, include_deleted=True)
                == []
            )
        assert (await composition.schedules.list(composition.principal, 10, None)).items == []


async def test_approval_cannot_be_reused_for_another_preparation_or_owner() -> None:
    from agent_core.application.cockpit_installation import (
        installation_approval,
        prepared_installation,
    )
    from agent_core.domain.errors import NotFoundError

    async with prepared() as (composition, script, invocation, _, _):
        arguments = reference(invocation)
        add_install(script, arguments)
        run_id = await composition.runs.submit("Install it.", session_id=invocation.session_id)
        [approval] = await composition.approvals.list_pending(run_id=run_id)
        await composition.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        assert (
            await installation_approval(
                composition.uow_factory, composition.principal, invocation.session_id, arguments
            )
            is not None
        )
        changed = {**arguments, "preparation_id": str(UUID(int=998))}
        assert (
            await installation_approval(
                composition.uow_factory,
                composition.principal,
                invocation.session_id,
                changed,
                approval.action_id,
            )
            is None
        )
        other = composition.principal.model_copy(update={"principal_id": "other-owner"})
        with pytest.raises(NotFoundError):
            await prepared_installation(
                composition.uow_factory, other, invocation.session_id, arguments
            )
