"""Profile-bound hosted browser provider."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionConsequence,
    BrowserActionContext,
    BrowserElement,
    BrowserLease,
    BrowserObservation,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserProviderError,
    browser_origin,
    normalize_browser_origin,
)
from agent_core.domain.errors import AgentCoreError
from agent_core.domain.tools import ToolExecutionContext
from agent_core.ports.browser_sessions import BrowserSessionControlPlane

ProfileLoader = Callable[[Principal, UUID], Awaitable[BrowserProfile]]


class HostedBrowserProvider:
    name = "hosted-playwright"

    def __init__(
        self,
        *,
        principal: Principal,
        profile_id: UUID,
        allowed_origins: tuple[str, ...],
        profiles: ProfileLoader,
        sessions: BrowserSessionControlPlane,
    ) -> None:
        normalized = tuple(normalize_browser_origin(origin) for origin in allowed_origins)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("hosted browser provider requires unique origins")
        self._principal = principal.model_copy(deep=True)
        self._profile_id = profile_id
        self._allowed_origins = normalized
        self._profiles = profiles
        self._sessions = sessions
        self._lease: BrowserLease | None = None
        self._lease_scope: tuple[UUID, int] | None = None
        self._sequence = 0
        self._observation: BrowserObservation | None = None
        self._lock = asyncio.Lock()

    def allows(self, url: str) -> bool:
        try:
            return browser_origin(url) in self._allowed_origins
        except ValueError:
            return False

    async def bind_execution(self, context: ToolExecutionContext) -> None:
        if context.principal != self._principal or context.tenant_id != self._principal.tenant_id:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        async with self._lock:
            try:
                profile = await self._profiles(self._principal, self._profile_id)
            except (AgentCoreError, OSError) as exc:
                await self._close_locked()
                raise BrowserProviderError(
                    "tool.browser.profile_unavailable",
                    retryable=False,
                ) from exc
            await self._require_ready_profile(profile)
            scope = (context.run_id, context.attempt_number)
            if (
                self._lease is not None
                and self._lease_scope == scope
                and self._lease.expires_at >= context.deadline_at
            ):
                return
            await self._close_locked()
            assert profile.provider_ref is not None
            self._lease = await self._sessions.acquire(
                profile.id,
                self._principal,
                profile.provider_ref,
                run_id=context.run_id,
                attempt_number=context.attempt_number,
                deadline_at=context.deadline_at,
            )
            self._lease_scope = scope
            self._sequence = 0
            self._observation = None

    async def navigate(self, url: str) -> BrowserObservation:
        if not self.allows(url):
            raise BrowserProviderError("tool.browser.url_disallowed", retryable=False)
        async with self._lock:
            lease = self._required_lease()
            observation = await self._sessions.navigate(lease.lease_ref, url)
            self._observation = self._validated_observation(observation)
            return self._observation

    async def observe(self) -> BrowserObservation:
        async with self._lock:
            lease = self._required_lease()
            observation = await self._sessions.observe(lease.lease_ref)
            self._observation = self._validated_observation(observation)
            return self._observation

    async def act(self, action: BrowserAction) -> BrowserObservation:
        async with self._lock:
            lease = self._required_lease()
            sequence = self._sequence + 1
            observation = await self._sessions.act(
                lease.lease_ref,
                action,
                sequence=sequence,
            )
            self._sequence = sequence
            self._observation = self._validated_observation(observation)
            return self._observation

    async def action_context(self, action: BrowserAction) -> BrowserActionContext:
        async with self._lock:
            observation = self._observation
            if observation is None or observation.revision != action.expected_revision:
                raise BrowserProviderError("tool.browser.page_changed", retryable=False)
            element = next((item for item in observation.elements if item.ref == action.ref), None)
            if element is None:
                raise BrowserProviderError("tool.browser.element_not_found", retryable=False)
            return BrowserActionContext(
                origin=browser_origin(observation.url),
                role=element.role,
                name=element.name,
                consequence=_classify_consequence(action, element),
                revision=observation.revision,
                ref=element.ref,
            )

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def _require_ready_profile(self, profile: BrowserProfile) -> None:
        if (
            profile.id != self._profile_id
            or profile.tenant_id != self._principal.tenant_id
            or profile.principal_id != self._principal.principal_id
            or profile.allowed_origins != self._allowed_origins
            or profile.provider_name != "hosted-isolated"
            or profile.provider_ref is None
        ):
            await self._close_locked()
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        if profile.status is BrowserProfileStatus.AUTHENTICATION_REQUIRED:
            await self._close_locked()
            raise BrowserProviderError("tool.browser.authentication_required", retryable=False)
        if profile.status is BrowserProfileStatus.NEEDS_USER:
            await self._close_locked()
            raise BrowserProviderError("tool.browser.needs_user", retryable=False)
        if profile.status is not BrowserProfileStatus.READY:
            await self._close_locked()
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)

    def _required_lease(self) -> BrowserLease:
        if self._lease is None:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        return self._lease

    def _validated_observation(self, observation: BrowserObservation) -> BrowserObservation:
        if not self.allows(observation.url):
            raise BrowserProviderError("tool.browser.output_invalid", retryable=False)
        return observation

    async def _close_locked(self) -> None:
        if self._lease is not None:
            await self._sessions.close(self._lease.lease_ref)
        self._lease = None
        self._lease_scope = None
        self._sequence = 0
        self._observation = None


_HARD_EXCLUSION_WORDS = frozenset(
    {
        "accept",
        "agree",
        "buy",
        "checkout",
        "delete",
        "download",
        "order",
        "password",
        "pay",
        "post",
        "publish",
        "recover",
        "remove",
        "security",
        "submit",
        "upload",
    }
)
_ROUTINE_CLICK_NAMES = frozenset(
    {
        "continue",
        "done",
        "finish",
        "got it",
        "next",
        "practice",
        "review",
        "skip",
        "start",
        "try again",
    }
)


def _classify_consequence(
    action: BrowserAction,
    element: BrowserElement,
) -> BrowserActionConsequence:
    normalized_name = " ".join(element.name.lower().split())
    words = frozenset(normalized_name.replace("-", " ").split())
    if words & _HARD_EXCLUSION_WORDS:
        return BrowserActionConsequence.UNKNOWN
    if (
        action.kind.value == "click"
        and element.role in {"button", "link"}
        and normalized_name in _ROUTINE_CLICK_NAMES
    ):
        return BrowserActionConsequence.ROUTINE
    if action.kind.value == "scroll":
        return BrowserActionConsequence.ROUTINE
    return BrowserActionConsequence.UNKNOWN
