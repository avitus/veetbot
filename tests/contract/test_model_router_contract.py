import pytest

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


def _router() -> StaticModelRouter:
    from agent_core.adapters.determinism import FixedClock
    from agent_core.config import PACKAGE_ROOT

    return StaticModelRouter(
        ProviderRegistry.load(PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS),
        FixedClock(NOW),
    )


async def test_model_router_refuses_an_undeclared_policy_or_an_unmet_capability() -> None:
    from agent_core.config import ConfigurationError
    from agent_core.domain.messages import Capability

    router = _router()
    with pytest.raises(ConfigurationError, match="not declared"):
        await router.resolve("no-such-policy", tenant_id="tenant-a")
    with pytest.raises(ConfigurationError, match="lacks required capabilities"):
        await router.resolve(
            "balanced", tenant_id="tenant-a", required=frozenset({Capability.AUDIO})
        )


@pytest.mark.parametrize(
    "drift",
    [{"model": "a-model-no-profile-declares"}, {"registry_version": "sha256:retired"}],
    ids=["model_withdrawn", "registry_changed"],
)
async def test_a_pin_the_registry_no_longer_honours_is_unavailable_not_rerouted(
    drift: dict[str, str],
) -> None:
    from agent_core.domain.errors import ProviderPinUnavailableError

    router = _router()
    resolved = await router.resolve("balanced", tenant_id="tenant-a")
    stale = router.pin(RUN_ID, resolved).model_copy(update=drift)

    with pytest.raises(ProviderPinUnavailableError):
        await router.resolve_pinned(stale)
