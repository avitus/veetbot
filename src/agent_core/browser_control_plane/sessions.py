"""Exclusive session and direct-authentication service for hosted profiles."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from agent_core.browser_control_plane.models import (
    ProfileMaterialIdentity,
    ProfileMaterialMetadata,
)
from agent_core.browser_control_plane.ports import EncryptedProfileStore
from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAction,
    BrowserAuthenticationStatus,
    BrowserAuthenticationView,
    BrowserInteractiveEvent,
    BrowserLease,
    BrowserObservation,
    BrowserProviderError,
    browser_origin,
    require_service_origin,
)
from agent_core.domain.errors import ConflictError

MAXIMUM_LEASE_SECONDS = 15 * 60
AUTHENTICATION_CEREMONY_SECONDS = 5 * 60


@dataclass(frozen=True, slots=True)
class _LeaseScope:
    profile_id: UUID
    tenant_id: str
    principal_id: str
    provider_ref: str
    run_id: UUID
    attempt_number: int
    expires_at: datetime


@dataclass(slots=True)
class _LeaseState:
    scope: _LeaseScope
    identity: ProfileMaterialIdentity
    runtime: BrowserSessionRuntime
    sequence: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


@dataclass(slots=True)
class _CeremonyState:
    id: UUID
    profile_id: UUID
    tenant_id: str
    principal_id: str
    identity: ProfileMaterialIdentity
    expires_at: datetime
    runtime: BrowserSessionRuntime
    status: BrowserAuthenticationStatus
    capability_digest: bytes


@dataclass(frozen=True, slots=True)
class _TerminalCeremonyState:
    id: UUID
    profile_id: UUID
    tenant_id: str
    principal_id: str
    expires_at: datetime
    status: BrowserAuthenticationStatus


class BrowserSessionRuntime(Protocol):
    async def start(
        self,
        material: bytes,
        allowed_origins: tuple[str, ...],
        *,
        interactive: bool,
    ) -> None: ...

    async def navigate(self, url: str) -> BrowserObservation: ...

    async def observe(self) -> BrowserObservation: ...

    async def act(self, action: BrowserAction) -> BrowserObservation: ...

    async def storage_state(self) -> bytes: ...

    async def authentication_status(self) -> BrowserAuthenticationStatus: ...

    async def interactive_frame(self) -> bytes: ...

    async def interactive_event(self, event: BrowserInteractiveEvent) -> None: ...

    async def close(self) -> None: ...


class HostedProfileSessionService:
    def __init__(
        self,
        store: EncryptedProfileStore,
        *,
        runtime_factory: Callable[[str], BrowserSessionRuntime],
        now: Callable[[], datetime],
        process_secret: bytes,
        ceremony_base_url: str,
    ) -> None:
        normalized_ceremony_origin = require_service_origin(
            ceremony_base_url,
            message="authentication ceremony requires one HTTPS origin",
        )
        if len(process_secret) < 32:
            raise ValueError("profile session process secret is too short")
        self._store = store
        self._runtime_factory = runtime_factory
        self._now = now
        self._process_secret = bytes(process_secret)
        self._ceremony_base_url = normalized_ceremony_origin
        self._leases: dict[bytes, _LeaseState] = {}
        self._ceremonies: dict[UUID, _CeremonyState] = {}
        self._terminal_ceremonies: dict[UUID, _TerminalCeremonyState] = {}
        self._ceremony_counter = 0
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
        *,
        run_id: UUID,
        attempt_number: int,
        deadline_at: datetime,
    ) -> BrowserLease:
        if attempt_number < 1:
            raise ConflictError("profile lease attempt is invalid")
        now = self._now()
        expires_at = min(deadline_at, now + timedelta(seconds=MAXIMUM_LEASE_SECONDS))
        if expires_at <= now:
            raise ConflictError("profile lease deadline has elapsed")
        metadata = await self._owned_metadata(profile_id, principal, provider_ref)
        if metadata.revoked:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        scope = _LeaseScope(
            profile_id=profile_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            provider_ref=provider_ref,
            run_id=run_id,
            attempt_number=attempt_number,
            expires_at=expires_at,
        )
        lease_ref = self._lease_ref(scope)
        async with self._lock:
            await self._expire_locked()
            existing = self._lease_for_profile(profile_id)
            if existing is not None:
                if existing.scope != scope:
                    raise ConflictError("browser profile already has an active lease")
                return BrowserLease(lease_ref=lease_ref, expires_at=expires_at)
            if self._active_ceremony_for_profile(profile_id) is not None:
                raise ConflictError("browser profile has an active authentication ceremony")
            identity = metadata.identity()
            material = await self._store.load(identity)
            runtime = self._runtime_factory(principal.tenant_id)
            try:
                await runtime.start(material, metadata.allowed_origins, interactive=False)
            except Exception:
                await runtime.close()
                raise
            self._leases[self._lookup_digest(lease_ref)] = _LeaseState(
                scope=scope,
                identity=identity,
                runtime=runtime,
            )
        return BrowserLease(lease_ref=lease_ref, expires_at=expires_at)

    async def navigate(self, lease_ref: str, url: str) -> BrowserObservation:
        async with self._lock:
            state = await self._require_lease_locked(lease_ref)
            if browser_origin(url) not in state.identity.allowed_origins:
                raise BrowserProviderError("tool.browser.url_disallowed", retryable=False)
        async with state.lock:
            if state.closed:
                raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
            return await state.runtime.navigate(url)

    async def observe(self, lease_ref: str) -> BrowserObservation:
        async with self._lock:
            state = await self._require_lease_locked(lease_ref)
        async with state.lock:
            if state.closed:
                raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
            return await state.runtime.observe()

    async def act(
        self,
        lease_ref: str,
        action: BrowserAction,
        *,
        sequence: int,
    ) -> BrowserObservation:
        async with self._lock:
            state = await self._require_lease_locked(lease_ref)
        async with state.lock:
            if state.closed:
                raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
            if sequence != state.sequence + 1:
                raise ConflictError("browser lease action sequence is invalid")
            try:
                observation = await state.runtime.act(action)
            except BrowserProviderError:
                raise
            except Exception as exc:
                raise BrowserProviderError(
                    "tool.browser.outcome_unknown",
                    retryable=False,
                ) from exc
            state.sequence = sequence
            return observation

    async def close(self, lease_ref: str) -> None:
        async with self._lock:
            key, state = self._find_lease(lease_ref)
            if state is None or key is None:
                return
            self._leases.pop(key)
        async with state.lock:
            try:
                material = await state.runtime.storage_state()
                await self._store.write(state.identity, material)
            finally:
                with suppress(Exception):
                    await state.runtime.close()
                state.closed = True

    async def invalidate_profile(self, profile_id: UUID) -> None:
        leases: list[_LeaseState] = []
        async with self._lock:
            for key, lease_state in tuple(self._leases.items()):
                if lease_state.scope.profile_id == profile_id:
                    self._leases.pop(key)
                    leases.append(lease_state)
            for _ceremony_id, ceremony_state in tuple(self._ceremonies.items()):
                if (
                    ceremony_state.profile_id == profile_id
                    and ceremony_state.status not in _TERMINAL_AUTH_STATUSES
                ):
                    ceremony_state.status = BrowserAuthenticationStatus.CANCELLED
                    with suppress(Exception):
                        await ceremony_state.runtime.close()
                    await self._finish_ceremony_locked(ceremony_state)
        for lease_state in leases:
            async with lease_state.lock:
                with suppress(Exception):
                    await lease_state.runtime.close()
                lease_state.closed = True

    async def begin_authentication(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
        *,
        login_url: str,
    ) -> BrowserAuthenticationView:
        metadata = await self._owned_metadata(profile_id, principal, provider_ref)
        if metadata.revoked:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        if browser_origin(login_url) not in metadata.allowed_origins:
            raise BrowserProviderError("tool.browser.url_disallowed", retryable=False)
        async with self._lock:
            await self._expire_locked()
            if self._lease_for_profile(profile_id) is not None:
                raise ConflictError("browser profile already has an active lease")
            if self._active_ceremony_for_profile(profile_id) is not None:
                raise ConflictError("browser profile already has an authentication ceremony")
            self._ceremony_counter += 1
            capability = self._ceremony_capability(profile_id, self._ceremony_counter)
            ceremony_id = UUID(bytes=self._mac(b"ceremony-id:" + capability.encode())[:16])
            expires_at = self._now() + timedelta(seconds=AUTHENTICATION_CEREMONY_SECONDS)
            identity = metadata.identity()
            runtime = self._runtime_factory(principal.tenant_id)
            try:
                await runtime.start(
                    await self._store.load(identity),
                    metadata.allowed_origins,
                    interactive=True,
                )
                await runtime.navigate(login_url)
            except Exception:
                await runtime.close()
                raise
            self._ceremonies[ceremony_id] = _CeremonyState(
                id=ceremony_id,
                profile_id=profile_id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                identity=identity,
                expires_at=expires_at,
                runtime=runtime,
                status=BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
                capability_digest=self._lookup_digest("ceremony:" + capability),
            )
            return BrowserAuthenticationView(
                id=ceremony_id,
                profile_id=profile_id,
                status=BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
                expires_at=expires_at,
                launch_url=(
                    f"{self._ceremony_base_url}/authentication/{ceremony_id}"
                    f"#capability={capability}"
                ),
            )

    async def authentication_status(
        self,
        ceremony_id: UUID,
        principal: Principal,
    ) -> BrowserAuthenticationView:
        async with self._lock:
            await self._expire_locked()
            terminal = self._owned_terminal_ceremony(ceremony_id, principal)
            if terminal is not None:
                return _terminal_ceremony_view(terminal)
            state = self._owned_ceremony(ceremony_id, principal)
            return _ceremony_view(state)

    async def refresh_authentication(
        self,
        ceremony_id: UUID,
        principal: Principal,
    ) -> BrowserAuthenticationView:
        async with self._lock:
            await self._expire_locked()
            terminal = self._owned_terminal_ceremony(ceremony_id, principal)
            if terminal is not None:
                return _terminal_ceremony_view(terminal)
            state = self._owned_ceremony(ceremony_id, principal)
            if state.status in _TERMINAL_AUTH_STATUSES:
                return _ceremony_view(state)
            status = await state.runtime.authentication_status()
            state.status = status
            if status is BrowserAuthenticationStatus.READY:
                try:
                    await self._store.write(
                        state.identity,
                        await state.runtime.storage_state(),
                    )
                finally:
                    with suppress(Exception):
                        await state.runtime.close()
                return await self._finish_ceremony_locked(state)
            return _ceremony_view(state)

    async def cancel_authentication(
        self,
        ceremony_id: UUID,
        principal: Principal,
    ) -> BrowserAuthenticationView:
        async with self._lock:
            await self._expire_locked()
            terminal = self._owned_terminal_ceremony(ceremony_id, principal)
            if terminal is not None:
                return _terminal_ceremony_view(terminal)
            state = self._owned_ceremony(ceremony_id, principal)
            if state.status in _TERMINAL_AUTH_STATUSES:
                return _ceremony_view(state)
            state.status = BrowserAuthenticationStatus.CANCELLED
            with suppress(Exception):
                await state.runtime.close()
            return await self._finish_ceremony_locked(state)

    async def authenticate_surface(self, ceremony_id: UUID, capability: str) -> bool:
        if not 32 <= len(capability) <= 128:
            return False
        async with self._lock:
            await self._expire_locked()
            state = self._ceremonies.get(ceremony_id)
            return bool(
                state is not None
                and state.status not in _TERMINAL_AUTH_STATUSES
                and hmac.compare_digest(
                    state.capability_digest,
                    self._lookup_digest("ceremony:" + capability),
                )
            )

    async def authentication_frame(
        self,
        ceremony_id: UUID,
        capability: str,
    ) -> bytes:
        async with self._lock:
            state = await self._surface_ceremony_locked(ceremony_id, capability)
            return await state.runtime.interactive_frame()

    async def authentication_event(
        self,
        ceremony_id: UUID,
        capability: str,
        event: BrowserInteractiveEvent,
    ) -> None:
        async with self._lock:
            state = await self._surface_ceremony_locked(ceremony_id, capability)
            await state.runtime.interactive_event(event)

    def _mac(self, value: bytes) -> bytes:
        return hmac.digest(self._process_secret, value, hashlib.sha256)

    def _lease_ref(self, scope: _LeaseScope) -> str:
        encoded = json.dumps(
            {
                "profile_id": str(scope.profile_id),
                "tenant_id": scope.tenant_id,
                "principal_id": scope.principal_id,
                "provider_ref": scope.provider_ref,
                "run_id": str(scope.run_id),
                "attempt_number": scope.attempt_number,
                "expires_at": scope.expires_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(self._mac(b"lease:" + encoded)).decode().rstrip("=")

    def _ceremony_capability(self, profile_id: UUID, counter: int) -> str:
        digest = self._mac(f"ceremony:{profile_id}:{counter}".encode())
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def _lookup_digest(self, secret_ref: str) -> bytes:
        return self._mac(b"lookup:" + secret_ref.encode())

    def _find_lease(self, lease_ref: str) -> tuple[bytes | None, _LeaseState | None]:
        if not 32 <= len(lease_ref) <= 128:
            return None, None
        candidate = self._lookup_digest(lease_ref)
        for key, state in self._leases.items():
            if hmac.compare_digest(candidate, key):
                return key, state
        return None, None

    async def _require_lease_locked(self, lease_ref: str) -> _LeaseState:
        key, state = self._find_lease(lease_ref)
        if state is None or key is None:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        if state.scope.expires_at <= self._now():
            self._leases.pop(key)
            with suppress(Exception):
                await state.runtime.close()
            state.closed = True
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        metadata = await self._store.find_by_profile(state.scope.profile_id)
        if metadata is None or metadata.revoked:
            self._leases.pop(key)
            with suppress(Exception):
                await state.runtime.close()
            state.closed = True
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        return state

    async def _expire_locked(self) -> None:
        now = self._now()
        for key, lease_state in tuple(self._leases.items()):
            if lease_state.scope.expires_at <= now:
                self._leases.pop(key)
                with suppress(Exception):
                    await lease_state.runtime.close()
                lease_state.closed = True
        for _ceremony_id, ceremony_state in tuple(self._ceremonies.items()):
            if ceremony_state.expires_at <= now:
                ceremony_state.status = BrowserAuthenticationStatus.EXPIRED
                with suppress(Exception):
                    await ceremony_state.runtime.close()
                await self._finish_ceremony_locked(ceremony_state)
        for ceremony_id, terminal_state in tuple(self._terminal_ceremonies.items()):
            if terminal_state.expires_at <= now:
                self._terminal_ceremonies.pop(ceremony_id, None)

    def _lease_for_profile(self, profile_id: UUID) -> _LeaseState | None:
        return next(
            (state for state in self._leases.values() if state.scope.profile_id == profile_id),
            None,
        )

    def _active_ceremony_for_profile(self, profile_id: UUID) -> _CeremonyState | None:
        return next(
            (
                state
                for state in self._ceremonies.values()
                if state.profile_id == profile_id and state.status not in _TERMINAL_AUTH_STATUSES
            ),
            None,
        )

    def _owned_ceremony(self, ceremony_id: UUID, principal: Principal) -> _CeremonyState:
        state = self._ceremonies.get(ceremony_id)
        if (
            state is None
            or state.tenant_id != principal.tenant_id
            or state.principal_id != principal.principal_id
        ):
            raise ConflictError("browser authentication ceremony scope mismatch")
        return state

    def _owned_terminal_ceremony(
        self,
        ceremony_id: UUID,
        principal: Principal,
    ) -> _TerminalCeremonyState | None:
        state = self._terminal_ceremonies.get(ceremony_id)
        if state is None:
            return None
        if state.tenant_id != principal.tenant_id or state.principal_id != principal.principal_id:
            raise ConflictError("browser authentication ceremony scope mismatch")
        return state

    async def _finish_ceremony_locked(
        self,
        state: _CeremonyState,
    ) -> BrowserAuthenticationView:
        self._ceremonies.pop(state.id, None)
        terminal = _TerminalCeremonyState(
            id=state.id,
            profile_id=state.profile_id,
            tenant_id=state.tenant_id,
            principal_id=state.principal_id,
            expires_at=state.expires_at,
            status=state.status,
        )
        self._terminal_ceremonies[state.id] = terminal
        return _terminal_ceremony_view(terminal)

    async def _surface_ceremony_locked(
        self,
        ceremony_id: UUID,
        capability: str,
    ) -> _CeremonyState:
        await self._expire_locked()
        state = self._ceremonies.get(ceremony_id)
        if (
            state is None
            or state.status in _TERMINAL_AUTH_STATUSES
            or not 32 <= len(capability) <= 128
            or not hmac.compare_digest(
                state.capability_digest,
                self._lookup_digest("ceremony:" + capability),
            )
        ):
            raise ConflictError("browser authentication capability is invalid")
        return state

    async def _owned_metadata(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
    ) -> ProfileMaterialMetadata:
        by_profile = await self._store.find_by_profile(profile_id)
        by_ref = await self._store.find_by_ref(provider_ref)
        if (
            by_profile is None
            or by_ref is None
            or by_profile != by_ref
            or by_profile.tenant_id != principal.tenant_id
            or by_profile.principal_id != principal.principal_id
        ):
            raise ConflictError("browser profile session scope mismatch")
        return by_profile


_TERMINAL_AUTH_STATUSES = frozenset(
    {
        BrowserAuthenticationStatus.READY,
        BrowserAuthenticationStatus.EXPIRED,
        BrowserAuthenticationStatus.CANCELLED,
    }
)


def _ceremony_view(state: _CeremonyState) -> BrowserAuthenticationView:
    return BrowserAuthenticationView(
        id=state.id,
        profile_id=state.profile_id,
        status=state.status,
        expires_at=state.expires_at,
    )


def _terminal_ceremony_view(state: _TerminalCeremonyState) -> BrowserAuthenticationView:
    return BrowserAuthenticationView(
        id=state.id,
        profile_id=state.profile_id,
        status=state.status,
        expires_at=state.expires_at,
    )
