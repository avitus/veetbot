"""Offline semantic preparation through real worker leases and PostgreSQL recall."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_core.domain.agents import Principal
from agent_core.ports.determinism import Clock
from tests.unit.test_local_semantic_composition import local_options
from tests.unit.test_semantic_index_composition import release_files


@pytest.mark.parametrize("path", ["automatic", "deliberate"])
async def test_bootstrap_prepares_local_vectors_for_actual_context_and_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    from dataclasses import replace

    import yaml

    import agent_core.bootstrap as bootstrap
    from agent_core.adapters.determinism import FixedClock
    from agent_core.domain.memory import BeliefType, Sensitivity
    from agent_core.domain.messages import (
        FakeModelScript,
        ScriptedToolCall,
        ScriptedTurn,
        StopReason,
    )
    from agent_core.memory import semantic_deployment as deployment
    from agent_core.runtime.worker import DurableWorker
    from tests.contract.memory_fixtures import memory
    from tests.contract.semantic_indexing_cases import Provider
    from tests.contract.support import NOW, agent, principal, session
    from tests.gates.test_runtime_tool_budget import _settings
    from tests.integration.m2_support import database_settings

    class HeldClock(FixedClock):
        async def sleep(self, seconds: float) -> None:
            # Worker heartbeats wait in real time without racing the fixture clock.
            await asyncio.sleep(seconds)

    async with bootstrap.build(
        settings=_settings(),
        principal=principal(),
        clock=HeldClock(NOW),
        script=FakeModelScript(turns=[ScriptedTurn(text="Done")]),
    ) as app:
        run_id = await app.runs.submit("Measure reader identity")
        await app.runs.wait_terminal(run_id)
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
        assert checkpoint is not None
        reader = checkpoint.budget_state["context_reader_sha256"]
    kwargs, _, _, _ = await local_options(tmp_path)
    folder = kwargs["settings"].config_dir / "memory"
    document = json.loads((folder / "semantic-index.yaml").read_text())
    document["configurations"][0]["reader_sha256"] = reader
    (folder / "semantic-index.yaml").write_text(json.dumps(document))
    profiles = yaml.safe_load(Path("src/agent_core/memory/profiles.yaml").read_text())
    profiles["retrieval"]["semantic_enabled"] = True
    (folder / "profiles.yaml").write_text(yaml.safe_dump(profiles))
    actual_bind = deployment.bind_index_manifest
    actual_compose = deployment.compose_indexing
    captured: list[Any] = []
    lifecycle: list[str] = []
    owner = principal()
    runtime_clock = HeldClock(NOW)

    def bind(
        root: Path,
        manifest: Path,
        evidence: Path,
        release: str,
        dependencies: dict[str, str],
        *,
        principal: Principal,
        clock: Clock,
    ) -> Any:
        # The fixture substitutes evidence, never the composition's authority.
        assert principal == owner
        assert clock is runtime_clock
        release_files(tmp_path, dependencies)
        return actual_bind(
            root, manifest, evidence, release, dependencies, principal=principal, clock=clock
        )

    async def compose(**options: Any) -> Any:
        provider = Provider(options["factory"])

        async def close() -> None:
            lifecycle.append("closed")

        provider.close = close  # type: ignore[attr-defined]
        options["local_provider_factory"] = lambda *_: provider
        result = await actual_compose(**options)
        captured.append((provider, result))
        return result

    monkeypatch.setattr(deployment, "bind_index_manifest", bind)
    monkeypatch.setattr(bootstrap, "compose_indexing", compose)
    turns = [ScriptedTurn(text="Done")]
    if path == "deliberate":
        turns.insert(
            0,
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        call_id="search",
                        name="memory.search",
                        arguments={
                            "text": "Which breakfast fits my constraints?",
                            "scope": "general",
                            "type_hints": ["fact"],
                        },
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
        )
    async with bootstrap.build(
        settings=replace(kwargs["settings"], database_url=database_settings().database_url),
        storage="postgres",
        principal=owner,
        clock=runtime_clock,
        script=FakeModelScript(turns=turns),
    ) as app:
        provider, services = captured[0]
        assert services is not None and services.queries is not None
        assert app.executor._prepare_recall is not None
        original = memory(statement="Dairy causes the user digestive discomfort").model_copy(
            update={
                "subject": "diet",
                "sensitivity": Sensitivity.RESTRICTED,
                "belief_type": BeliefType.USER_MODEL_ATTR,
            }
        )
        async with app.uow_factory() as uow:
            await uow.agents.put(agent())
            await uow.sessions.create(session())
            await uow.memories.upsert_belief(original)
        assert (await services.index.run_slice()).indexed == 1
        run_id = await app.runs.submit("Suggest breakfast options")
        worker = app.worker_factory("local-recall-test")
        assert isinstance(worker, DurableWorker)
        assert await worker.run_once()
        outcome = await app.runs.wait_terminal(run_id)
        assert outcome.failure is None, outcome.failure
        async with app.uow_factory() as uow:
            actual = await uow.checkpoints.latest(run_id)
        assert actual is not None
        assert actual.budget_state.get("context_reader_sha256") == reader
        assert actual.recall_work is not None
        # The automatic round reserves its base and possible delta even when
        # only the base executes; unused reservations remain spent (ADR-0196).
        assert actual.recall_work.searches == (3 if path == "deliberate" else 2)
        assert actual.recall_work.rounds == (2 if path == "deliberate" else 1)
        assert (actual.recall_work.deadline_at - NOW).total_seconds() == 10
        assert len(provider.calls) >= (3 if path == "deliberate" else 2), (
            "Runtime must prepare the exact recall queries"
        )
        requests = app.executor._model_provider.requests  # type: ignore[attr-defined]
        assert original.statement in requests[-1].model_dump_json()
        if path == "deliberate":
            assert any(
                "Which breakfast fits my constraints?" in text
                for request in provider.calls
                for text in request.texts
            )
    assert lifecycle == ["closed"], "Composition owns the offline adapter lifetime"
