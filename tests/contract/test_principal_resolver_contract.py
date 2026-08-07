import pytest

from agent_core.adapters.identity import StaticPrincipalResolver
from agent_core.domain.errors import NotFoundError
from tests.contract.support import principal, run


async def test_principal_resolver_is_tenant_scoped() -> None:
    resolver = StaticPrincipalResolver(principal())
    assert await resolver.for_run(run()) == principal()
    with pytest.raises(NotFoundError):
        await resolver.for_run(run().model_copy(update={"tenant_id": "other"}))
