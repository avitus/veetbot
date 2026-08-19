"""Milestone 10 hard gates for provider-neutral public-web access."""

from agent_core.adapters.web.firecrawl import FirecrawlWebProvider
from agent_core.adapters.web.tavily import TavilyWebProvider
from tests.contract import test_web_provider_contract as provider_contract
from tests.unit import test_web_composition as composition_contract
from tests.unit import test_web_tools as tool_contract


async def test_provider_contract() -> None:
    for provider_name, factory in provider_contract.provider_factories():
        await provider_contract.test_web_provider_search_contract(provider_name, factory)
        await provider_contract.test_web_provider_fetch_contract(provider_name, factory)


async def test_capability_routing() -> None:
    await composition_contract.test_recommended_hybrid_registers_tavily_search_and_firecrawl_fetch()
    for provider_name, provider_type in (
        ("tavily", TavilyWebProvider),
        ("firecrawl", FirecrawlWebProvider),
    ):
        await composition_contract.test_one_provider_can_serve_both_web_capabilities(
            provider_name, provider_type
        )


async def test_default_off_registration() -> None:
    await composition_contract.test_disabled_web_capabilities_are_not_registered()


async def test_context_advertisement() -> None:
    await composition_contract.test_web_research_plan_omits_unusable_skill_loader()


async def test_invocation_trust() -> None:
    await composition_contract.test_web_search_runs_through_policy_and_persists_untrusted_result()


async def test_failure_and_secret_boundary() -> None:
    for provider_name, factory in provider_contract.provider_factories():
        await provider_contract.test_web_provider_never_exposes_upstream_error_text(
            provider_name, factory
        )
        await provider_contract.test_web_provider_rejected_credential_is_a_stable_auth_failure(
            provider_name, factory
        )
        await provider_contract.test_web_provider_normalizes_permanent_rejections(
            provider_name, factory
        )
        await provider_contract.test_web_provider_normalizes_invalid_success_output(
            provider_name, factory
        )
        await provider_contract.test_web_provider_normalizes_transport_failures(
            provider_name, factory
        )
        await provider_contract.test_web_provider_missing_credential_is_a_stable_auth_failure(
            provider_name
        )


async def test_fetch_confinement_and_bounds() -> None:
    await (
        tool_contract.test_fetch_rejects_non_public_or_non_https_destinations_before_provider_call()
    )
    await tool_contract.test_fetch_bounds_multibyte_content_before_building_both_output_shapes()
    for provider_name, factory in provider_contract.provider_factories():
        await provider_contract.test_web_provider_bounds_oversized_responses(provider_name, factory)
