"""The requested hosted models resolve with their supported, priced limits."""

from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.bootstrap import build
from agent_core.config import PACKAGE_ROOT, ConfigurationError, load_settings
from agent_core.domain.messages import Capability, FakeModelScript, ScriptedTurn
from agent_core.model.registry import ProviderRegistry, StaticModelRouter
from tests.contract.support import NOW


@pytest.mark.parametrize(
    ("policy", "provider", "model", "context_window", "cached_input"),
    [
        ("flagship", "anthropic", "claude-fable-5-1", 1000000, "0.25"),
        ("astra", "openai", "gpt-6-astra", 272000, "1.00"),
        ("fable", "anthropic", "claude-fable-5-1", 1000000, "0.25"),
    ],
)
async def test_requested_models_resolve_with_verified_pricing_and_limits(
    policy: str,
    provider: str,
    model: str,
    context_window: int,
    cached_input: str,
) -> None:
    router = StaticModelRouter(
        ProviderRegistry.load(PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS),
        FixedClock(NOW),
    )

    resolved = await router.resolve(
        policy,
        tenant_id="tenant-a",
        required=frozenset({Capability.STRUCTURED_OUTPUT, Capability.STREAMING}),
    )

    assert (resolved.provider, resolved.model) == (provider, model)
    assert resolved.limits.context_window_tokens == context_window
    assert resolved.limits.max_output_tokens == 128000
    assert resolved.pricing.input_per_mtok == Decimal("10.00")
    assert resolved.pricing.cached_input_per_mtok == Decimal(cached_input)
    assert resolved.pricing.cache_write_per_mtok == Decimal("12.50")
    assert resolved.pricing.output_per_mtok == Decimal("50.00")
    assert resolved.pricing.reasoning_priced_separately is False


@pytest.mark.parametrize(
    ("model", "one_hour_write"),
    [("claude-fable-5-1", "20.00"), ("claude-opus-5", "10.00")],
)
async def test_anthropic_models_price_the_one_hour_cache_write(
    model: str, one_hour_write: str
) -> None:
    """A one-hour write is twice the base input price (ADR-0132); OpenAI has no TTL."""
    registry = ProviderRegistry.load(PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS)
    anthropic = next(
        profile for profile in registry.profiles.values() if profile.document.adapter == "anthropic"
    )
    pricing = next(entry for entry in anthropic.models if entry.model_id == model).pricing
    astra = await StaticModelRouter(registry, FixedClock(NOW)).resolve(
        "astra", tenant_id="tenant-a"
    )

    assert anthropic.document.capabilities.explicit_cache_control is True
    assert pricing.cache_write_1h_per_mtok == Decimal(one_hour_write)
    assert pricing.cache_write_1h_per_mtok == 2 * pricing.input_per_mtok
    assert astra.pricing.cache_write_1h_per_mtok is None


@pytest.mark.parametrize("chat_policy", ["astra", "flagship", "fable"])
async def test_chat_defaults_keep_sol_people_memory(tmp_path: Path, chat_policy: str) -> None:
    settings = load_settings(
        {
            "DATABASE_URL": "postgresql+asyncpg://localhost/unused",
            "DEPLOYMENT_MODE": "development",
            "AUTH_MODE": "dev",
            "SANDBOX_MECHANISM": "fake",
            "AGENT_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        }
    )
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="unused")]), FixedClock(NOW)
    )
    async with (
        build(
            settings=settings,
            storage="memory",
            model_policy=chat_policy,
            model_provider_overrides={"openai": provider, "anthropic": provider},
        ) as app,
        app.uow_factory() as uow,
    ):
        selections = await uow.process_events.list("memory.provider_extraction.selection")
        agent = await uow.agents.get_version(
            UUID(str(selections[0].payload["agent_id"])),
            str(selections[0].payload["agent_version"]),
        )

    assert len(selections) == 1
    assert agent.model_policy == chat_policy
    assert agent.version == f"1.0.0+model.{chat_policy}"
    assert selections[0].payload["chat_model_policy"] == agent.model_policy
    assert selections[0].payload["model_policy"] == "balanced"
    assert selections[0].payload["model"] == "gpt-5.6-sol"
    assert selections[0].payload["outcome"] == "activated"
    assert selections[0].payload["evidence_source"] is None
    assert selections[0].payload["reason"] == "people_default"
    assert selections[0].payload["formation_policy_version"] == "formation@11"


@pytest.mark.parametrize(
    ("memory_policy", "mode", "expected_reason"),
    [
        ("astra", "auto", "people_default"),
        ("missing-policy", "auto", "model_resolution_failed"),
        ("missing-policy", "off", "configured_off"),
    ],
)
async def test_memory_policy_overlay_preserves_availability_and_off_boundaries(
    tmp_path: Path, memory_policy: str, mode: str, expected_reason: str
) -> None:
    overlay = tmp_path / "memory" / "profiles.yaml"
    overlay.parent.mkdir()
    overlay.write_text(f"formation:\n  model_policy: {memory_policy}\n", encoding="utf-8")
    settings = load_settings(
        {
            "DATABASE_URL": "postgresql+asyncpg://localhost/unused",
            "DEPLOYMENT_MODE": "development",
            "AUTH_MODE": "dev",
            "SANDBOX_MECHANISM": "fake",
            "AGENT_CONFIG_DIR": str(tmp_path),
            "AGENT_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            "AGENT_MEMORY_PROVIDER_EXTRACTION_MODE": mode,
        }
    )
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="unused")]), FixedClock(NOW)
    )
    async with (
        build(
            settings=settings,
            storage="memory",
            model_policy="fable",
            model_provider_overrides={"openai": provider},
        ) as app,
        app.uow_factory() as uow,
    ):
        selection = (await uow.process_events.list("memory.provider_extraction.selection"))[0]
    assert selection.payload["reason"] == expected_reason
    assert selection.payload["outcome"] == (
        "disabled"
        if mode == "off"
        else "activated"
        if memory_policy == "astra"
        else "deterministic_fallback"
    )
    assert selection.payload["model_policy"] == memory_policy
    assert provider.requests == []

    if mode == "auto" and memory_policy == "missing-policy":
        from dataclasses import replace

        from agent_core.config import MemoryProviderExtractionMode

        with pytest.raises(ConfigurationError):
            async with build(
                settings=replace(
                    settings, memory_provider_extraction_mode=MemoryProviderExtractionMode.REQUIRED
                ),
                storage="memory",
                model_policy="fable",
                model_provider_overrides={"openai": provider},
            ):
                pytest.fail("required mode activated an unresolved memory model")


@pytest.mark.parametrize("distillation", [False, True])
async def test_explicit_memory_evaluation_uses_requested_astra_model(
    tmp_path: Path, distillation: bool
) -> None:
    settings = load_settings(
        {
            "DATABASE_URL": "postgresql+asyncpg://localhost/unused",
            "DEPLOYMENT_MODE": "development",
            "AUTH_MODE": "dev",
            "SANDBOX_MECHANISM": "fake",
            "AGENT_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        }
    )
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="unused")]), FixedClock(NOW)
    )
    async with (
        build(
            settings=settings,
            storage="memory",
            model_policy="astra",
            model_provider_overrides={"openai": provider},
            memory_provider_evaluation_mode=not distillation,
            memory_distillation_evaluation_mode=distillation,
        ) as app,
        app.uow_factory() as uow,
    ):
        selection = (await uow.process_events.list("memory.provider_extraction.selection"))[0]
    assert selection.payload["outcome"] == "evaluation"
    assert selection.payload["model_policy"] == "astra"
    assert selection.payload["model"] == "gpt-6-astra"
