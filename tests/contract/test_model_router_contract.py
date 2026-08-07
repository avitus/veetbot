from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
from agent_core.model.registry import ProviderRegistry, StaticModelRouter
from tests.contract.support import NOW, RUN_ID


async def test_model_router_pins_and_reconstructs_exact_resolution() -> None:
    from agent_core.adapters.determinism import FixedClock
    from agent_core.config import PACKAGE_ROOT

    router = StaticModelRouter(
        ProviderRegistry.load(PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS),
        FixedClock(NOW),
    )
    resolved = await router.resolve("balanced", tenant_id="tenant-a")
    pin = router.pin(RUN_ID, resolved)
    reconstructed = await router.resolve_pinned(pin)
    assert reconstructed.provider == resolved.provider
    assert reconstructed.model == resolved.model
    assert reconstructed.capabilities == resolved.capabilities
    assert reconstructed.limits == resolved.limits
    assert reconstructed.pricing == resolved.pricing
