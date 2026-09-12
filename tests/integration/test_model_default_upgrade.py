"""A deployed default changes without overwriting the previous agent version."""

from dataclasses import replace
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import DEFAULT_AGENT_ID, build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.contract.support import NOW
from tests.integration.m2_support import database_settings


def _provider() -> FakeModelProvider:
    return FakeModelProvider(FakeModelScript(turns=[ScriptedTurn(text="unused")]), FixedClock(NOW))


async def test_production_default_preserves_legacy_agent_and_activates_sol_memory(
    tmp_path: Path,
) -> None:
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    async with (
        build(
            settings=settings,
            storage="postgres",
            model_policy="balanced",
            model_provider_overrides={"openai": _provider()},
        ) as old,
        old.uow_factory() as uow,
    ):
        existing = await uow.agents.latest_version(DEFAULT_AGENT_ID)
        legacy = existing.model_copy(update={"version": "1.0.0"})
        await uow.agents.put(legacy)

    production = replace(
        settings,
        deployment_mode=DeploymentMode.PRODUCTION,
        auth_mode=AuthMode.TOKEN,
        auth_token=SecretStr("synthetic-token"),
        auth_tenant_id="tenant-a",
        auth_principal_id="owner",
        auth_scopes=PLATFORM_SCOPES,
        sandbox=SandboxMechanism.GVISOR,
        execution_service_socket=tmp_path / "execution.sock",
    )
    async with build(
        settings=production,
        storage="postgres",
        model_provider_overrides={"openai": _provider()},
    ) as current:
        session_id = await current.sessions.create()
        async with current.uow_factory() as uow:
            assert await uow.agents.get_version(DEFAULT_AGENT_ID, "1.0.0") == legacy
            latest = await uow.agents.latest_version(DEFAULT_AGENT_ID)
            assert latest.version != legacy.version
            assert latest.version.startswith("1.0.0+h")
            assert latest.model_policy == "astra"
            selections = await uow.process_events.list("memory.provider_extraction.selection")
            selected = next(
                event.payload
                for event in selections
                if event.payload["agent_version"] == latest.version
            )
            assert UUID(str(selected["agent_id"])) == DEFAULT_AGENT_ID
            assert selected["model"] == "gpt-5.6-sol"
            assert selected["model_policy"] == "balanced"
            assert selected["chat_model_policy"] == "astra"
            assert selected["outcome"] == "activated"
            assert selected["formation_policy_version"] == "formation@9"
            session = await uow.sessions.get(session_id, current.principal)
            assert session.agent_version == latest.version
