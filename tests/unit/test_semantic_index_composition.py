"""M33 indexing is opt-in, release-bound and isolated from foreground work."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_core.bootstrap import build
from agent_core.config import ConfigurationError, load_settings
from agent_core.memory.semantic_deployment import SemanticServices
from agent_core.runtime.worker import MaintenanceWorker
from tests.contract.support import memory_uow_factory
from tests.integration.m2_support import memory_settings
from tests.unit.test_config import base_environment


async def test_index_activation_refuses_missing_release_dependencies() -> None:
    settings = load_settings({**base_environment(), "AGENT_MEMORY_SEMANTIC_INDEX_ENABLED": "1"})
    with pytest.raises(ConfigurationError, match="Semantic index"):
        async with build(
            settings=replace(settings, sandbox=memory_settings().sandbox), storage="memory"
        ):
            pass


@pytest.mark.parametrize("stop", [True, False])
async def test_metadata_capture_cannot_reach_semantic_admission_or_return_a_runtime(
    monkeypatch: pytest.MonkeyPatch, stop: bool
) -> None:
    import agent_core.bootstrap as bootstrap
    from agent_core.execution.manager import SandboxManager
    from agent_core.memory.semantic_deployment import SemanticCompositionInputs

    settings = replace(memory_settings(), memory_semantic_index_enabled=True)
    captured = []
    closed = []
    original_close = SandboxManager.close

    async def close(manager: SandboxManager) -> None:
        closed.append(manager)
        await original_close(manager)

    def capture(inputs: SemanticCompositionInputs) -> None:
        captured.append(inputs)
        if stop:
            raise ValueError("Capture complete")

    async def forbidden(**_: Any) -> None:
        pytest.fail("Capture must stop before semantic admission or provider creation")

    monkeypatch.setattr(bootstrap, "compose_indexing", forbidden)
    monkeypatch.setattr(SandboxManager, "close", close)
    error = ValueError if stop else ConfigurationError
    message = "Capture complete" if stop else "capture callback must stop"
    with pytest.raises(error, match=message):
        async with build(settings=settings, model_metadata_only=True, recall_capture=capture):
            pytest.fail("Configuration capture cannot expose an executable runtime")
    assert len(captured) == 1
    assert captured[0].configuration["storage"] == "memory"
    assert len(closed) == 1, "A stopped capture must close its partially constructed resources"


async def test_recall_capture_requires_metadata_mode_before_model_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_core.bootstrap as bootstrap

    def forbidden(*_: Any, **__: Any) -> None:
        pytest.fail("Refuse invalid capture before model adapters are constructed")

    monkeypatch.setattr(bootstrap, "_provider_adapters", forbidden)
    with pytest.raises(ConfigurationError, match="capture requires model metadata"):
        async with build(settings=memory_settings(), recall_capture=lambda _: None):
            pytest.fail("Capture must be explicit metadata-only composition")


@pytest.mark.parametrize("value", ["maybe", "", "2"])
def test_index_flag_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ConfigurationError):
        load_settings({**base_environment(), "AGENT_MEMORY_SEMANTIC_INDEX_ENABLED": value})


async def test_disabled_index_ignores_unavailable_evidence(tmp_path: Path) -> None:
    settings = load_settings(
        {
            **base_environment(),
            "AGENT_MEMORY_SEMANTIC_INDEX_EVIDENCE": str(tmp_path / "absent.json"),
            "AGENT_MEMORY_SEMANTIC_INDEX_MANIFEST": str(tmp_path / "absent-manifest.json"),
        }
    )
    async with build(
        settings=replace(settings, sandbox=memory_settings().sandbox), storage="memory"
    ) as composition:
        worker = composition.maintenance_factory()
        assert isinstance(worker, MaintenanceWorker)
        assert worker._sweep_memory_semantic_index is None


async def test_index_slice_is_single_background_task_and_shutdown_awaits_cleanup() -> None:
    clock, factory = await memory_uow_factory()
    started, stopped = asyncio.Event(), asyncio.Event()
    calls = sweeps = 0

    async def index() -> int:
        nonlocal calls
        calls += 1
        assert not factory.is_open()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            stopped.set()
        return 0

    async def ordinary() -> int:
        nonlocal sweeps
        sweeps += 1
        return 0

    worker = MaintenanceWorker(
        uow_factory=factory, clock=clock, sweep_memory=ordinary, sweep_memory_semantic_index=index
    )
    await worker.run_once()
    await asyncio.sleep(0)
    assert started.is_set(), "Indexing must be dispatched outside maintenance transactions"
    await worker.run_once()
    assert calls == 1 and sweeps == 2
    worker.stop()
    await worker.run_forever()
    assert stopped.is_set(), "Shutdown must await cancelled indexing cleanup"
    await worker.run_once()
    assert calls == 1


def release_files(
    tmp_path: Path, dependencies: dict[str, str] | None = None
) -> tuple[Path, Path, dict[str, str]]:
    from agent_core.memory.semantic_deployment import IndexConfiguration, active_index_dependencies
    from tests.contract.semantic_indexing_cases import INDEX_CONFIG
    from tests.unit.test_general_recall_release import digest, fixture

    profile = IndexConfiguration(credential_ref="openai", embedding=INDEX_CONFIG)
    dependencies = dependencies or active_index_dependencies(
        profile,
        configuration={"policy": "reviewed"},
        formation={"policy": "formation"},
        reader={"model": "reader"},
    )
    bundle = fixture()
    identity = bundle.identity.model_copy(update=dependencies)
    identity_hash = digest(identity.model_dump_json())
    bundle = bundle.model_copy(
        update={
            "identity": identity,
            "observations": tuple(
                row.model_copy(update={"identity_sha256": identity_hash})
                for row in bundle.observations
            ),
            "gates": tuple(
                row.model_copy(update={"identity_sha256": identity_hash}) for row in bundle.gates
            ),
        }
    )
    manifest, evidence = tmp_path / "manifest.json", tmp_path / "evidence.json"
    manifest.write_text(identity.model_dump_json())
    evidence.write_text(bundle.model_dump_json())
    return manifest, evidence, dependencies


def test_manifest_binds_real_verifier_and_withdrawal_latches(tmp_path: Path) -> None:
    from agent_core.memory.semantic_deployment import bind_index_manifest
    from tests.unit.test_general_recall_release import ROOT

    manifest, evidence, dependencies = release_files(tmp_path)
    admitted = bind_index_manifest(
        ROOT, manifest, evidence, "20261011-120000-aaaaaaa", dependencies
    )
    assert admitted(), (
        "A matching reviewed manifest and passing measured bundle must admit indexing"
    )
    original = manifest.read_bytes()
    manifest.unlink()
    assert not admitted()
    manifest.write_bytes(original)
    assert not admitted(), "Restoration must not revive a withdrawn binding"


@pytest.mark.parametrize(
    "changed",
    [
        "configuration_sha256",
        "embedding_configuration_sha256",
        "formation_configuration_sha256",
        "reader_configuration_sha256",
        "release",
        "missing_dependency",
    ],
)
def test_manifest_refuses_changed_active_dependencies(tmp_path: Path, changed: str) -> None:
    from agent_core.memory.semantic_deployment import bind_index_manifest
    from tests.unit.test_general_recall_release import ROOT

    manifest, evidence, dependencies = release_files(tmp_path)
    release = "20261011-120000-aaaaaaa"
    if changed == "release":
        release = "20261011-120000-bbbbbbb"
    elif changed == "missing_dependency":
        dependencies.pop("configuration_sha256")
    else:
        dependencies[changed] = "0" * 64
    with pytest.raises(ValueError):
        bind_index_manifest(ROOT, manifest, evidence, release, dependencies)


async def configured_index(
    tmp_path: Path, *, rejected: str = ""
) -> tuple[dict[str, Any], Any, list[str], Path, Path]:
    import json

    from agent_core.domain.credentials import CredentialRef, SecretValue
    from agent_core.memory import semantic_deployment as deployment
    from tests.contract.semantic_indexing_cases import INDEX_CONFIG, Provider
    from tests.contract.support import principal
    from tests.unit.test_general_recall_release import ROOT

    clock, factory = await memory_uow_factory()
    config = INDEX_CONFIG.model_copy(
        update={
            "space": INDEX_CONFIG.space.model_copy(update={"provider": "openai"}),
            "residency_provider": "openai",
        }
    )
    overlay = tmp_path / "config" / "memory"
    overlay.mkdir(parents=True)
    document: dict[str, Any] = {
        "schema_version": 1,
        "configurations": [
            {"credential_ref": "openai", "embedding": config.model_dump(mode="json")}
        ],
    }
    (overlay / "semantic-index.yaml").write_text(json.dumps(document))
    settings = replace(
        memory_settings(),
        config_dir=overlay.parent,
        memory_semantic_index_enabled=True,
        memory_semantic_index_manifest=tmp_path / "manifest.json",
        memory_semantic_index_evidence=tmp_path / "evidence.json",
        release_id="20261011-120000-aaaaaaa",
    )
    profile = deployment.IndexProfiles.model_validate(document).configurations[0]
    deps = deployment.active_index_dependencies(
        profile,
        configuration={"policy": "reviewed"},
        formation={"policy": "formation"},
        reader={"model": "reader"},
    )
    manifest, evidence, _ = release_files(tmp_path, deps)
    if rejected == "evidence":
        evidence.write_text("{}")
    if rejected == "manifest":
        manifest.unlink()
    if rejected == "policy":
        document["configurations"][0]["embedding"]["egress_revision"] = "unknown"
        (overlay / "semantic-index.yaml").write_text(json.dumps(document))
    calls = []

    class Credentials:
        async def resolve(self, reference: CredentialRef) -> SecretValue:
            calls.append(reference.name)
            assert not factory.is_open()
            if rejected == "credential":
                raise PermissionError
            return SecretValue("unit-secret")

    provider = Provider(factory)

    def provider_factory(key: str) -> Provider:
        assert key == "unit-secret" and calls == ["openai"]
        return provider

    kwargs: dict[str, Any] = {
        "root": ROOT,
        "settings": settings,
        "principal": principal(),
        "factory": factory,
        "clock": clock,
        "credentials": Credentials(),
        "configuration": {"policy": "reviewed"},
        "formation": {"policy": "formation"},
        "reader": {"model": "reader"},
        "provider_factory": provider_factory,
    }
    return kwargs, provider, calls, manifest, evidence


async def test_composed_index_sends_only_permitted_sources_and_stops_on_evidence_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_core.domain.memory import Sensitivity
    from agent_core.memory.semantic_deployment import compose_indexing
    from tests.contract.memory_fixtures import memory

    kwargs, provider, calls, _, evidence = await configured_index(tmp_path)
    index = await compose_indexing(**kwargs)
    assert index is not None and calls == ["openai"] and not provider.calls
    async with kwargs["factory"]() as uow:
        await uow.memories.upsert_belief(memory(statement="User prefers morning walks"))
        await uow.memories.upsert_belief(
            memory(belief_id=4444, statement="User prefers quiet evenings").model_copy(
                update={"sensitivity": Sensitivity.SENSITIVE}
            )
        )
    outcome = await index.index.run_slice()
    assert outcome.indexed == 1 and outcome.deferred == 1
    assert len(provider.calls) == 1
    assert "morning walks" in provider.calls[0].texts[0]
    raw = evidence.read_bytes()
    evidence.unlink()
    assert (await index.index.run_slice()).reason == "disabled"
    evidence.write_bytes(raw)
    assert (await index.index.run_slice()).reason == "disabled" and len(provider.calls) == 1


@pytest.mark.parametrize("rejected", ["evidence", "manifest", "policy", "credential"])
async def test_invalid_admission_does_not_create_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rejected: str
) -> None:
    from agent_core.memory.semantic_deployment import compose_indexing

    kwargs, provider, calls, _, _ = await configured_index(tmp_path, rejected=rejected)
    with pytest.raises(ConfigurationError, match="Semantic index"):
        await compose_indexing(**kwargs)
    assert not provider.calls
    assert calls == (["openai"] if rejected == "credential" else [])


async def test_maintenance_defers_indexing_while_a_run_holds_a_live_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from agent_core.domain.runs import RunStatus
    from tests.contract.support import NOW, run

    clock, factory = await memory_uow_factory()
    async with factory() as uow:
        await uow.runs.create(
            run(status=RunStatus.RUNNING).model_copy(
                update={
                    "lease_owner": "foreground",
                    "lease_expires_at": NOW + timedelta(seconds=30),
                }
            )
        )

    # The in-memory maintenance adapter intentionally has no durable leases.
    async def leases() -> frozenset[tuple[UUID, int]]:
        from tests.contract.support import RUN_ID

        return (
            frozenset({(RUN_ID, 1)}) if clock.now() < NOW + timedelta(seconds=30) else frozenset()
        )

    async with factory() as uow:
        monkeypatch.setattr(uow.maintenance, "live_run_leases", leases)
    calls = []

    async def index() -> int:
        calls.append("started")
        return 0

    worker = MaintenanceWorker(uow_factory=factory, clock=clock, sweep_memory_semantic_index=index)
    await worker.run_once()
    await asyncio.sleep(0)
    assert calls == []
    await clock.sleep(31)
    await worker.run_once()
    await asyncio.sleep(0)
    assert calls == ["started"]
    worker.stop()
    await worker.run_forever()


async def test_index_failure_does_not_stop_maintenance_or_log_provider_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, factory = await memory_uow_factory()
    calls = []

    async def index() -> int:
        calls.append("started")
        raise ValueError("Private provider error payload")

    worker = MaintenanceWorker(uow_factory=factory, clock=clock, sweep_memory_semantic_index=index)
    for _ in range(2):
        await worker.run_once()
        await asyncio.sleep(0)
    assert calls == ["started", "started"]
    assert "ValueError" in caplog.text and "Private provider" not in caplog.text
    worker.stop()
    await worker.run_forever()


async def test_bootstrap_wires_the_admitted_index_to_maintenance_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_core.bootstrap as bootstrap
    import agent_core.memory.semantic_deployment as deployment
    from tests.contract.memory_fixtures import memory
    from tests.contract.semantic_indexing_cases import Provider
    from tests.contract.support import principal

    kwargs, _, _, _, evidence = await configured_index(tmp_path)
    real_compose = deployment.compose_indexing
    real_bind = deployment.bind_index_manifest
    providers = []
    configurations = []

    def bind(
        root: Path,
        manifest: Path,
        path: Path,
        release: str,
        dependencies: dict[str, str],
        **context: Any,
    ) -> Callable[[], bool]:
        release_files(tmp_path, dependencies)
        return real_bind(root, manifest, path, release, dependencies, **context)

    async def compose(**options: Any) -> SemanticServices | None:
        configurations.append(options["configuration"])
        provider = Provider(options["factory"])
        providers.append(provider)
        options["provider_factory"] = lambda _: provider
        return await real_compose(**options)

    monkeypatch.setattr(deployment, "bind_index_manifest", bind)
    monkeypatch.setattr(bootstrap, "compose_indexing", compose)
    async with build(
        settings=kwargs["settings"],
        storage="memory",
        principal=principal(),
        clock=kwargs["clock"],
        credential_resolver=kwargs["credentials"],
    ) as app:
        assert len(providers) == 1 and not providers[0].calls
        assert configurations[0]["query_authority"] == "local-reviewed-reader@1;remote-withheld"
        assert app.recall_composition.configuration == configurations[0]
        assert app.executor._prepare_recall is None
        async with app.uow_factory() as uow:
            await uow.memories.upsert_belief(memory(statement="User prefers morning walks"))
        worker = app.maintenance_factory()
        assert isinstance(worker, MaintenanceWorker)
        await worker.run_once()
        assert worker._semantic_index_task is not None
        await worker._semantic_index_task
        assert len(providers[0].calls) == 1
        evidence.unlink()
        await worker.run_once()
        assert worker._semantic_index_task is not None
        await worker._semantic_index_task
        assert len(providers[0].calls) == 1
        worker.stop()
        await worker.run_forever()


async def test_recall_dependency_capture_uses_the_effective_inactive_composition() -> None:
    from tests.contract.support import principal

    owner = principal().model_copy(update={"scopes": {"memory.read", "memory.write"}})
    async with build(
        settings=memory_settings(), principal=owner, enabled_tools=["memory.search"]
    ) as app:
        captured = getattr(app, "recall_composition", None)
        assert captured is not None, "Confirmation must resolve inputs before index activation"
        assert captured.configuration["principal"] == {
            "tenant": owner.tenant_id,
            "owner": owner.principal_id,
            "scopes": sorted(owner.scopes),
            "roles": sorted(owner.roles),
        }
        assert captured.configuration["memory"] == app.memory_profiles.model_dump(mode="json")
        assert captured.configuration["agent"]["enabled_tools"] == ["memory.search"]
        assert captured.configuration["storage"] == "memory"
        assert captured.formation["profiles"] == app.memory_profiles.formation.model_dump(
            mode="json"
        )
        assert captured.reader["registry"] == captured.formation["registry"]
        assert app.semantic_services is None


async def test_metadata_only_capture_matches_normal_composition() -> None:
    from tests.contract.support import NOW, principal

    captured = []
    for metadata_only in (False, True):
        async with build(
            settings=memory_settings(),
            principal=principal(),
            fixed_clock_at=NOW,
            sequential_ids=True,
            model_metadata_only=metadata_only,
        ) as app:
            captured.append(app.recall_composition)
    assert captured[0] == captured[1]


async def test_repeated_stop_does_not_interrupt_index_spend_cleanup() -> None:
    clock, factory = await memory_uow_factory()
    started, cleaning, cleaned, finish = (asyncio.Event() for _ in range(4))

    async def index() -> int:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish.wait()
            cleaned.set()
        return 0

    worker = MaintenanceWorker(uow_factory=factory, clock=clock, sweep_memory_semantic_index=index)
    await worker.run_once()
    await started.wait()
    worker.stop()
    await cleaning.wait()
    worker.stop()
    finish.set()
    await worker.run_forever()
    assert cleaned.is_set(), "A repeated stop must not cancel in-progress spend cleanup"


async def test_production_indexing_requires_durable_storage(tmp_path: Path) -> None:
    from agent_core.config import DeploymentMode
    from agent_core.memory.semantic_deployment import compose_indexing

    kwargs, _, calls, _, _ = await configured_index(tmp_path)
    kwargs["settings"] = replace(kwargs["settings"], deployment_mode=DeploymentMode.PRODUCTION)
    kwargs["configuration"]["storage"] = "memory"
    from agent_core.config import load_config_document
    from agent_core.memory.semantic_deployment import (
        DOCUMENT,
        IndexProfiles,
        active_index_dependencies,
    )

    profile = IndexProfiles.model_validate(
        load_config_document(kwargs["settings"], DOCUMENT)
    ).configurations[0]
    release_files(
        tmp_path,
        active_index_dependencies(
            profile,
            configuration=kwargs["configuration"],
            formation=kwargs["formation"],
            reader=kwargs["reader"],
        ),
    )
    with pytest.raises(ConfigurationError, match="PostgreSQL"):
        await compose_indexing(**kwargs)
    assert calls == []


@pytest.mark.parametrize(
    "change",
    ["empty", "multiple", "unknown", "provider", "residency", "interpolation", "missing_price"],
)
async def test_index_configuration_is_explicit_closed_and_reviewed(
    tmp_path: Path, change: str
) -> None:
    import json

    from agent_core.memory.semantic_deployment import compose_indexing

    kwargs, provider, calls, _, _ = await configured_index(tmp_path)
    path = tmp_path / "config/memory/semantic-index.yaml"
    document = json.loads(path.read_text())
    profile = document["configurations"][0]
    if change == "empty":
        document["configurations"] = []
    elif change == "multiple":
        document["configurations"].append(profile)
    elif change == "unknown":
        profile["guess_private_content_is_safe"] = True
    elif change == "provider":
        profile["embedding"]["space"]["provider"] = "unknown"
    elif change == "residency":
        profile["embedding"]["residency_provider"] = "unknown"
    elif change == "interpolation":
        profile["embedding"]["space"]["model"] = "${OPENAI_MODEL}"
    else:
        del profile["embedding"]["usd_per_million_tokens"]
    path.write_text(json.dumps(document))
    with pytest.raises(ConfigurationError, match="Semantic index"):
        await compose_indexing(**kwargs)
    assert calls == [] and provider.calls == []


@pytest.mark.parametrize("change", ["duplicate", "symlink", "directory", "invalid_revision"])
def test_manifest_requires_unambiguous_regular_release_identity(
    tmp_path: Path, change: str
) -> None:
    from agent_core.memory.semantic_deployment import bind_index_manifest
    from tests.unit.test_general_recall_release import ROOT

    manifest, evidence, dependencies = release_files(tmp_path)
    release = "20261011-120000-aaaaaaa"
    if change == "duplicate":
        manifest.write_text(
            manifest.read_text().replace("{", '{"build_ref":"' + "a" * 40 + '",', 1)
        )
    elif change == "invalid_revision":
        release = "20261011-120000-a"
    else:
        original = tmp_path / "original.json"
        manifest.rename(original)
        if change == "symlink":
            manifest.symlink_to(original)
        else:
            manifest.mkdir()
    with pytest.raises((OSError, ValueError)):
        bind_index_manifest(ROOT, manifest, evidence, release, dependencies)
