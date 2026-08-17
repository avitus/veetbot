"""PostgreSQL concurrency and replay coverage for Milestone 10A authoring."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.bootstrap import build
from agent_core.domain.errors import ConflictError, SkillRevisionConflict
from agent_core.domain.skills import (
    AuthoringContext,
    SkillPackage,
    SkillPackageMember,
    SkillRef,
    SkillRevision,
    SkillSource,
)
from tests.integration.m2_support import database_settings


def _package(body: str, *, version: str) -> SkillPackage:
    return SkillPackage(
        directory_name="authoring-m10a",
        members=(
            SkillPackageMember(
                path="SKILL.md",
                data=(
                    "---\nname: authoring-m10a\n"
                    f"version: {version}\n"
                    "description: Durable authoring fixture.\n"
                    "required_tools: []\n---\n"
                    f"{body}"
                ).encode(),
            ),
        ),
    )


async def test_postgres_skill_authoring_replay_and_conflict(tmp_path: Path) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path)
    async with build(settings=settings, storage="postgres") as composition:
        session_id = await composition.sessions.create()
        authoring_run_id = await composition.runs.submit("authoring provenance", session_id)
        replay_context = AuthoringContext(
            run_id=authoring_run_id,
            principal_id=composition.principal.principal_id,
            invocation_id=UUID("00000000-0000-0000-0000-000000010001"),
            idempotency_key="same-canonical-authoring-request",
        )

        async def install(
            package: SkillPackage,
            expected_revision: int,
            context: AuthoringContext,
        ) -> SkillRevision:
            async with composition.uow_factory() as uow:
                return await uow.skills.install(
                    composition.principal.tenant_id,
                    package,
                    SkillSource.AGENT,
                    expected_revision,
                    context,
                )

        replayed = await asyncio.gather(
            install(_package("Initial procedure.", version="1.0.0"), 0, replay_context),
            install(_package("Initial procedure.", version="1.0.0"), 0, replay_context),
        )
        assert {(item.skill_id, item.revision) for item in replayed} == {(replayed[0].skill_id, 1)}
        with pytest.raises(ConflictError, match="different arguments"):
            await install(
                _package("Different request.", version="1.0.1"),
                1,
                replay_context.model_copy(update={"idempotency_key": "different-request"}),
            )

        async def competing_patch(body: str, invocation: int) -> SkillRevision:
            return await install(
                _package(body, version="1.0.1"),
                1,
                AuthoringContext(
                    run_id=authoring_run_id,
                    principal_id=composition.principal.principal_id,
                    invocation_id=UUID(int=invocation),
                    idempotency_key=f"competing-{invocation}",
                ),
            )

        competitors = await asyncio.gather(
            competing_patch("Winner A.", 10_002),
            competing_patch("Winner B.", 10_003),
            return_exceptions=True,
        )
        winners = [item for item in competitors if isinstance(item, SkillRevision)]
        losers = [item for item in competitors if isinstance(item, SkillRevisionConflict)]
        assert len(winners) == 1
        assert winners[0].revision == 2
        assert len(losers) == 1
        assert losers[0].current_revision == 2

        archive_context = AuthoringContext(
            run_id=authoring_run_id,
            principal_id=composition.principal.principal_id,
            invocation_id=UUID(int=10_004),
            idempotency_key="archive-replay",
        )

        async with composition.uow_factory() as uow:
            persisted = await uow.skills.resolve(
                composition.principal.tenant_id, SkillRef.parse("authoring-m10a")
            )
            linked_run = await uow.runs.get(authoring_run_id, composition.principal)
            archived = await uow.skills.archive(
                composition.principal.tenant_id,
                "authoring-m10a",
                persisted.revision,
                archive_context,
            )
        assert persisted.body in {"Winner A.", "Winner B."}
        assert persisted.authored_by_run_id == linked_run.id
        assert persisted.authored_by_principal_id == composition.principal.principal_id
        assert persisted.authored_by_invocation_id is not None
        assert persisted.authoring_idempotency_key is not None
        async with composition.uow_factory() as uow:
            replayed_archive = await uow.skills.archive(
                composition.principal.tenant_id,
                "authoring-m10a",
                persisted.revision,
                archive_context,
            )
            fallback = await uow.skills.resolve(
                composition.principal.tenant_id, SkillRef.parse("authoring-m10a")
            )
        assert archived.archived_by_invocation_id == archive_context.invocation_id
        assert replayed_archive.archived_by_invocation_id == archive_context.invocation_id
        assert fallback.revision == 1
