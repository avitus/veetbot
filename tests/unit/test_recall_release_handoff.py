"""Exercise the deployment handoff against a real bounded restart fixture."""

import hashlib
import json
import os
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_core.memory.semantic_deployment import bind_index_manifest
from tests.unit.test_bounded_recall_restart import restart_files

pytestmark = pytest.mark.static
SOURCE = "20261031-000001-aaaaaaa"
TARGET = "20261031-000002-ccccccc"
PERMISSIONS = "20261031-000003-ddddddd"


@pytest.mark.parametrize("revoke", ["expiry", "withdraw", "renew", "retry", "tamper", "source"])
def test_handoff_is_bound_to_live_trial_and_deployment_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revoke: str
) -> None:
    kwargs, manifest, _ = restart_files(tmp_path, monkeypatch)
    assert bind_index_manifest(**kwargs)()
    target = tmp_path / TARGET
    shutil.copytree(kwargs["root"], target)
    (target / ".venv").mkdir()
    (target / ".venv/recall-handoff-attempt").write_bytes(b"test cutover")
    (target / "README.md").write_text("Operator documentation only.")
    previous = kwargs["manifest"].read_bytes()
    evidence = kwargs["evidence"].read_bytes()
    receipt = {
        "version": "recall-release-handoff@1",
        "source_release": SOURCE,
        "target_release": TARGET,
        "deployment_build_ref": "c" * 40,
        "manifest_sha256": hashlib.sha256(previous).hexdigest(),
        "evidence_sha256": hashlib.sha256(evidence).hexdigest(),
        "rollback_build_ref": manifest["rollback_build_ref"],
        "attempt_sha256": hashlib.sha256(b"test cutover").hexdigest(),
        "environment_sha256": "1" * 64,
        "review_sha256": "2" * 64,
        "ci_sha256": "3" * 64,
        "rollback_verification_sha256": "4" * 64,
    }
    from agent_core.memory.recall_release_binding import RecallInputManifest

    receipt["inputs_sha256"] = RecallInputManifest.model_validate(manifest["target_inputs"]).digest
    (tmp_path / ("handoff-" + hashlib.sha256(b"test cutover").hexdigest() + ".json")).write_text(
        json.dumps(receipt)
    )
    monkeypatch.setattr("agent_core.memory.recall_handoff.TRIALS", tmp_path.parent)
    monkeypatch.setattr("agent_core.memory.recall_handoff.OPERATOR_UID", os.getuid())
    kwargs.update(root=target, release_id=TARGET)
    active = bind_index_manifest(**kwargs)
    assert active()
    assert kwargs["manifest"].read_bytes() == previous
    assert kwargs["evidence"].read_bytes() == evidence
    kwargs["dependencies"]["configuration_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        bind_index_manifest(**kwargs)
    if revoke == "expiry":
        kwargs["clock"].value += timedelta(hours=24)
    elif revoke == "withdraw":
        kwargs["manifest"].rename(tmp_path / "manifest.withdrawn.json")
    elif revoke == "renew":
        kwargs["manifest"].write_bytes(previous + b" ")
    elif revoke == "retry":
        (target / ".venv/recall-handoff-attempt").write_bytes(b"other attempt")
    elif revoke == "tamper":
        (tmp_path / ("handoff-" + hashlib.sha256(b"test cutover").hexdigest() + ".json")).chmod(
            0o666
        )
    else:
        (target / "runtime.py").write_text("Changed runtime")
    assert not active()


async def test_deployed_composition_retrieves_and_executes_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use real trial binding, bootstrap, index/retrieval and job execution with local fixtures."""
    from dataclasses import replace
    from uuid import UUID

    import yaml

    import agent_core.bootstrap as bootstrap
    from agent_core.domain.embeddings import EmbeddingRequest, EmbeddingResponse
    from agent_core.domain.events import NewEvent, conversation_items
    from agent_core.domain.runs import RunCheckpoint, RunStatus
    from agent_core.memory.recall_release_binding import capture_inputs
    from agent_core.memory.semantic_deployment import IndexProfiles, active_index_dependencies
    from agent_core.operations import recall_release_handoff as issuer
    from agent_core.operations.recall_release_handoff import Environment, Verification
    from tests.contract.memory_fixtures import memory, recall_query
    from tests.contract.support import RUN_ID, SESSION_ID, principal, run, session
    from tests.contract.test_job_store_contract import record
    from tests.unit.test_bounded_recall_admission import write
    from tests.unit.test_job_execution import Runner
    from tests.unit.test_local_semantic_composition import local_options

    class Encoder:
        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            return EmbeddingResponse(
                request_digest=request.digest,
                vectors=tuple((1.0, 0.0, 0.0) for _ in request.texts),
                input_tokens=1,
            )

        async def close(self) -> None:
            pass

    opts, _, _, _ = await local_options(tmp_path)
    settings = replace(opts["settings"], jobs_enabled=True, release_id=SOURCE)
    profiles = yaml.safe_load(Path("src/agent_core/memory/profiles.yaml").read_text())
    profiles["retrieval"]["semantic_enabled"] = True
    (settings.config_dir / "memory/profiles.yaml").write_text(yaml.safe_dump(profiles))
    kw, manifest, evidence = restart_files(tmp_path, monkeypatch)
    deployments = tmp_path / "deployments"
    releases = deployments / "releases"
    releases.mkdir(parents=True)
    source = releases / SOURCE
    kw["root"].rename(source)
    target = releases / TARGET
    shutil.copytree(source, target)
    (target / ".venv").mkdir()
    (target / ".venv/recall-handoff-attempt").write_bytes(b"test cutover")
    (target / "README.md").write_text("Updated documentation.")
    monkeypatch.setattr("agent_core.memory.recall_handoff.TRIALS", tmp_path.parent)
    monkeypatch.setattr("agent_core.memory.recall_handoff.OPERATOR_UID", os.getuid())
    monkeypatch.setattr(bootstrap._CockpitMaintenance, "sweeps", lambda self: {})
    from agent_core.memory.semantic_deployment import SemanticServices, compose_indexing

    async def compose(**options: Any) -> SemanticServices | None:
        options["root"] = releases / options["settings"].release_id
        options["local_provider_factory"] = lambda *_: Encoder()
        return await compose_indexing(**options)

    monkeypatch.setattr(bootstrap, "compose_indexing", compose)
    owner = principal()
    common: dict[str, Any] = {
        "sequential_ids": True,
        "storage": "memory",
        "clock": kw["clock"],
        "job_runner_override": Runner(),
        "credential_resolver": opts["credentials"],
    }
    async with bootstrap.build(settings=settings, principal=owner, **common) as app:
        composition = app.recall_composition
    profile = IndexProfiles.model_validate_json(
        (settings.config_dir / "memory/semantic-index.yaml").read_bytes()
    ).configurations[0]
    deps = active_index_dependencies(
        profile,
        configuration=composition.configuration,
        formation=composition.formation,
        reader=composition.reader,
    )
    inputs = capture_inputs(source, manifest["target_inputs"]["dependencies"] | deps)
    manifest["target_inputs"] = inputs.model_dump(mode="json")
    comparison = evidence["compatibility"]
    comparison["target_inputs_sha256"] = inputs.digest
    for check in comparison["checks"]:
        check["inputs_sha256"] = inputs.digest
    comparison["dependency_changes"] = {
        k: {
            "before": evidence["measured_inputs"]["dependencies"][k],
            "after": v,
            "rationale": "Synthetic fixture binds the actual assembled role configuration.",
            "checks": ["full-check"],
        }
        for k, v in deps.items()
        if evidence["measured_inputs"]["dependencies"][k] != v
    }
    evidence["production_restart"]["target_inputs_sha256"] = inputs.digest
    write(kw, manifest, evidence)
    expected_bytes = kw["manifest"].read_bytes(), kw["evidence"].read_bytes()
    environment = Environment(**dict.fromkeys(Environment.model_fields, "1" * 64))
    proof = Verification(
        source_release=SOURCE,
        target_release=TARGET,
        target_commit="c" * 40,
        prior_manifest_sha256=hashlib.sha256(expected_bytes[0]).hexdigest(),
        evidence_sha256=hashlib.sha256(expected_bytes[1]).hexdigest(),
        rollback_build_ref=manifest["rollback_build_ref"],
        source_environment=environment,
        target_environment=environment,
        review_sha256="2" * 64,
        ci_sha256="3" * 64,
        rollback_verification_sha256="4" * 64,
    )
    (deployments / "current").symlink_to(source)
    approvals = tmp_path / "approved"
    approvals.mkdir()
    trial_root = tmp_path / "trials"
    trial_root.mkdir()
    trial = trial_root / "active"
    trial.mkdir()
    for key in ("manifest", "evidence"):
        path = kw[key]
        kw[key] = trial / path.name
        path.rename(kw[key])
    settings = replace(
        settings,
        memory_semantic_index_manifest=kw["manifest"],
        memory_semantic_index_evidence=kw["evidence"],
    )
    monkeypatch.setattr("agent_core.memory.recall_handoff.TRIALS", trial_root)
    monkeypatch.setattr(issuer, "TRIALS", trial_root)
    from agent_core.operations import recall_handoff_controller as controller

    monkeypatch.setattr(controller, "CONTROLLER", approvals)
    (approvals / "enrollment.json").write_text(
        controller.Enrollment(
            manifest_sha256=proof.prior_manifest_sha256,
            evidence_sha256=proof.evidence_sha256,
            environment=environment,
            rollback_build_ref=proof.rollback_build_ref,
        ).model_dump_json()
    )

    permission_drift = True

    def observed(name: str, *args: str) -> bytes:
        if name == "environment":
            current = (
                environment
                if args != (PERMISSIONS,) or not permission_drift
                else environment.model_copy(update={"permissions_sha256": "5" * 64})
            )
            return current.model_dump_json().encode()
        if name == "recovery":
            return (
                controller.Recovery(
                    build_ref=proof.rollback_build_ref,
                    schema_sha256=environment.schema_sha256,
                    assets_and_surfaces_verified=True,
                    schema_guard_passed=True,
                    verification_sha256="4" * 64,
                )
                .model_dump_json()
                .encode()
            )
        assert name == "release-clearance" and args in [(TARGET,), (PERMISSIONS,)]
        return (
            controller.Clearance(
                target_release=args[0],
                target_commit=proof.target_commit if args[0] == TARGET else "d" * 40,
                reviewed_head="d" * 40,
                reviewed_tree="e" * 40,
                merge_tree="e" * 40,
                final_head_review_clear=True,
                all_review_forms_disposed=True,
                final_head_hosted_ci_passed=True,
                review_receipt_sha256="2" * 64,
                ci_receipt_sha256="3" * 64,
            )
            .model_dump_json()
            .encode()
        )

    monkeypatch.setattr(controller, "probe", observed)
    monkeypatch.setattr(issuer, "APP", deployments)
    for role, rel, semantic in [
        (owner, SOURCE, True),
        (owner, TARGET, True),
        (owner, PERMISSIONS, False),
        (owner.model_copy(update={"scopes": owner.scopes | {"extra"}}), TARGET, False),
    ]:
        if rel == TARGET and semantic:
            issuer.main(TARGET, "--prepare", clock=kw["clock"])
            issuer.main(TARGET, "--commit", clock=kw["clock"])
            (deployments / "current").unlink()
            (deployments / "current").symlink_to(target)
        if rel == PERMISSIONS:
            changed = releases / PERMISSIONS
            shutil.copytree(target, changed)
            (changed / ".venv/recall-handoff-attempt").write_bytes(b"new attempt")
            with pytest.raises(ValueError, match="environment"):
                issuer.main(PERMISSIONS, clock=kw["clock"])
        async with bootstrap.build(
            settings=replace(settings, release_id=rel), principal=role, **common
        ) as app:
            assert await app.readiness_probe()
            assert (app.semantic_services is not None) == semantic
            item = memory(statement="User prefers morning walks")
            async with app.uow_factory() as uow:
                await uow.sessions.create(session())
                await uow.memories.upsert_belief(item)
                await uow.jobs.put(record(role), expected_version=None)
            if semantic:
                assert app.semantic_services is not None
                assert (
                    app.semantic_services.admitted is not None and app.semantic_services.admitted()
                )
                assert (await app.semantic_services.index.run_slice()).indexed == 1
            q = recall_query(text="morning walks").model_copy(
                update={"candidate_selection": "relevance"}
            )
            async with app.uow_factory() as uow:
                live = run(status=RunStatus.RUNNING).model_copy(
                    update={
                        "lease_owner": "worker",
                        "lease_epoch": 1,
                        "lease_expires_at": kw["clock"].now() + timedelta(minutes=5),
                    }
                )
                await uow.runs.create(live)
                event = await uow.events.append(
                    NewEvent(
                        session_id=SESSION_ID,
                        run_id=RUN_ID,
                        actor_type="principal",
                        actor_id=role.principal_id,
                        event_type="user.message.created",
                        payload={"content": "morning walks"},
                    )
                )
                await uow.runs.set_seed_event_sequence(RUN_ID, event.sequence)
                await uow.checkpoints.write(
                    RUN_ID,
                    RunCheckpoint(
                        run_id=RUN_ID,
                        version=1,
                        status=RunStatus.RUNNING,
                        created_at=kw["clock"].now(),
                        conversation=conversation_items(event),
                        last_event_sequence=event.sequence,
                        budget_state={"context_reader_sha256": "a" * 64},
                    ),
                    full=True,
                )
            prepared = await app.memory_retriever.prepare_semantic_queries(
                [q], session_id=SESSION_ID, run_id=RUN_ID
            )
            assert prepared == int(semantic)
            recalled = await app.memory_retriever.recall(q, session_id=SESSION_ID, run_id=RUN_ID)
            assert item.id in [x.belief_id for x in recalled.items]
            async with app.uow_factory() as uow:
                trace = await uow.traces.get(recalled.trace_id, role)
            assert ("semantic" in trace.arm_latencies_ms) == semantic
            async with app.uow_factory() as uow:
                events = await uow.events.list_after(SESSION_ID, 0, role, limit=100)
            event = next(e for e in events if e.event_type == "memory.recalled")
            if semantic:
                ref = trial / (
                    "manifest.json"
                    if rel == SOURCE
                    else "handoff-" + hashlib.sha256(b"test cutover").hexdigest() + ".json"
                )
                assert (
                    event.payload["semantic_binding_sha256"]
                    == hashlib.sha256(ref.read_bytes()).hexdigest()
                )
            else:
                assert "semantic_binding_sha256" not in event.payload

            async def unused_model(**options: Any) -> dict[str, Any]:
                pytest.fail("Saved job unexpectedly invoked a model")

            assert app.job_execution is not None
            job = await app.job_execution.run(
                principal=role,
                slug="count-items",
                revision_id=UUID(int=1),
                grant_id=UUID(int=2),
                run_id=UUID(int=3),
                lease_epoch=0,
                model=unused_model,
            )
            assert job.value == {"count": 2}
    # Read back the exact per-role binding and record origin retirement. The fixture
    # supplies Linux process/journal metadata; all comparisons are the deployed code.
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(issuer, "PROCESSES", proc)
    units = ["api", "worker", "async-worker", "maintenance", "schedule", "call", "surface"]
    for n in range(1, len(units) + 1):
        pid = proc / str(n)
        pid.mkdir()
        (pid / "cwd").symlink_to(target)
        (pid / "environ").write_bytes(
            (
                f"AGENT_JOBS_ENABLED={int(n <= 5)}\0"
                f"AGENT_MEMORY_SEMANTIC_INDEX_ENABLED={int(n <= 4)}"
            ).encode()
        )
    receipt_sha = hashlib.sha256(next(trial.glob("handoff-[!p]*.json")).read_bytes()).hexdigest()

    def status(args: list[str], **options: Any) -> str | bytes:
        if args[0].endswith("journalctl"):
            return f"semantic_trial_composed binding={receipt_sha} jobs=1".encode()
        return str(1 + units.index(args[-1].removeprefix("veetbot-").removesuffix(".service")))

    monkeypatch.setattr("subprocess.check_output", status)
    issuer.main(TARGET, "--verify", clock=kw["clock"])
    kw.update(root=source, release_id=SOURCE, dependencies=deps)
    with pytest.raises(ValueError, match="retired original"):
        bind_index_manifest(**kw)
    # Renewal after preflight wins over an in-flight cutover.
    permission_drift = False
    issuer.main(PERMISSIONS, "--prepare", clock=kw["clock"])
    kw["manifest"].write_bytes(expected_bytes[0] + b" ")
    with pytest.raises(ValueError, match="identity mismatch"):
        issuer.main(PERMISSIONS, "--commit", clock=kw["clock"])
    assert not (
        trial / ("handoff-" + hashlib.sha256(b"new attempt").hexdigest() + ".json")
    ).exists()
    assert kw["manifest"].read_bytes() == expected_bytes[0] + b" "
    assert kw["evidence"].read_bytes() == expected_bytes[1]
