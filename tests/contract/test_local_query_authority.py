"""Local queries still require the real owner, reader and live paired surface."""

from typing import Any
from uuid import UUID

import pytest

from agent_core.domain.events import NewEvent, conversation_items
from agent_core.domain.memory import Sensitivity
from agent_core.domain.runs import RunCheckpoint
from agent_core.domain.surfaces import Pairing, SurfaceSession
from agent_core.memory.local_query_authority import LocalQueryAuthority
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal
from tests.contract.test_runtime_semantic_queries_contract import runtime_stack


@pytest.mark.parametrize("change", ["delegated", "review", "parent"])
async def test_bounded_rollout_authority_withholds_noninteractive_or_child_work(
    change: str,
) -> None:
    from agent_core.domain.runs import RunKind

    factory, _, live, session, checkpoint = await setup()
    authority = LocalQueryAuthority(
        factory,
        principal(),
        policy_version="policy@1",
        retrieval_policy="retrieval@1",
        reader_sha256="a" * 64,
        interactive_only=True,
    )
    assert await authority(live, session, checkpoint) is not None
    updates = (
        {"parent_run_id": UUID(int=999)}
        if change == "parent"
        else {"kind": RunKind.DELEGATED if change == "delegated" else RunKind.SKILL_REVIEW}
    )
    assert await authority(live.model_copy(update=updates), session, checkpoint) is None


async def setup(origin: dict[str, str] | None = None) -> tuple[Any, ...]:
    _, factory, _, _, _, _ = await runtime_stack()
    async with factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=RUN_ID,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=principal().principal_id,
                payload={
                    "content": "Suggest breakfast options",
                    **({"origin": origin} if origin else {}),
                },
            )
        )
        await uow.runs.set_seed_event_sequence(RUN_ID, event.sequence)
        live = await uow.runs.get(RUN_ID, principal())
        session = await uow.sessions.get(SESSION_ID, principal())
        checkpoint = RunCheckpoint(
            run_id=RUN_ID,
            version=2,
            status=live.status,
            created_at=NOW,
            conversation=conversation_items(event),
            last_event_sequence=event.sequence,
            budget_state={"context_reader_sha256": "a" * 64},
        )
        await uow.checkpoints.write(RUN_ID, checkpoint, full=True)
    authority = LocalQueryAuthority(
        factory,
        principal(),
        policy_version="policy@1",
        retrieval_policy="retrieval@1",
        reader_sha256="a" * 64,
    )
    return factory, authority, live, session, checkpoint


async def test_offline_query_does_not_downgrade_unknown_task_privacy() -> None:
    _, authority, live, session, checkpoint = await setup()
    value = await authority(live, session, checkpoint)
    assert value is not None, "Authenticated offline work needs current run authority"
    assert value.sensitivity_floor is Sensitivity.RESTRICTED
    assert value.surface_id == "private"


@pytest.mark.parametrize("marker", [None, "b" * 64])
async def test_actual_reader_must_match_the_reviewed_reader(marker: str | None) -> None:
    _, authority, live, session, checkpoint = await setup()
    checkpoint.budget_state["context_reader_sha256"] = marker
    assert await authority(live, session, checkpoint) is None


async def test_foreign_owner_and_unbound_surface_are_withheld() -> None:
    _, authority, live, session, checkpoint = await setup()
    assert (
        await authority(live, session.model_copy(update={"principal_id": "foreign"}), checkpoint)
        is None
    )
    _, authority, live, session, checkpoint = await setup(
        {"kind": "telegram", "surface_id": str(UUID(int=901))}
    )
    assert (
        await authority(
            live, session.model_copy(update={"metadata": {"surface": "private"}}), checkpoint
        )
        is None
    )


async def test_private_paired_surface_is_rechecked_and_bound_to_current_scopes() -> None:
    surface_id = UUID(int=901)
    factory, authority, live, session, checkpoint = await setup(
        {"kind": "telegram", "surface_id": str(surface_id)}
    )
    mapping = SurfaceSession(
        id=UUID(int=902),
        surface_id=surface_id,
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        external_key="dm:sender",
        session_id=SESSION_ID,
        created_at=NOW,
        last_inbound_at=NOW,
    )
    pairing = Pairing(
        id=UUID(int=903),
        surface_id=surface_id,
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        sender_id="sender",
        granted_scopes=live.principal_scopes,
        paired_at=NOW,
    )
    async with factory() as uow:
        await uow.surfaces.sessions.create(mapping)
        await uow.surfaces.pairings.create_pairing(pairing)
    value = await authority(live, session, checkpoint)
    assert value is not None, "Live owner DM pairings must be eligible for local recall"
    async with factory() as uow:
        await uow.surfaces.pairings.revoke_pairing(pairing.id, principal(), NOW)
    assert await authority(live, session, checkpoint) is None
