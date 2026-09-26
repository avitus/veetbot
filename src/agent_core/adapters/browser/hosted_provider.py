"""Profile-bound hosted browser provider."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    MAXIMUM_BROWSER_LEASE_LIFETIME_SECONDS,
    MAXIMUM_BROWSER_LEASE_SECONDS,
    BrowserAction,
    BrowserActionConsequence,
    BrowserActionContext,
    BrowserActionKind,
    BrowserElement,
    BrowserLease,
    BrowserObservation,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserProviderError,
    BrowserRunState,
    browser_origin,
    normalize_browser_origin,
)
from agent_core.domain.browser_classification import classify_browser_action
from agent_core.domain.errors import AgentCoreError
from agent_core.domain.tools import ToolExecutionContext
from agent_core.ports.browser_sessions import BrowserSessionControlPlane

ProfileLoader = Callable[[Principal, UUID], Awaitable[BrowserProfile]]
ProfileSelector = Callable[[ToolExecutionContext], Awaitable[UUID]]
RunStateReader = Callable[[UUID], Awaitable[BrowserRunState]]

logger = logging.getLogger(__name__)

# Upkeep renews a lease this close to expiry (ADR-0127).
_RENEWAL_WINDOW = timedelta(minutes=5)
_RENEWABLE_RUN_STATES = frozenset(
    {BrowserRunState.RUNNING, BrowserRunState.RESUMING, BrowserRunState.AWAITING_APPROVAL}
)
# Failures that mean the service no longer honours, or cannot be asked about,
# the lease itself rather than refusing one request.
_LEASE_FAILURES = frozenset(
    {"tool.browser.profile_unavailable", "tool.browser.provider_unavailable"}
)
# Refusals the runtime gives before it dispatches an action; the lease and its
# action sequence are unchanged.
_ACTION_REFUSALS = frozenset(
    {
        "tool.browser.page_changed",
        "tool.browser.element_not_found",
        "tool.browser.action_not_allowed",
        "tool.browser.url_disallowed",
    }
)


def _run_attempt_horizon(context: ToolExecutionContext, now: datetime) -> datetime:
    """The deadline a lease owned by this call's run attempt may request.

    `context.deadline_at` bounds only the current tool call; a lease bounded by
    it would expire before the next call and replace the page the run opened.
    """

    horizon = now + timedelta(seconds=MAXIMUM_BROWSER_LEASE_SECONDS)
    if context.run_deadline_at is not None:
        horizon = min(horizon, context.run_deadline_at)
    return max(horizon, context.deadline_at)


@dataclass
class _SessionBinding:
    profile_id: UUID
    provider: HostedBrowserProvider
    deadline_at: datetime


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
        now: Callable[[], datetime],
        run_state: RunStateReader | None = None,
    ) -> None:
        normalized = tuple(normalize_browser_origin(origin) for origin in allowed_origins)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("hosted browser provider requires unique origins")
        self._principal = principal.model_copy(deep=True)
        self._profile_id = profile_id
        self._allowed_origins = normalized
        self._profiles = profiles
        self._sessions = sessions
        self._now = now
        self._run_state = run_state
        self._lease: BrowserLease | None = None
        self._lease_scope: tuple[UUID, int] | None = None
        self._lease_acquired_at: datetime | None = None
        self._run_deadline_at: datetime | None = None
        self._renewal_exhausted = False
        self._sequence = 0
        self._observation: BrowserObservation | None = None
        # Leases this provider gave up on but has not yet closed on the service.
        self._unclosed: list[str] = []
        self._lock = asyncio.Lock()

    @property
    def lease_run_id(self) -> UUID | None:
        return None if self._lease_scope is None else self._lease_scope[0]

    @property
    def holds_lease(self) -> bool:
        return self._lease is not None or bool(self._unclosed)

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
                await self._close_locked(strict=False)
                raise BrowserProviderError(
                    "tool.browser.profile_unavailable",
                    retryable=False,
                ) from exc
            await self._require_ready_profile(profile)
            scope = (context.run_id, context.attempt_number)
            # One lease serves every call of a run attempt while it outlives the
            # call; an expired lease is replaced, and renewal is the upkeep's job.
            if (
                self._lease is not None
                and self._lease_scope == scope
                and self._lease.expires_at >= context.deadline_at
            ):
                return
            await self._refuse_while_the_holder_needs_the_page(context)
            # Close the previous lease, and any given up earlier, before a new
            # acquisition, so the next call starts from a fresh page.
            await self._close_locked()
            assert profile.provider_ref is not None
            now = self._now()
            self._lease = await self._sessions.acquire(
                profile.id,
                self._principal,
                profile.provider_ref,
                run_id=context.run_id,
                attempt_number=context.attempt_number,
                deadline_at=_run_attempt_horizon(context, now),
            )
            self._lease_scope = scope
            self._lease_acquired_at = now
            self._run_deadline_at = context.run_deadline_at
            # A run resumed in another process reattaches to its own live lease
            # and continues the action sequence its earlier worker left.
            self._sequence = self._lease.sequence

    async def navigate(self, url: str) -> BrowserObservation:
        if not self.allows(url):
            raise BrowserProviderError("tool.browser.url_disallowed", retryable=False)
        async with self._lock:
            lease = self._required_lease()
            try:
                observation = await self._sessions.navigate(lease.lease_ref, url)
            except BrowserProviderError as error:
                if error.reason_code in _LEASE_FAILURES:
                    await self._close_locked(strict=False)
                raise
            except Exception:
                await self._close_locked(strict=False)
                raise
            self._observation = self._validated_observation(observation)
            return self._observation

    async def observe(self) -> BrowserObservation:
        async with self._lock:
            lease = self._required_lease()
            try:
                observation = await self._sessions.observe(lease.lease_ref)
            except BrowserProviderError as error:
                if error.reason_code in _LEASE_FAILURES:
                    await self._close_locked(strict=False)
                raise
            except Exception:
                await self._close_locked(strict=False)
                raise
            self._observation = self._validated_observation(observation)
            return self._observation

    async def act(self, action: BrowserAction) -> BrowserObservation:
        async with self._lock:
            lease = self._required_lease()
            sequence = self._sequence + 1
            try:
                observation = await self._sessions.act(
                    lease.lease_ref,
                    action,
                    sequence=sequence,
                )
            except BrowserProviderError as error:
                if error.reason_code in _ACTION_REFUSALS:
                    raise
                # Without a definite refusal the action may have landed and the
                # service's sequence moved on: give the lease up, never retry.
                await self._close_locked(strict=False)
                if error.reason_code == "tool.browser.profile_unavailable":
                    raise
                raise BrowserProviderError(
                    "tool.browser.outcome_unknown",
                    retryable=False,
                ) from error
            except asyncio.CancelledError:
                self._abandon_locked()
                raise
            except Exception as exc:
                await self._close_locked(strict=False)
                raise BrowserProviderError(
                    "tool.browser.outcome_unknown",
                    retryable=False,
                ) from exc
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

    async def release_run(self, run_id: UUID) -> None:
        """Close, and so seal, the lease an ended run held; leave any other run's."""

        async with self._lock:
            if self._lease_scope is not None and self._lease_scope[0] == run_id:
                self._abandon_locked()
            await self._settle_locked(strict=True)

    async def maintain_leases(self) -> None:
        """Release or renew the held lease as its run requires (ADR-0127).

        An ended run's lease is closed. A lease whose run is running, queued to
        resume, or parked on its own approval is renewed within five minutes of
        expiry, in steps of at most fifteen minutes, never past an hour or the
        run's own deadline.
        """

        async with self._lock:
            await self._settle_locked(strict=False)
            lease, scope = self._lease, self._lease_scope
            if lease is None or scope is None or self._lease_acquired_at is None:
                return
            now = self._now()
            if lease.expires_at <= now:
                # The service discards an expired lease itself, unsealed.
                self._forget_locked()
                return
            if self._run_state is None:
                return
            state = await self._run_state(scope[0])
            if state is BrowserRunState.ENDED:
                await self._close_locked(strict=False)
                return
            if (
                state not in _RENEWABLE_RUN_STATES
                or self._renewal_exhausted
                or lease.expires_at - now > _RENEWAL_WINDOW
            ):
                return
            target = min(
                now + timedelta(seconds=MAXIMUM_BROWSER_LEASE_SECONDS),
                self._lease_acquired_at + timedelta(seconds=MAXIMUM_BROWSER_LEASE_LIFETIME_SECONDS),
            )
            if self._run_deadline_at is not None:
                target = min(target, self._run_deadline_at)
            if target <= lease.expires_at:
                return
            try:
                renewed = await self._sessions.renew(lease.lease_ref, deadline_at=target)
            except BrowserProviderError as error:
                if error.reason_code == "tool.browser.profile_unavailable":
                    # Expired or revoked on the service, which already closed it.
                    self._forget_locked()
                    return
                raise
            self._renewal_exhausted = renewed.expires_at <= lease.expires_at
            self._lease = renewed

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
            await self._close_locked(strict=False)
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        if profile.status is BrowserProfileStatus.AUTHENTICATION_REQUIRED:
            await self._close_locked(strict=False)
            raise BrowserProviderError("tool.browser.authentication_required", retryable=False)
        if profile.status is BrowserProfileStatus.NEEDS_USER:
            await self._close_locked(strict=False)
            raise BrowserProviderError("tool.browser.needs_user", retryable=False)
        if profile.status is not BrowserProfileStatus.READY:
            await self._close_locked(strict=False)
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)

    async def _refuse_while_the_holder_needs_the_page(
        self,
        context: ToolExecutionContext,
    ) -> None:
        """Keep another run's live lease from a newcomer while that run needs it."""

        lease, scope = self._lease, self._lease_scope
        if (
            lease is None
            or scope is None
            or scope[0] == context.run_id
            or self._run_state is None
            or lease.expires_at <= self._now()
        ):
            return
        if await self._run_state(scope[0]) is not BrowserRunState.ENDED:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)

    def _required_lease(self) -> BrowserLease:
        if self._lease is None:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        return self._lease

    def _validated_observation(self, observation: BrowserObservation) -> BrowserObservation:
        if not self.allows(observation.url):
            raise BrowserProviderError("tool.browser.output_invalid", retryable=False)
        return observation

    def _forget_locked(self) -> None:
        self._lease = None
        self._lease_scope = None
        self._lease_acquired_at = None
        self._run_deadline_at = None
        self._renewal_exhausted = False
        self._sequence = 0
        self._observation = None

    def _abandon_locked(self) -> None:
        """Stop using the lease now and queue it for closing; no I/O."""

        lease = self._lease
        self._forget_locked()
        if lease is not None and lease.expires_at > self._now():
            self._unclosed.append(lease.lease_ref)

    async def _settle_locked(self, *, strict: bool) -> None:
        while self._unclosed:
            try:
                await self._sessions.close(self._unclosed[0])
            except Exception:
                if strict:
                    raise
                return
            self._unclosed.pop(0)

    async def _close_locked(self, *, strict: bool = True) -> None:
        self._abandon_locked()
        await self._settle_locked(strict=strict)


class SessionBoundHostedBrowserProvider:
    """Resolve a trusted profile from session metadata before each tool invocation."""

    name = "hosted-session-bound-playwright"

    def __init__(
        self,
        *,
        principal: Principal,
        profiles: ProfileLoader,
        profile_selector: ProfileSelector,
        sessions: BrowserSessionControlPlane,
        now: Callable[[], datetime],
        run_state: RunStateReader | None = None,
    ) -> None:
        self._principal = principal.model_copy(deep=True)
        self._profiles = profiles
        self._profile_selector = profile_selector
        self._sessions = sessions
        self._now = now
        self._run_state = run_state
        self._bindings: dict[UUID, _SessionBinding] = {}
        self._current: ContextVar[HostedBrowserProvider | None] = ContextVar(
            "session_bound_hosted_browser_provider",
            default=None,
        )
        self._lock = asyncio.Lock()

    async def bind_execution(self, context: ToolExecutionContext) -> None:
        if context.principal != self._principal or context.tenant_id != self._principal.tenant_id:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        try:
            profile_id = await self._profile_selector(context)
        except (AgentCoreError, OSError, ValueError) as exc:
            raise BrowserProviderError(
                "tool.browser.profile_unavailable",
                retryable=False,
            ) from exc

        providers_to_close: list[HostedBrowserProvider] = []
        others: list[_SessionBinding] = []
        async with self._lock:
            now = self._now()
            for session_id, other in tuple(self._bindings.items()):
                if session_id == context.session_id:
                    continue
                if other.deadline_at <= now and not other.provider.holds_lease:
                    del self._bindings[session_id]
                else:
                    others.append(other)
            binding = self._bindings.get(context.session_id)
            if binding is None or binding.profile_id != profile_id:
                if binding is not None:
                    providers_to_close.append(binding.provider)
                try:
                    profile = await self._profiles(self._principal, profile_id)
                except (AgentCoreError, OSError) as exc:
                    raise BrowserProviderError(
                        "tool.browser.profile_unavailable",
                        retryable=False,
                    ) from exc
                binding = _SessionBinding(
                    profile_id=profile_id,
                    provider=HostedBrowserProvider(
                        principal=self._principal,
                        profile_id=profile_id,
                        allowed_origins=profile.allowed_origins,
                        profiles=self._profiles,
                        sessions=self._sessions,
                        now=self._now,
                        run_state=self._run_state,
                    ),
                    deadline_at=_run_attempt_horizon(context, now),
                )
                self._bindings[context.session_id] = binding
            else:
                binding.deadline_at = max(binding.deadline_at, _run_attempt_horizon(context, now))

        for provider in providers_to_close:
            await provider.close()
        # A lease another session's ended run still holds would refuse this
        # session the profile, or a later login, until it expired.
        for other in others:
            await self._release_if_ended(other, now)
        await binding.provider.bind_execution(context)
        self._current.set(binding.provider)

    def allows(self, url: str) -> bool:
        provider = self._current.get()
        return provider is not None and provider.allows(url)

    async def navigate(self, url: str) -> BrowserObservation:
        return await self._required_provider().navigate(url)

    async def observe(self) -> BrowserObservation:
        return await self._required_provider().observe()

    async def act(self, action: BrowserAction) -> BrowserObservation:
        return await self._required_provider().act(action)

    async def action_context(self, action: BrowserAction) -> BrowserActionContext:
        return await self._required_provider().action_context(action)

    async def release_run(self, run_id: UUID) -> None:
        async with self._lock:
            providers = [binding.provider for binding in self._bindings.values()]
        failure: Exception | None = None
        for provider in providers:
            try:
                await provider.release_run(run_id)
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    async def maintain_leases(self) -> None:
        """Release or renew every session's lease as its run requires."""

        async with self._lock:
            now = self._now()
            bindings = tuple(self._bindings.items())
        for _session_id, binding in bindings:
            try:
                await binding.provider.maintain_leases()
            except Exception as exc:
                logger.warning(
                    "browser_lease_upkeep_failed",
                    extra={"error_class": type(exc).__name__},
                )
        async with self._lock:
            for session_id, binding in bindings:
                if (
                    self._bindings.get(session_id) is binding
                    and binding.deadline_at <= now
                    and not binding.provider.holds_lease
                ):
                    del self._bindings[session_id]

    async def close(self) -> None:
        async with self._lock:
            providers = [binding.provider for binding in self._bindings.values()]
            self._bindings.clear()
            self._current.set(None)
        for provider in providers:
            await provider.close()

    async def _release_if_ended(self, binding: _SessionBinding, now: datetime) -> None:
        run_id = binding.provider.lease_run_id
        if run_id is None:
            return
        try:
            if self._run_state is None:
                ended = binding.deadline_at <= now
            else:
                ended = await self._run_state(run_id) is BrowserRunState.ENDED
            if ended:
                await binding.provider.release_run(run_id)
        except Exception as exc:
            logger.warning(
                "browser_lease_release_failed",
                extra={"error_class": type(exc).__name__},
            )

    def _required_provider(self) -> HostedBrowserProvider:
        provider = self._current.get()
        if provider is None:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        return provider


def _classify_consequence(
    action: BrowserAction,
    element: BrowserElement,
) -> BrowserActionConsequence:
    """The shared classifier over what the worker's cached observation shows."""

    return classify_browser_action(
        kind=action.kind,
        role=element.role,
        labels=(element.name,),
        facts=None,
        option_texts=(action.value,)
        if action.kind is BrowserActionKind.SELECT and action.value is not None
        else (),
    )
