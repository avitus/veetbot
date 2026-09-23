"""ADR-0119: memory formation sends its evaluated effort and follows the owner's choice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.config import PACKAGE_ROOT, ConfigurationError, Settings, load_settings
from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import FakeModelScript, ReasoningEffort, ScriptedTurn
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.contract.support import NOW

SOL_ARTIFACT = (
    PACKAGE_ROOT / "memory/release_evidence/openai-balanced-gpt-5.6-sol-default-formation9.json"
)
OWNER = Principal(
    tenant_id="local", principal_id="local-user", roles={"user"}, scopes=set(PLATFORM_SCOPES)
)


def _settings(tmp_path: Path, **values: str) -> Settings:
    return load_settings(
        {
            "DATABASE_URL": "postgresql+asyncpg://localhost/unused",
            "DEPLOYMENT_MODE": "development",
            "AUTH_MODE": "dev",
            "SANDBOX_MECHANISM": "fake",
            "AGENT_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            **values,
        }
    )


def _provider() -> FakeModelProvider:
    return FakeModelProvider(FakeModelScript(turns=[ScriptedTurn(text="{}")]), FixedClock(NOW))


async def _form(composition: Composition, text: str) -> None:
    session_id = await composition.sessions.create()
    async with composition.uow_factory() as uow:
        await uow.events.append(
            NewEvent(
                session_id=session_id,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=composition.principal.principal_id,
                payload={"content": text},
            )
        )
    await composition.memory.run(trigger="evaluation", scope="evaluation", session_id=session_id)


async def _selection(composition: Composition) -> dict[str, Any]:
    async with composition.uow_factory() as uow:
        events = await uow.process_events.list("memory.provider_extraction.selection")
    assert len(events) == 1
    return dict(events[0].payload)


async def test_an_evaluation_sends_the_effort_it_evaluates(tmp_path: Path) -> None:
    provider = _provider()
    async with build(
        settings=_settings(tmp_path),
        storage="memory",
        principal=OWNER,
        model_policy="astra",
        memory_distillation_evaluation_mode=True,
        memory_reasoning_effort=ReasoningEffort.MEDIUM,
        model_provider_overrides={"openai": provider},
    ) as composition:
        await _form(composition, "I started learning to sail on weekends.")
        selection = await _selection(composition)

    assert selection["reasoning_effort"] == "medium"
    assert provider.requests
    assert {request.reasoning_effort for request in provider.requests} == {ReasoningEffort.MEDIUM}


async def test_the_profile_default_effort_reaches_every_formation_request(tmp_path: Path) -> None:
    overlay = tmp_path / "config" / "memory" / "profiles.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        "formation:\n  model_policy: astra\n  reasoning_effort: medium\n", encoding="utf-8"
    )
    provider = _provider()
    async with build(
        settings=_settings(tmp_path, AGENT_CONFIG_DIR=str(tmp_path / "config")),
        storage="memory",
        principal=OWNER,
        model_policy="astra",
        model_provider_overrides={"openai": provider},
    ) as composition:
        await _form(composition, "I started learning to sail on weekends.")
        selection = await _selection(composition)

    assert (selection["model_policy"], selection["reasoning_effort"]) == ("astra", "medium")
    assert {request.reasoning_effort for request in provider.requests} == {ReasoningEffort.MEDIUM}


async def test_an_effort_the_memory_model_does_not_accept_refuses_startup(tmp_path: Path) -> None:
    # The deterministic development model reasons natively at no effort.
    with pytest.raises(ConfigurationError, match="does not accept reasoning effort"):
        async with build(
            settings=_settings(tmp_path),
            storage="memory",
            principal=OWNER,
            memory_distillation_evaluation_mode=True,
            memory_reasoning_effort=ReasoningEffort.MEDIUM,
        ):
            pytest.fail("an effort the model cannot take was accepted")


async def test_the_owner_can_move_formation_to_an_evaluated_alternative(tmp_path: Path) -> None:
    alternative = tmp_path / "astra-medium-formation9.json"
    artifact = json.loads(SOL_ARTIFACT.read_text(encoding="utf-8"))
    artifact.update(
        schema_version=8, model_policy="astra", model="gpt-6-astra", reasoning_effort="medium"
    )
    alternative.write_text(json.dumps(artifact), encoding="utf-8")
    provider = _provider()
    async with build(
        settings=_settings(tmp_path, AGENT_MEMORY_PROVIDER_EXTRACTION_EVIDENCE=str(alternative)),
        storage="memory",
        principal=OWNER,
        model_policy="astra",
        model_provider_overrides={"openai": provider},
    ) as composition:
        app = create_app(
            composition.services,
            composition.settings,
            composition.principal,
            composition.new_request_id,
            composition.readiness_probe,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
            offered = (await client.get("/v1/settings/models")).json()["memory_options"]
            saved = await client.put(
                "/v1/settings/models",
                json={
                    "expected_version": 0,
                    "chat": {"model_policy": "astra", "reasoning_effort": "high"},
                    "memory": {"model_policy": "astra", "reasoning_effort": "medium"},
                },
            )
        await _form(composition, "I started learning to sail on weekends.")
        async with composition.uow_factory() as uow:
            runs = await uow.memories.list_consolidations(composition.principal, limit=5)

    assert [(option["model_policy"], option["reasoning_effort"]) for option in offered] == [
        ("balanced", None),
        ("astra", "medium"),
    ]
    assert saved.status_code == 200, saved.text
    assert {(request.model_policy, request.reasoning_effort) for request in provider.requests} == {
        ("astra", ReasoningEffort.MEDIUM)
    }
    assert runs[0].model.endswith("@astra:medium")
