"""Complete metadata recovery restores old-memory admission without resurrecting erasures."""

from pathlib import Path

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.application.reconsolidation import PublicReconsolidationService
from agent_core.domain.dreaming_recovery import LegacyAttributionManifest
from tests.contract.support import NOW, memory_uow_factory, principal


async def test_complete_empty_recovery_retires_cutover_and_keeps_schedule() -> None:
    _, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"memory.read", "memory.write"}})
    service = PublicReconsolidationService(factory, FixedClock(NOW))
    boundary = await service.enable_forward_dreaming(owner)
    before = await service.dreaming_status(owner)
    manifest = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "legacy_digest": boundary.legacy_digest,
        "proofs": [],
    }
    await service.recover_legacy_dreaming(
        owner, LegacyAttributionManifest.model_validate(manifest), confirm=True
    )
    after = await service.dreaming_status(owner)
    assert after.forward_only_since is None
    assert after.schedule == before.schedule
    assert after.runs == before.runs


async def test_recovery_rejects_new_unknown_heads_after_completion() -> None:
    from uuid import UUID

    import pytest

    from agent_core.adapters.persistence.people import InMemoryPeopleStore
    from agent_core.domain.errors import ConflictError
    from tests.unit.test_dreaming_forward import _unknown_erased_person

    _, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"memory.read", "memory.write"}})
    service = PublicReconsolidationService(factory, FixedClock(NOW))
    boundary = await service.enable_forward_dreaming(owner)
    manifest = LegacyAttributionManifest(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        legacy_digest=boundary.legacy_digest,
        proofs=(),
    )
    await service.recover_legacy_dreaming(owner, manifest, confirm=True)
    async with factory() as uow:
        assert isinstance(uow.people, InMemoryPeopleStore)
        await _unknown_erased_person(uow.people, UUID(int=9991))
    assert (await service.dreaming_status(owner)).blockers == ("legacy_attribution_unavailable",)
    with pytest.raises(ConflictError, match="no longer complete"):
        await service.recover_legacy_dreaming(owner, manifest, confirm=True)


def test_recovery_cli_rejects_invalid_input_before_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from agent_core.cli.main import app

    def unavailable(**kwargs: object) -> None:
        raise AssertionError("Invalid manifests must not connect")

    monkeypatch.setattr("agent_core.cli.main.build", unavailable)
    path = tmp_path / "invalid.json"
    for content in ("{}", '{"proofs": "wrong"}', "x" * 2_000_001):
        path.write_text(content)
        result = CliRunner().invoke(
            app,
            [
                "memory",
                "reconsolidations",
                "recover-legacy",
                "--owner",
                "a/b",
                "--manifest",
                str(path),
            ],
        )
        assert result.exit_code == 2
        assert "valid bounded proof JSON" in result.output


def test_recovery_cli_preview_confirm_and_owner_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from agent_core.cli.main import app
    from agent_core.domain.dreaming_recovery import LegacyRecoveryResult

    calls: list[bool] = []

    async def recover(
        owner: object, manifest: LegacyAttributionManifest, *, confirm: bool = False
    ) -> LegacyRecoveryResult:
        calls.append(confirm)
        return LegacyRecoveryResult(records=0, applied=confirm, manifest_digest=manifest.digest())

    @asynccontextmanager
    async def configured(**kwargs: object) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(
            principal=principal(),
            services=SimpleNamespace(
                reconsolidation=SimpleNamespace(recover_legacy_dreaming=recover)
            ),
        )

    monkeypatch.setattr("agent_core.cli.main.build", configured)
    manifest = LegacyAttributionManifest(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        legacy_digest="0" * 64,
        proofs=(),
    )
    path = tmp_path / "manifest.json"
    path.write_text(manifest.model_dump_json())
    base = ["memory", "reconsolidations", "recover-legacy", "--manifest", str(path), "--owner"]
    for confirm in (False, True):
        result = CliRunner().invoke(
            app,
            [
                *base,
                f"{principal().tenant_id}/{principal().principal_id}",
                *(["--confirm"] if confirm else []),
            ],
        )
        assert result.exit_code == 0, result.output
    assert calls == [False, True]
    result = CliRunner().invoke(app, [*base, "other/owner", "--confirm"])
    assert result.exit_code == 1 and "Owner must match" in result.output
    assert calls == [False, True]


async def test_unknown_live_attribution_blocks_recovered_owner() -> None:
    from uuid import UUID

    from agent_core.adapters.persistence.people import InMemoryPeopleStore
    from agent_core.domain.people import Person

    _, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"memory.read", "memory.write"}})
    service = PublicReconsolidationService(factory, FixedClock(NOW))
    boundary = await service.enable_forward_dreaming(owner)
    manifest = LegacyAttributionManifest(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        legacy_digest=boundary.legacy_digest,
        proofs=(),
    )
    await service.recover_legacy_dreaming(owner, manifest, confirm=True)
    async with factory() as uow:
        assert isinstance(uow.people, InMemoryPeopleStore)
        person = Person(
            id=UUID(int=9992),
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            display_name="Fixture",
            created_at=NOW,
            updated_at=NOW,
        )
        await uow.people.put(person, expected_revision=0)
        uow.people._attributions.pop((owner.tenant_id, owner.principal_id, person.id))
    assert (await service.dreaming_status(owner)).blockers == ("legacy_attribution_unavailable",)


async def test_recovered_originals_reach_provider_review() -> None:
    from typing import cast
    from uuid import UUID

    from agent_core.memory.reconsolidation_execution import ReconsolidationBatchExecutor
    from agent_core.ports.persistence import UnitOfWorkFactory
    from tests.contract.dreaming_recovery_cases import fixture
    from tests.contract.reconsolidation_admission_cases import model, policy
    from tests.contract.reconsolidation_cases import Factory
    from tests.contract.reconsolidation_execution_cases import ExecutionProvider, TrackedFactory

    _, factory = await memory_uow_factory()
    service, manifest = await fixture(factory)
    owner = principal().model_copy(update={"scopes": {"memory.read", "memory.write"}})
    await service.recover_legacy_dreaming(owner, manifest, confirm=True)
    tracked = TrackedFactory(cast(Factory, factory))
    async with tracked() as uow:
        job = await uow.reconsolidation.claim_due(owner, NOW, "recovered-review")
        assert job is not None
        page = await uow.reconsolidation.inventory(owner, job.lease_token, NOW)
        selected = await uow.reconsolidation.selectable_sources(owner, page.sources, NOW)
        assert {s.belief_id for s in selected} == {UUID(int=501), UUID(int=502)}
        await uow.reconsolidation.checkpoint(owner, job.lease_token, page, (selected,), NOW)
        group = await uow.reconsolidation.claim_group(owner, job.lease_token, NOW)
        assert group is not None
    provider = ExecutionProvider(tracked)
    executor = ReconsolidationBatchExecutor(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        owner,
        admitted=lambda: True,
        egress_policy=policy,
    )
    result = await executor.run(
        job, (group.id,), model=model(), provider=provider, batch_id=UUID(int=90)
    )
    assert result.reason == "reviewed" and result.review is not None
    assert len(provider.requests) == 2


async def test_unidentified_erased_head_cannot_be_omitted_from_recovery() -> None:
    from uuid import UUID

    from agent_core.adapters.persistence.people import InMemoryPeopleStore
    from agent_core.domain.errors import ConflictError

    _, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"memory.read", "memory.write"}})
    service = PublicReconsolidationService(factory, FixedClock(NOW))
    boundary = await service.enable_forward_dreaming(owner)
    manifest = LegacyAttributionManifest(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        legacy_digest=boundary.legacy_digest,
        proofs=(),
    )
    async with factory() as uow:
        assert isinstance(uow.people, InMemoryPeopleStore)
        uow.people._erased.add((owner.tenant_id, owner.principal_id, UUID(int=9993)))
    with pytest.raises(ConflictError):
        await service.recover_legacy_dreaming(owner, manifest, confirm=True)
    assert (await service.dreaming_status(owner)).forward_only_since == NOW
