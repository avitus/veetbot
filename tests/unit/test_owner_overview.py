"""Production entrypoint regression coverage for owner overviews and search delivery."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from tests.contract.owner_overview_cases import overview_run
from tests.gates.test_runtime_tool_budget import _settings


def script(family: bool) -> FakeModelScript:
    return FakeModelScript(
        turns=[
            *(
                []
                if family
                else [
                    ScriptedTurn(
                        tool_calls=[
                            ScriptedToolCall(
                                name="memory.search",
                                arguments={"text": "briefing"},
                                call_id="recall",
                            )
                        ],
                        stop_reason=StopReason.TOOL_USE,
                    )
                ]
            ),
            ScriptedTurn(text="Answer based on the delivered evidence."),
        ]
    )


@pytest.mark.parametrize("family", [False, True])
async def test_production_owner_overview(family: bool) -> None:
    async with build(
        settings=replace(_settings(), people_enabled=True), script=script(family)
    ) as app:
        await overview_run(app, family=family)


async def test_production_snapshot_uses_configured_owner_core() -> None:
    from uuid import UUID

    from agent_core.domain.memory import BeliefType, MemoryAuthority, MemoryStatus
    from tests.contract.owner_overview_cases import seed
    from tests.gates.test_runtime_tool_budget import _requests

    async with build(settings=_settings(), script=script(True)) as app:
        session_id = await app.sessions.create()
        await seed(app, session_id, family=False)
        async with app.uow_factory() as uow:
            for index in range(20):
                record = await uow.memories.get(UUID(int=80000 + index), app.principal)
                await uow.memories.reinforce(
                    record.model_copy(
                        update={
                            "status": MemoryStatus.ACTIVE,
                            "authority": MemoryAuthority.USER,
                            "belief_type": BeliefType.RELATIONSHIP
                            if index == 0
                            else BeliefType.PROCEDURE_POINTER,
                            "statement": "User has a daughter named Mira."
                            if index == 0
                            else record.statement * 4,
                        }
                    )
                )
        run_id = await app.runs.submit("Hello", session_id=session_id)
        await app.runs.wait_terminal(run_id)
        request = _requests(app)[0].model_dump_json()
        assert "owner-core@1" in request, (
            "The production planner bypassed the configured snapshot policy"
        )
        assert "User has a daughter named Mira." in request


@pytest.mark.parametrize("restriction", ["local", "expired", "retracted", "erased", "corrected"])
async def test_production_overview_visibility(restriction: str) -> None:
    from tests.contract.owner_overview_cases import restricted_overview_run

    async with build(
        settings=replace(_settings(), people_enabled=True), script=script(True)
    ) as app:
        await restricted_overview_run(app, restriction)


def page_script() -> FakeModelScript:
    from uuid import UUID

    return FakeModelScript(
        turns=[
            ScriptedTurn(text="Hello"),
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.search",
                        arguments={
                            "text": "Continue direct family relationships",
                            "relationship_group": "family",
                            "people_after": str(UUID(int=90002)),
                        },
                        call_id="page",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Answer based on both delivered pages."),
        ]
    )


@pytest.mark.parametrize("relatives", [3, 4, 5])
async def test_production_overview_continues_in_existing_chat(relatives: int) -> None:
    from tests.contract.owner_overview_cases import paged_overview_run

    async with build(
        settings=replace(_settings(), people_enabled=True), script=page_script()
    ) as app:
        await paged_overview_run(app, relatives=relatives)


def test_owner_overview_preserves_prepared_semantic_query_authority() -> None:
    from uuid import UUID

    from agent_core.memory.semantic_queries import people_narrowing
    from tests.contract.memory_fixtures import recall_query

    parent = recall_query(text="Tell me everything you know about me.").model_copy(
        update={"exclude_ids": (UUID(int=11),)}
    )
    derived = parent.model_copy(
        update={
            "relationship_group": "family",
            "next_people_after": UUID(int=15),
            "expand_ids": (UUID(int=16),),
        }
    )
    assert people_narrowing(parent, derived)
    for change in (
        {"current_scope": "another-project"},
        {"exclude_ids": ()},
        {"text": "Different information"},
        {"people_after": UUID(int=10)},
        {"relationship_group": "work"},
    ):
        assert not people_narrowing(parent, derived.model_copy(update=change))


async def test_production_search_respects_smaller_inline_limit(tmp_path: Path) -> None:
    overlay = tmp_path / "tools"
    overlay.mkdir()
    (overlay / "limits.yaml").write_text("output:\n  inline_maximum_bytes: 1024\n")
    async with build(
        settings=replace(_settings(), config_dir=tmp_path, people_enabled=True),
        script=script(False),
    ) as app:
        await overview_run(app, family=False, byte_limit=1024)


async def test_preupgrade_chat_keeps_pinned_tools_and_gains_family_context() -> None:
    import html

    from agent_core.domain.runs import RunStatus
    from agent_core.tools.registry import StaticToolRegistry
    from tests.contract.owner_overview_cases import finish, seed
    from tests.gates.test_runtime_tool_budget import _requests

    async with build(
        settings=replace(_settings(), people_enabled=True),
        script=FakeModelScript(turns=[ScriptedTurn(text="Hello"), ScriptedTurn(text="Done")]),
    ) as app:
        registry = app.tool_pipeline._registry
        assert isinstance(registry, StaticToolRegistry)
        registry._latest["memory.search"] = "2.1.0"  # Persist a pre-upgrade catalog.
        session_id = await app.sessions.create()
        opening = await app.runs.submit("Hello", session_id=session_id)
        assert (await finish(app, opening)).status is RunStatus.COMPLETED
        registry._latest["memory.search"] = "2.2.0"
        facts = await seed(app, session_id, family=True)
        run_id = await app.runs.submit(
            "Tell me everything you know about me.", session_id=session_id
        )
        assert (await finish(app, run_id)).status is RunStatus.COMPLETED
        request = _requests(app)[-1]
        assert all(fact in html.unescape(request.model_dump_json()) for fact in facts)
        assert (
            next(tool for tool in request.tools if tool.name == "memory.search").version == "2.1.0"
        )
