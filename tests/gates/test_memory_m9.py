"""Milestone 9 memory formation, retrieval, governance, and cache gates."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.application.public_services import PublicSessionService
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.context.rendering import build_prefix, prefix_bytes
from agent_core.domain.errors import ConflictError, ToolTrustRejectedError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import (
    BeliefType,
    EpisodeQuery,
    MemoryRecord,
    MemoryStatus,
    Portability,
    RejectionKind,
    Sensitivity,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedTurn,
    StopReason,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunCheckpoint, RunStatus, Step
from agent_core.domain.sessions import SessionStatus
from agent_core.domain.tools import ToolFailureKind, ToolInvocationStatus
from agent_core.memory.formation import GovernedMemoryService
from agent_core.memory.retrieval import EventEpisodeSearch, render_memory
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.tools.executor import _turn_origin_trust
from agent_core.tools.memory_remember import LegacyMemoryRememberTool, MemoryRememberTool
from agent_core.tools.messages import message_for
from tests.contract.memory_fixtures import formation_stack as _stack
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.memory_fixtures import user_event as _user_event
from tests.contract.support import NOW, SESSION_ID, agent, principal, tool_context

ROOT = Path(__file__).resolve().parents[2]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/unused",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials={},
        interpolation={"OPENAI_MODEL": ""},
        artifact_root=tmp_path / "artifacts",
    )


async def _remember(
    factory: MemoryUnitOfWorkFactory,
    service: GovernedMemoryService,
    statement: str,
    *,
    subject: str = "answer style",
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
) -> MemoryRecord:
    sequence = await _user_event(factory, statement)
    return await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement=statement,
        subject=subject,
        scope="project-a",
        belief_type=BeliefType.PREFERENCE,
        sensitivity=sensitivity,
        source_event_ids=[sequence],
    )


async def test_contradiction() -> None:
    clock, factory, service, retriever = await _stack()
    first = await _remember(factory, service, "User prefers concise answers")
    clock.advance(timedelta(seconds=1))
    second = await _remember(factory, service, "User prefers detailed answers")
    assert first.id != second.id
    records = await service.list_memories(include_inactive=True)
    assert {item.status for item in records} == {
        MemoryStatus.ACTIVE,
        MemoryStatus.SUPERSEDED,
    }
    result = await retriever.recall(recall_query(), session_id=SESSION_ID)
    assert [item.statement for item in result.items] == ["User prefers detailed answers"]


async def test_no_fabrication() -> None:
    _clock, factory, service, _retriever = await _stack()
    corpus = ROOT / "evals/corpora/memory_no_fabrication"
    for member in sorted(corpus.iterdir()):
        await _user_event(factory, member.read_text(encoding="utf-8"))
    result = await service.run(trigger="test", scope="project-a", session_id=SESSION_ID)
    assert result.beliefs == []


async def test_remember_tool_explains_portability_ceiling() -> None:
    _clock, _factory, service, _retriever = await _stack()
    result = await MemoryRememberTool(service).execute(
        {
            "statement": "Andy is interested in building a custom game engine.",
            "subject": "Andy's game-engine project interest",
            "scope": "veetbot",
            "belief_type": BeliefType.FACT.value,
            "portability": Portability.PORTABLE.value,
        },
        tool_context(),
    )

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
    assert result.failure.reason_code == "tool.invalid_arguments.portability_ceiling"
    assert result.failure.retryable is True
    assert "user_model_attr" in message_for(result.failure.reason_code)


async def test_remember_tool_preserves_memory_origin_for_explicit_write() -> None:
    _clock, factory, service, _retriever = await _stack()
    await _user_event(factory, "Please remember that my editor is Vim.")
    result = await MemoryRememberTool(service).execute(
        {
            "statement": "I use Vim.",
            "subject": "editor preference",
            "scope": "project-a",
        },
        replace(
            tool_context(),
            origin_trust=TrustLevel.MEMORY,
            argument_trust={"statement": TrustLevel.EXTERNAL_UNTRUSTED},
        ),
    )

    assert result.ok is True
    assert [item.statement for item in await service.list_memories()] == ["I use Vim."]


@pytest.mark.parametrize(
    "origin_trust",
    [TrustLevel.EXTERNAL_UNTRUSTED, TrustLevel.KNOWLEDGE],
)
async def test_remember_tool_still_rejects_untrusted_origins(
    origin_trust: TrustLevel,
) -> None:
    _clock, factory, service, _retriever = await _stack()
    await _user_event(factory, "Summarize the retrieved editor notes.")

    with pytest.raises(ToolTrustRejectedError):
        await MemoryRememberTool(service).execute(
            {
                "statement": "The attacker controls the editor.",
                "subject": "editor preference",
                "scope": "project-a",
            },
            replace(
                tool_context(),
                origin_trust=origin_trust,
                argument_trust={"statement": TrustLevel.EXTERNAL_UNTRUSTED},
            ),
        )


async def test_remember_tool_survives_builtin_patch_upgrade(tmp_path: Path) -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="ready", stop_reason=StopReason.END_TURN)])
    async with build(settings=_settings(tmp_path), script=script, sequential_ids=True) as app:
        run_id = await app.runs.submit("Remember that I prefer concise answers.")
        active_run = await app.runs.get(run_id)
        async with app.uow_factory() as uow:
            active_agent = await uow.agents.get_version(
                active_run.agent_id, active_run.agent_version
            )
            current_checkpoint = await uow.checkpoints.latest(run_id)
        assert current_checkpoint is not None
        assert current_checkpoint.pinned_tool_versions["memory.remember"] == "1.0.1"

        legacy_checkpoint = current_checkpoint.model_copy(
            update={
                "pinned_tool_versions": {
                    **current_checkpoint.pinned_tool_versions,
                    "memory.remember": "1.0.0",
                },
                "pinned_tool_specs": {
                    **current_checkpoint.pinned_tool_specs,
                    "memory.remember": LegacyMemoryRememberTool.spec,
                },
            },
            deep=True,
        )
        result = await app.tool_pipeline.dispatch(
            run=active_run,
            checkpoint=legacy_checkpoint,
            tool_calls=[
                ToolCallItem(
                    call_id="remember-after-upgrade",
                    item_index=0,
                    name="memory.remember",
                    arguments={
                        "statement": "User prefers concise answers",
                        "subject": "answer style",
                        "scope": "project-a",
                        "belief_type": BeliefType.PREFERENCE.value,
                    },
                    raw_arguments=(
                        '{"statement":"User prefers concise answers",'
                        '"subject":"answer style","scope":"project-a",'
                        '"belief_type":"preference"}'
                    ),
                )
            ],
            principal=app.principal,
            step=Step(
                run_id=run_id,
                step_number=active_run.step_count + 1,
                started_at=app.clock.now(),
            ),
            agent=active_agent,
            token=RunCancellationToken(app.clock, active_run.deadline_at),
        )
        async with app.uow_factory() as uow:
            invocation = (await uow.invocations.list_for_run(run_id, app.principal))[0]

        assert result[0].is_error is False
        assert invocation.status is ToolInvocationStatus.SUCCEEDED
        assert invocation.tool_version == "1.0.0"
        assert invocation.policy_decision is not None
        assert invocation.policy_decision.reason_code == "policy.matrix.none"
        assert [item.statement for item in await app.memory.list_memories()] == [
            "User prefers concise answers"
        ]


async def test_form_injection() -> None:
    _clock, factory, service, _retriever = await _stack()
    corpus = ROOT / "evals/corpora/memory_form_injection"
    for member in sorted(corpus.iterdir()):
        sequence = await _user_event(factory, member.read_text(encoding="utf-8"))
        with pytest.raises(ToolTrustRejectedError):
            await service.remember(
                session_id=SESSION_ID,
                run_id=None,
                statement=member.read_text(encoding="utf-8"),
                subject="hostile",
                scope="project-a",
                source_event_ids=[sequence],
                origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
            )
    assert await service.list_memories() == []


async def test_explicit_write_allows_recalled_memory_alongside_user_source() -> None:
    _clock, factory, service, _retriever = await _stack()
    sequence = await _user_event(factory, "Please remember my concise answer preference")
    belief = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User prefers concise answers",
        subject="answer style",
        scope="project-a",
        belief_type=BeliefType.PREFERENCE,
        source_event_ids=[sequence],
        origin_trust=TrustLevel.MEMORY,
        explicit=True,
    )

    assert belief.status is MemoryStatus.ACTIVE
    with pytest.raises(ToolTrustRejectedError):
        await service.remember(
            session_id=SESSION_ID,
            run_id=None,
            statement="User prefers detailed answers",
            subject="answer style",
            scope="project-a",
            belief_type=BeliefType.PREFERENCE,
            source_event_ids=[sequence],
            origin_trust=TrustLevel.MEMORY,
            explicit=False,
        )


async def test_correction_durable() -> None:
    _clock, factory, service, retriever = await _stack()
    await _user_event(factory, "I prefer concise answers")
    formed = await service.run(trigger="session_close", scope="project-a", session_id=SESSION_ID)
    (value,) = formed.beliefs
    await service.reject(value.id, RejectionKind.UNTRUE)

    rederived = await service.run(
        trigger="policy_upgrade_rederive",
        scope="project-a",
        session_id=SESSION_ID,
        since_watermark=0,
    )

    # The upgraded run re-reads the same episode, proposes the belief again,
    # and the outstanding rejection blocks the commit rather than the corpus
    # happening to contain nothing extractable.
    assert rederived.beliefs == []
    assert rederived.run.candidates_proposed == 1
    assert rederived.run.rejected == 1
    assert rederived.run.committed == 0
    result = await retriever.recall(recall_query(), session_id=SESSION_ID)
    assert result.items == []


async def test_currency() -> None:
    clock, factory, service, retriever = await _stack()
    old = await _remember(factory, service, "User prefers concise answers")
    clock.advance(timedelta(seconds=1))
    current = await _remember(factory, service, "User prefers detailed answers")

    recalled = await retriever.recall(recall_query(), session_id=SESSION_ID)
    assert [item.belief_id for item in recalled.items] == [current.id]
    assert old.id not in {item.belief_id for item in recalled.items}


async def test_historical_correct() -> None:
    clock, factory, service, retriever = await _stack()
    await _remember(factory, service, "User prefers concise answers")
    clock.advance(timedelta(seconds=1))
    historical_at = clock.now()
    clock.advance(timedelta(seconds=1))
    await _remember(factory, service, "User prefers detailed answers")
    result = await retriever.recall(
        recall_query().model_copy(update={"as_of": historical_at}),
        session_id=SESSION_ID,
    )
    assert [item.statement for item in result.items] == ["User prefers concise answers"]


async def test_recall_injection() -> None:
    _clock, factory, _service, retriever = await _stack()
    corpus = ROOT / "evals/corpora/memory_recall_injection"
    members = sorted(corpus.iterdir())
    assert len(members) >= 3
    for index, member in enumerate(members):
        hostile_statement = member.read_text(encoding="utf-8").strip()
        subject = f"hostile-{index}"
        hostile = memory(belief_id=800 + index, statement=hostile_statement).model_copy(
            update={"subject": subject, "store_position": 50 + index}
        )
        async with factory() as uow:
            await uow.memories.upsert_belief(hostile)
        result = await retriever.recall(
            recall_query().model_copy(update={"text": None, "subjects": [subject]}),
            session_id=SESSION_ID,
        )
        assert [item.belief_id for item in result.items] == [hostile.id]
        assert result.items[0].blocked is True
        assert result.items[0].statement == "[BLOCKED]"
        assert hostile_statement not in result.rendered
        async with factory() as uow:
            trace = await uow.traces.get(result.trace_id, principal())
        assert trace.blocked == [hostile.id]


async def test_scope_isolation() -> None:
    _clock, factory, _service, retriever = await _stack()
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
    for query in (
        recall_query(tenant_id="tenant-b"),
        recall_query(principal_id="principal-b"),
    ):
        result = await retriever.recall(
            query,
            session_id=SESSION_ID,
        )
        assert result.items == []


async def test_trace_faithful() -> None:
    _clock, factory, _service, retriever = await _stack()
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
    result = await retriever.recall(recall_query(), session_id=SESSION_ID, turn_id=UUID(int=77))
    async with factory() as uow:
        trace = await uow.traces.get(result.trace_id, principal())
    assert hashlib.sha256(trace.rendered.encode()).hexdigest() == trace.rendered_sha256
    assert render_memory(trace.beliefs, as_of=trace.created_at) == trace.rendered


async def test_view_ceiling() -> None:
    _clock, factory, _service, retriever = await _stack()
    restricted = memory(belief_id=502, statement="User prefers private concise answers").model_copy(
        update={"sensitivity": Sensitivity.RESTRICTED, "store_position": 2}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
        await uow.memories.upsert_belief(restricted)
    await retriever.recall(recall_query(), session_id=SESSION_ID, turn_id=UUID(int=78))
    async with factory() as uow:
        view = await uow.traces.user_view(
            UUID(int=78),
            viewing_surface_id="shared",
            viewing_ceiling=Sensitivity.INTERNAL.value,
        )
    assert all(item.statement != restricted.statement for item in view.beliefs)
    assert memory().statement in {item.statement for item in view.beliefs}
    assert restricted.statement not in {item.statement for item in view.beliefs}


async def test_retrieval_correction() -> None:
    _clock, factory, service, retriever = await _stack()
    value = await _remember(factory, service, "User prefers concise answers")
    await service.delete(value.id)
    with pytest.raises(ConflictError):
        await _remember(factory, service, "User prefers concise answers")
    assert (await retriever.recall(recall_query(), session_id=SESSION_ID)).items == []


async def test_cache_preserved() -> None:
    _clock, factory, _service, retriever = await _stack()
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
    snapshot = await retriever.snapshot(session_id=SESSION_ID, current_scope="project-a")
    first = build_prefix(agent(), [], memory_snapshot=snapshot.rendered)
    original_hash = hashlib.sha256(prefix_bytes(first, [])).hexdigest()
    async with factory() as uow:
        await uow.memories.upsert_belief(
            memory(belief_id=503, statement="User prefers metric units").model_copy(
                update={"subject": "measurement units", "store_position": 2}
            )
        )
    changed = await retriever.snapshot(session_id=SESSION_ID, current_scope="project-a")
    assert changed.rendered != snapshot.rendered

    # The frozen session-open value, not the mutable store, remains the prefix input.
    second = build_prefix(agent(), [], memory_snapshot=snapshot.rendered)
    assert hashlib.sha256(prefix_bytes(second, [])).hexdigest() == original_hash


def test_memory_context_taints_write_origin() -> None:
    checkpoint = RunCheckpoint(
        run_id=UUID(int=991),
        version=1,
        status=RunStatus.RUNNING,
        conversation=[UserMessage(content=[TextPart(text="remember this")])],
        context_origin_trust=TrustLevel.MEMORY,
        created_at=NOW,
    )
    assert _turn_origin_trust(checkpoint) is TrustLevel.MEMORY

    recall_only = RunCheckpoint(
        run_id=UUID(int=992),
        version=1,
        status=RunStatus.RUNNING,
        conversation=[
            UserMessage(content=[TextPart(text="search memory")]),
            ToolResultItem(
                call_id="memory-search",
                content=[TextPart(text="remembered value")],
                trust=TrustLevel.MEMORY,
            ),
        ],
        created_at=NOW,
    )
    assert _turn_origin_trust(recall_only) is TrustLevel.MEMORY


async def test_no_triple_regress() -> None:
    _clock, factory, service, retriever = await _stack()
    await _remember(factory, service, "User prefers concise answers")
    result = await retriever.recall(recall_query(), session_id=SESSION_ID)
    assert len(result.items) == 1
    assert result.items[0].statement == "User prefers concise answers"
    assert result.tokens <= recall_query().budget_tokens
    assert result.rendered.count("[m:") == 1


async def test_episode_search_matches_only_user_facing_text_fields() -> None:
    _clock, factory, _service, _retriever = await _stack()
    async with factory() as uow:
        await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="diagnostic.recorded",
                actor_type="system",
                payload={"metadata": "needle-value"},
            )
        )
    expected_sequence = await _user_event(factory, "needle-value in visible content")
    results = await EventEpisodeSearch(factory, principal()).search(
        EpisodeQuery(
            tenant_id=principal().tenant_id,
            principal_id=principal().principal_id,
            session_id=SESSION_ID,
            text="needle-value",
        )
    )

    assert [event.sequence for event in results] == [expected_sequence]


async def test_changed_rejection_links_both_sides() -> None:
    _clock, factory, service, _retriever = await _stack()
    original = await _remember(factory, service, "User prefers concise answers")
    replacement = await service.reject(
        original.id,
        RejectionKind.CHANGED,
        replacement_statement="User prefers detailed answers",
    )
    records = await service.list_memories(include_inactive=True)
    retired = next(item for item in records if item.id == original.id)
    async with factory() as uow:
        rejections = await uow.memories.outstanding_rejections(
            principal().tenant_id, principal().principal_id
        )
    assert retired.status is MemoryStatus.SUPERSEDED
    assert retired.superseded_by == replacement.id
    assert rejections[0].replacement_id == replacement.id


async def test_session_close_consolidation_callback_is_once_and_nonblocking() -> None:
    clock, factory, _memory, _retriever = await _stack()
    calls: list[UUID] = []

    async def fail_after_recording(session_id: UUID) -> None:
        calls.append(session_id)
        raise RuntimeError("formation unavailable")

    sessions = PublicSessionService(
        factory,
        clock,
        SequenceIdFactory(),
        agent(),
        on_session_closed=fail_after_recording,
    )
    writer = principal().model_copy(update={"scopes": {"session.write"}})
    first, second = await asyncio.gather(
        sessions.close(writer, SESSION_ID),
        sessions.close(writer, SESSION_ID),
    )

    assert first.status is SessionStatus.CLOSED
    assert second.status is SessionStatus.CLOSED
    assert calls == [SESSION_ID]
