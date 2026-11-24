"""The sign-in ceremony list is 404 for a profile the caller does not own.

Guards docs/plan/browser-automation.md "Profile API contract", which says of
the profile and authentication-ceremony routes: "Cross-principal access is
404." GET /v1/browser-profiles/{id}/authentication-ceremonies answers for the
profile it names, so another principal's profile, like one that does not
exist, is not found rather than an empty list.
"""

from __future__ import annotations

from uuid import UUID

from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.contract.test_browser_profile_repository_contract import profile
from tests.integration.journey_support import api_client
from tests.integration.m2_support import memory_settings

PROFILE_ID = UUID(int=0xB0B)


async def test_ceremony_list_is_404_for_a_profile_the_caller_does_not_own() -> None:
    stranger = Principal(
        tenant_id="local", principal_id="someone-else", roles={"user"}, scopes=set(PLATFORM_SCOPES)
    )
    async with build(settings=memory_settings(), storage="memory") as app:
        async with app.uow_factory() as uow:
            await uow.browser_profiles.create(profile(profile_id=PROFILE_ID, owner=app.principal))
        path = f"/v1/browser-profiles/{PROFILE_ID}/authentication-ceremonies"
        async with api_client(app) as owner:
            assert (await owner.get(path)).status_code == 200
            assert (await owner.get(f"/v1/browser-profiles/{PROFILE_ID}")).status_code == 200
        async with api_client(app, principal=stranger) as other:
            assert (await other.get(f"/v1/browser-profiles/{PROFILE_ID}")).status_code == 404
            listed = await other.get(path)
            assert listed.status_code == 404, (listed.status_code, listed.text)
