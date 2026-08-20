"""Public browser profile and standing-grant management."""

from __future__ import annotations

import asyncio
import base64
import builtins
import json
import math
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    ALLOWED_BROWSER_PROFILE_TRANSITIONS,
    BrowserActionKind,
    BrowserAuthenticationRecord,
    BrowserAuthenticationStatus,
    BrowserAuthenticationView,
    BrowserGrant,
    BrowserGrantView,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserProfileView,
    BrowserProviderError,
    normalize_browser_origin,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.views import Page
from agent_core.ports.browser_authentications import BrowserAuthenticationRepository
from agent_core.ports.browser_grants import BrowserGrantRepository
from agent_core.ports.browser_profiles import (
    BrowserProfileControlPlane,
    BrowserProfileRepository,
)
from agent_core.ports.browser_sessions import BrowserAuthenticationControlPlane
from agent_core.ports.determinism import Clock, IdFactory


class _BrowserUnitOfWork(Protocol):
    browser_profiles: BrowserProfileRepository
    browser_authentications: BrowserAuthenticationRepository
    browser_grants: BrowserGrantRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class BrowserUnitOfWorkFactory(Protocol):
    def __call__(self) -> _BrowserUnitOfWork: ...


def _profile_view(profile: BrowserProfile) -> BrowserProfileView:
    return BrowserProfileView(
        id=profile.id,
        allowed_origins=profile.allowed_origins,
        status=profile.status,
        generation=profile.generation,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        last_used_at=profile.last_used_at,
    )


def _authentication_view(record: BrowserAuthenticationRecord) -> BrowserAuthenticationView:
    return BrowserAuthenticationView(
        id=record.id,
        profile_id=record.profile_id,
        status=record.status,
        expires_at=record.expires_at,
    )


def _grant_view(grant: BrowserGrant) -> BrowserGrantView:
    return BrowserGrantView.model_validate(grant.model_dump())


class BrowserProfileManagementService:
    def __init__(
        self,
        *,
        uow_factory: BrowserUnitOfWorkFactory,
        lifecycle: BrowserProfileControlPlane,
        authentications: BrowserAuthenticationControlPlane,
        clock: Clock,
        ids: IdFactory,
        authentication_timeout_seconds: float = 30.0,
        authentication_lock_timeout_seconds: float = 5.0,
    ) -> None:
        if not math.isfinite(authentication_timeout_seconds) or authentication_timeout_seconds <= 0:
            raise ValueError("authentication timeout must be positive and finite")
        if (
            not math.isfinite(authentication_lock_timeout_seconds)
            or authentication_lock_timeout_seconds <= 0
        ):
            raise ValueError("authentication lock timeout must be positive and finite")
        self._uow_factory = uow_factory
        self._lifecycle = lifecycle
        self._authentications = authentications
        self._clock = clock
        self._ids = ids
        self._authentication_timeout_seconds = authentication_timeout_seconds
        self._authentication_lock_timeout_seconds = authentication_lock_timeout_seconds

    async def create(
        self,
        principal: Principal,
        allowed_origins: tuple[str, ...],
        idempotency_key: str | None = None,
    ) -> BrowserProfileView:
        require_scope(principal, "browser.profile.write")
        normalized = tuple(normalize_browser_origin(origin) for origin in allowed_origins)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("browser profile requires unique allowed origins")
        now = self._clock.now()
        profile_id = (
            self._ids.new_id()
            if idempotency_key is None
            else _idempotent_resource_id("browser-profile", principal, idempotency_key)
        )
        reservation = BrowserProfile(
            id=profile_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            allowed_origins=normalized,
            status=BrowserProfileStatus.PROVISIONING,
            generation=0,
            created_at=now,
            updated_at=now,
        )
        try:
            async with self._uow_factory() as uow:
                await uow.browser_profiles.create(reservation)
        except ConflictError:
            async with self._uow_factory() as uow:
                existing = await uow.browser_profiles.get(profile_id, principal)
            if existing.allowed_origins != normalized:
                raise ConflictError("idempotency key was reused with a different profile") from None
            if existing.status is not BrowserProfileStatus.PROVISIONING:
                return _profile_view(existing)
            reservation = existing
        try:
            provisioning = await self._lifecycle.provision(
                reservation.id,
                principal,
                normalized,
            )
        except Exception as original:
            failures: list[Exception] = [original]
            try:
                async with self._uow_factory() as uow:
                    await uow.browser_profiles.transition(
                        reservation.id,
                        principal,
                        expected_generation=0,
                        status=BrowserProfileStatus.REVOKED,
                        updated_at=self._clock.now(),
                    )
            except Exception as compensation:
                failures.append(compensation)
            if len(failures) > 1:
                raise ExceptionGroup(
                    "browser profile provisioning and compensation failed",
                    failures,
                ) from original
            raise
        try:
            async with self._uow_factory() as uow:
                bound = await uow.browser_profiles.bind(
                    reservation.id,
                    principal,
                    expected_generation=0,
                    provisioning=provisioning,
                    updated_at=self._clock.now(),
                )
        except Exception as original:
            failures = [original]
            try:
                await self._lifecycle.delete(
                    reservation.id,
                    principal,
                    provisioning.provider_ref,
                )
            except Exception as cleanup:
                failures.append(cleanup)
            try:
                async with self._uow_factory() as uow:
                    await uow.browser_profiles.transition(
                        reservation.id,
                        principal,
                        expected_generation=0,
                        status=BrowserProfileStatus.REVOKED,
                        updated_at=self._clock.now(),
                    )
            except Exception as compensation:
                failures.append(compensation)
            if len(failures) > 1:
                raise ExceptionGroup(
                    "browser profile binding and compensation failed",
                    failures,
                ) from original
            raise
        return _profile_view(bound)

    async def get(self, principal: Principal, profile_id: UUID) -> BrowserProfileView:
        require_scope(principal, "browser.profile.read")
        async with self._uow_factory() as uow:
            return _profile_view(await uow.browser_profiles.get(profile_id, principal))

    async def list(
        self,
        principal: Principal,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[BrowserProfileView]:
        require_scope(principal, "browser.profile.read")
        after_created_at, after_id = _decode_browser_cursor(cursor)
        async with self._uow_factory() as uow:
            profiles = await uow.browser_profiles.list(
                principal,
                limit=limit + 1,
                after_created_at=after_created_at,
                after_id=after_id,
            )
        has_more = len(profiles) > limit
        visible = profiles[:limit]
        return Page(
            items=[_profile_view(profile) for profile in visible],
            next_cursor=(
                _encode_browser_cursor(visible[-1].created_at, visible[-1].id)
                if has_more and visible
                else None
            ),
        )

    async def revoke(self, principal: Principal, profile_id: UUID) -> BrowserProfileView:
        require_scope(principal, "browser.profile.write")
        async with self._uow_factory() as uow:
            profile = await uow.browser_profiles.get(profile_id, principal)
        if profile.provider_ref is not None:
            await self._lifecycle.revoke(profile_id, principal, profile.provider_ref)
        async with self._uow_factory() as uow:
            if profile.status is BrowserProfileStatus.REVOKED:
                revoked = profile
            else:
                revoked = await uow.browser_profiles.transition(
                    profile_id,
                    principal,
                    expected_generation=profile.generation,
                    status=BrowserProfileStatus.REVOKED,
                    updated_at=self._clock.now(),
                )
        return _profile_view(revoked)

    async def delete(self, principal: Principal, profile_id: UUID) -> None:
        require_scope(principal, "browser.profile.write")
        try:
            async with self._uow_factory() as uow:
                profile = await uow.browser_profiles.get(profile_id, principal)
        except NotFoundError:
            return
        if profile.status is not BrowserProfileStatus.REVOKED:
            raise ConflictError("browser profile must be revoked before deletion")
        if profile.provider_ref is not None:
            await self._lifecycle.delete(profile_id, principal, profile.provider_ref)
        async with self._uow_factory() as uow:
            await uow.browser_profiles.delete(
                profile_id,
                principal,
                expected_generation=profile.generation,
            )

    async def begin_authentication(
        self,
        principal: Principal,
        profile_id: UUID,
        *,
        login_url: str,
    ) -> BrowserAuthenticationView:
        require_scope(principal, "browser.profile.write")
        launched: BrowserAuthenticationView | None = None
        try:
            async with (
                self._uow_factory() as uow,
                uow.browser_profiles.authentication_admission(
                    profile_id,
                    principal,
                    timeout_seconds=self._authentication_lock_timeout_seconds,
                ) as profile,
            ):
                if (
                    profile.status
                    in {BrowserProfileStatus.PROVISIONING, BrowserProfileStatus.REVOKED}
                    or profile.provider_ref is None
                ):
                    raise ConflictError("browser profile cannot authenticate in its current state")
                existing = await uow.browser_authentications.list(
                    principal,
                    profile_id=profile_id,
                )
                active = next(
                    (
                        record
                        for record in reversed(existing)
                        if record.status
                        not in {
                            BrowserAuthenticationStatus.READY,
                            BrowserAuthenticationStatus.EXPIRED,
                            BrowserAuthenticationStatus.CANCELLED,
                        }
                        and record.expires_at > self._clock.now()
                    ),
                    None,
                )
                if active is not None:
                    raise ConflictError("browser profile already has an active authentication")
                try:
                    async with asyncio.timeout(self._authentication_timeout_seconds):
                        launched = await self._authentications.begin_authentication(
                            profile_id,
                            principal,
                            profile.provider_ref,
                            login_url=login_url,
                        )
                except TimeoutError as exc:
                    raise BrowserProviderError(
                        "tool.browser.provider_unavailable",
                        retryable=True,
                    ) from exc
                now = self._clock.now()
                record = BrowserAuthenticationRecord(
                    id=launched.id,
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    profile_id=profile_id,
                    status=launched.status,
                    expires_at=launched.expires_at,
                    created_at=now,
                    updated_at=now,
                )
                await uow.browser_authentications.create(record)
        except Exception as original:
            if launched is None:
                raise
            try:
                await self._authentications.cancel_authentication(launched.id, principal)
            except Exception as compensation:
                raise ExceptionGroup(
                    "browser authentication persistence and compensation failed",
                    [original, compensation],
                ) from original
            raise
        return launched

    async def authentication_status(
        self,
        principal: Principal,
        authentication_id: UUID,
    ) -> BrowserAuthenticationView:
        require_scope(principal, "browser.profile.read")
        async with self._uow_factory() as uow:
            record = await uow.browser_authentications.get(authentication_id, principal)
        remote = await self._authentications.authentication_status(authentication_id, principal)
        if remote.id != record.id or remote.profile_id != record.profile_id:
            raise ConflictError("browser authentication response identity changed")
        async with self._uow_factory() as uow:
            current = await uow.browser_authentications.get(authentication_id, principal)
            if current.status is not remote.status:
                current = await uow.browser_authentications.transition(
                    authentication_id,
                    principal,
                    expected_status=current.status,
                    status=remote.status,
                    updated_at=self._clock.now(),
                )
            await self._synchronize_profile_status(uow.browser_profiles, principal, current)
        return _authentication_view(current)

    async def list_authentications(
        self,
        principal: Principal,
        profile_id: UUID,
    ) -> builtins.list[BrowserAuthenticationView]:
        require_scope(principal, "browser.profile.read")
        async with self._uow_factory() as uow:
            records = await uow.browser_authentications.list(principal, profile_id=profile_id)
        return [_authentication_view(record) for record in records]

    async def cancel_authentication(
        self,
        principal: Principal,
        authentication_id: UUID,
    ) -> BrowserAuthenticationView:
        require_scope(principal, "browser.profile.write")
        async with self._uow_factory() as uow:
            record = await uow.browser_authentications.get(authentication_id, principal)
        remote = await self._authentications.cancel_authentication(authentication_id, principal)
        if remote.id != record.id or remote.profile_id != record.profile_id:
            raise ConflictError("browser authentication response identity changed")
        async with self._uow_factory() as uow:
            current = await uow.browser_authentications.get(authentication_id, principal)
            if current.status is not remote.status:
                current = await uow.browser_authentications.transition(
                    authentication_id,
                    principal,
                    expected_status=current.status,
                    status=remote.status,
                    updated_at=self._clock.now(),
                )
        return _authentication_view(current)

    async def _synchronize_profile_status(
        self,
        profiles: BrowserProfileRepository,
        principal: Principal,
        authentication: BrowserAuthenticationRecord,
    ) -> None:
        target = {
            BrowserAuthenticationStatus.READY: BrowserProfileStatus.READY,
            BrowserAuthenticationStatus.NEEDS_USER: BrowserProfileStatus.NEEDS_USER,
            BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED: (
                BrowserProfileStatus.AUTHENTICATION_REQUIRED
            ),
        }.get(authentication.status)
        if target is None:
            return
        profile = await profiles.get(authentication.profile_id, principal)
        if profile.status is target or target not in ALLOWED_BROWSER_PROFILE_TRANSITIONS.get(
            profile.status, frozenset()
        ):
            return
        await profiles.transition(
            profile.id,
            principal,
            expected_generation=profile.generation,
            status=target,
            updated_at=self._clock.now(),
        )


class BrowserGrantManagementService:
    def __init__(
        self,
        *,
        uow_factory: BrowserUnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        agent_version: str,
        policy_version: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._agent_version = agent_version
        self._policy_version = policy_version

    async def create(
        self,
        principal: Principal,
        *,
        profile_id: UUID,
        allowed_origins: tuple[str, ...],
        action_kinds: tuple[BrowserActionKind, ...],
        element_roles: tuple[str, ...],
        element_names: tuple[str, ...],
        purpose: str | None,
        starts_at: datetime,
        expires_at: datetime,
        idempotency_key: str | None = None,
    ) -> BrowserGrantView:
        require_scope(principal, "browser.grant.write")
        async with self._uow_factory() as uow:
            profile = await uow.browser_profiles.get(profile_id, principal)
            if profile.status is not BrowserProfileStatus.READY:
                raise ConflictError("browser profile must be ready before granting authority")
            normalized = tuple(normalize_browser_origin(origin) for origin in allowed_origins)
            if not set(normalized).issubset(profile.allowed_origins):
                raise ConflictError("browser grant origin exceeds profile scope")
            now = self._clock.now()
            grant = BrowserGrant(
                id=(
                    self._ids.new_id()
                    if idempotency_key is None
                    else _idempotent_resource_id("browser-grant", principal, idempotency_key)
                ),
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                profile_id=profile.id,
                profile_generation=profile.generation,
                agent_version=self._agent_version,
                policy_version=self._policy_version,
                allowed_origins=normalized,
                action_kinds=action_kinds,
                element_roles=element_roles,
                element_names=element_names,
                purpose=purpose,
                starts_at=starts_at,
                expires_at=expires_at,
                approved_by=principal.principal_id,
                created_at=now,
                updated_at=now,
            )
            try:
                created = await uow.browser_grants.create(grant)
            except ConflictError:
                existing = await uow.browser_grants.get(grant.id, principal)
                if existing.model_dump(exclude={"created_at", "updated_at"}) != grant.model_dump(
                    exclude={"created_at", "updated_at"}
                ):
                    raise ConflictError(
                        "idempotency key was reused with a different grant"
                    ) from None
                created = existing
        return _grant_view(created)

    async def get(self, principal: Principal, grant_id: UUID) -> BrowserGrantView:
        require_scope(principal, "browser.grant.read")
        async with self._uow_factory() as uow:
            return _grant_view(await uow.browser_grants.get(grant_id, principal))

    async def list(
        self,
        principal: Principal,
        *,
        profile_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[BrowserGrantView]:
        require_scope(principal, "browser.grant.read")
        after_created_at, after_id = _decode_browser_cursor(cursor)
        async with self._uow_factory() as uow:
            grants = await uow.browser_grants.list(
                principal,
                profile_id=profile_id,
                limit=limit + 1,
                after_created_at=after_created_at,
                after_id=after_id,
            )
        has_more = len(grants) > limit
        visible = grants[:limit]
        return Page(
            items=[_grant_view(grant) for grant in visible],
            next_cursor=(
                _encode_browser_cursor(visible[-1].created_at, visible[-1].id)
                if has_more and visible
                else None
            ),
        )

    async def revoke(self, principal: Principal, grant_id: UUID) -> BrowserGrantView:
        require_scope(principal, "browser.grant.write")
        async with self._uow_factory() as uow:
            return _grant_view(
                await uow.browser_grants.revoke(
                    grant_id,
                    principal,
                    revoked_at=self._clock.now(),
                )
            )

    async def delete(self, principal: Principal, grant_id: UUID) -> None:
        require_scope(principal, "browser.grant.write")
        try:
            async with self._uow_factory() as uow:
                grant = await uow.browser_grants.get(grant_id, principal)
                if grant.revoked_at is None:
                    raise ConflictError("browser grant must be revoked before deletion")
                await uow.browser_grants.delete(grant_id, principal)
        except NotFoundError:
            return


def _idempotent_resource_id(kind: str, principal: Principal, key: str) -> UUID:
    if not key or len(key) > 255:
        raise ValueError("idempotency key is invalid")
    return uuid5(
        NAMESPACE_URL,
        f"veetbot:{kind}:{principal.tenant_id}:{principal.principal_id}:{key}",
    )


def _encode_browser_cursor(created_at: datetime, resource_id: UUID) -> str:
    payload = json.dumps(
        {"created_at": created_at.isoformat(), "id": str(resource_id)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_browser_cursor(value: str | None) -> tuple[datetime | None, UUID | None]:
    if value is None:
        return None, None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(decoded, dict) or set(decoded) != {"created_at", "id"}:
            raise ValueError
        created_at = datetime.fromisoformat(decoded["created_at"])
        resource_id = UUID(decoded["id"])
        if created_at.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("browser cursor is malformed") from exc
    return created_at, resource_id
