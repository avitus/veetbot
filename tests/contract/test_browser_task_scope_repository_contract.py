"""The same owner-policy contract runs against memory and PostgreSQL."""

from agent_core.adapters.browser.task_grants import InMemoryBrowserTaskGrantRepository
from agent_core.domain.agents import Principal
from agent_core.domain.browser_task_grants import BrowserTaskScopePolicy, parse_task_grant_scopes
from agent_core.ports.browser_task_grants import BrowserTaskGrantRepository
from tests.contract.support import principal

SCOPES = parse_task_grant_scopes("https://www.example.org/lesson")


async def assert_scope_policy_contract(repo: BrowserTaskGrantRepository, owner: Principal) -> None:
    assert await repo.get_scopes(owner) == BrowserTaskScopePolicy()
    async with repo.locked_scopes(owner, defaults=SCOPES) as policy:
        assert policy.scopes == SCOPES
        assert policy.revision == 0
        await repo.replace_scopes(owner, BrowserTaskScopePolicy(revision=1, scopes=()))
    async with repo.locked_scopes(owner, defaults=SCOPES) as policy:
        assert policy == BrowserTaskScopePolicy(revision=1, scopes=())
    for other in (
        owner.model_copy(update={"principal_id": "another-owner"}),
        owner.model_copy(update={"tenant_id": "another-tenant"}),
    ):
        assert await repo.get_scopes(other) == BrowserTaskScopePolicy()
    assert await repo.get_scopes(owner) == BrowserTaskScopePolicy(revision=1)


async def test_memory_scope_policy_contract() -> None:
    await assert_scope_policy_contract(InMemoryBrowserTaskGrantRepository(), principal())
