"""Exclusive session and direct-authentication service for hosted profiles."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from agent_core.browser_control_plane.handoff import (
    DeviceSessionHandoff,
    confirmed_url_in_scope,
    device_session_material,
)
from agent_core.browser_control_plane.models import (
    ProfileMaterialIdentity,
    ProfileMaterialMetadata,
)
from agent_core.browser_control_plane.ports import EncryptedProfileStore
from agent_core.browser_control_plane.verification import (
    BrowserSiteVerification,
    BrowserVerificationCatalog,
)
from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    MAXIMUM_BROWSER_LEASE_LIFETIME_SECONDS,
    MAXIMUM_BROWSER_LEASE_SECONDS,
    BrowserAction,
    BrowserAuthenticationMode,
    BrowserAuthenticationStatus,
    BrowserAuthenticationView,
    BrowserDispatchConstraint,
    BrowserElementFacts,
    BrowserInteractiveEvent,
    BrowserLease,
    BrowserObservation,
    BrowserObservationExpansion,
    BrowserObservationFacts,
    BrowserPageEvidence,
    BrowserProviderError,
    BrowserSnapshot,
    BrowserVerificationStage,
    browser_origin,
    require_service_origin,
)
from agent_core.domain.browser_diagnostics import browser_phase_call
from agent_core.domain.browser_extraction import BrowserExtractionRequest
from agent_core.domain.browser_upload import BrowserImageFile
from agent_core.domain.errors import ConflictError
from agent_core.ports.browser import expand_browser_observation, extract_browser_observation
from agent_core.ports.browser_upload import upload_browser_image

MAXIMUM_LEASE_SECONDS = MAXIMUM_BROWSER_LEASE_SECONDS
MAXIMUM_LEASE_LIFETIME_SECONDS = MAXIMUM_BROWSER_LEASE_LIFETIME_SECONDS
AUTHENTICATION_CEREMONY_SECONDS = 5 * 60
# A device handoff's two verification loads end within this budget (ADR-0128).
DEVICE_VERIFICATION_SECONDS = 30.0
# Headed browsers alive at once, each budgeted a third of the container (ADR-0145).
MAXIMUM_LIVE_BROWSERS = 3
# Element facts beside one observation are cut to this many serialized bytes.
MAXIMUM_FACTS_BYTES = 64 * 1024
# Finished ceremonies kept for idempotent status reads, oldest dropped first.
MAXIMUM_TERMINAL_CEREMONIES = 1_024
# An outcome no client has read yet is kept this long (ADR-0128 D16).
TERMINAL_CEREMONY_RETENTION_SECONDS = 24 * 60 * 60
# The sweep checks an open remote ceremony in its last seconds of life.
SWEEP_WINDOW_SECONDS = 20
_NO_SESSION_MATERIAL = b'{"format_version":1}'
SurfaceOperation = Literal["frame", "events", "handoff"]
_LOGGER = logging.getLogger(__name__)


class CeremonyCapabilityRejected(Exception):  # noqa: N818 - a refusal, not a fault
    """The ceremony capability is missing, wrong, spent, expired or for another mode."""


class DeviceHandoffInvalid(Exception):  # noqa: N818 - a refusal, not a fault
    """The handoff is well formed but its confirmed page is outside the profile."""


class DeviceSessionRejected(Exception):  # noqa: N818 - a refusal, not a fault
    """Verification decided the handed-off session cannot be sealed (ADR-0128)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _LeaseScope:
    profile_id: UUID
    tenant_id: str
    principal_id: str
    provider_ref: str
    run_id: UUID
    attempt_number: int
    # The first expiry; with the rest of the scope it derives the lease reference.
    expires_at: datetime


@dataclass(slots=True)
class _LeaseState:
    scope: _LeaseScope
    identity: ProfileMaterialIdentity
    runtime: BrowserSessionRuntime
    acquired_at: datetime
    # The live expiry, which renewal extends.
    expires_at: datetime
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
    # A device ceremony has no browser until its handoff is verified (ADR-0128).
    runtime: BrowserSessionRuntime | None
    status: BrowserAuthenticationStatus
    capability_digest: bytes
    mode: BrowserAuthenticationMode = BrowserAuthenticationMode.REMOTE
    # A device handoff is being processed: the capability is spent.
    consumed: bool = False
    # The sweep has sampled this remote ceremony near its expiry (ADR-0128 D16).
    swept: bool = False
    # Serializes calls into the headed page: sweep, refresh, frame and events.
    runtime_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class _VerificationLoad:
    """How far one of a handoff's two loads got: all its diagnostic may say (ADR-0128)."""

    name: Literal["with_session", "without_session"]
    stage: BrowserVerificationStage = BrowserVerificationStage.START
    finished: bool = False
    failed: bool = False

    def enter(self, stage: BrowserVerificationStage) -> None:
        # A runtime's value outside the vocabulary fails its load, unrecorded.
        self.stage = BrowserVerificationStage(stage)


@dataclass(frozen=True, slots=True)
class _TerminalCeremonyState:
    id: UUID
    profile_id: UUID
    tenant_id: str
    principal_id: str
    expires_at: datetime
    retained_until: datetime
    status: BrowserAuthenticationStatus


class BrowserSessionRuntime(Protocol):
    def set_interactive_authentication(self) -> None: ...

    async def start(
        self,
        material: bytes,
        allowed_origins: tuple[str, ...],
        *,
        headed: bool,
    ) -> None: ...

    async def navigate(self, url: str) -> BrowserObservation: ...

    async def observe(self) -> BrowserObservation: ...

    async def act(self, action: BrowserAction) -> BrowserObservation: ...

    async def act_within_grant(
        self,
        action: BrowserAction,
        constraint: BrowserDispatchConstraint,
        *,
        now: datetime,
    ) -> BrowserObservation: ...

    async def load_page_evidence(
        self,
        url: str,
        *,
        on_stage: Callable[[BrowserVerificationStage], None],
    ) -> BrowserPageEvidence: ...

    def facts(self, revision: str) -> BrowserObservationFacts | None: ...

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
        device_sign_in_enabled: bool = True,
        verification_seconds: float = DEVICE_VERIFICATION_SECONDS,
        verification: BrowserVerificationCatalog | None = None,
        sweep_sample_seconds: float = 2.5,
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
        self._device_sign_in_enabled = device_sign_in_enabled
        self._verification_seconds = verification_seconds
        self._verification = verification or BrowserVerificationCatalog()
        self._sweep_sample_seconds = sweep_sample_seconds
        self._leases: dict[bytes, _LeaseState] = {}
        self._ceremonies: dict[UUID, _CeremonyState] = {}
        self._terminal_ceremonies: dict[UUID, _TerminalCeremonyState] = {}
        self._lock = asyncio.Lock()
        self._verification_browsers = 0
        self._pending_acquisitions: dict[UUID, object] = {}

    @browser_phase_call("acquisition", "hosted")
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
        await self._expire()
        async with self._lock:
            existing = self._lease_for_profile(profile_id)
            if existing is not None:
                # A repeat for the same run attempt, whose earlier answer may have
                # been lost, is that attempt's lease whatever horizon it asks for.
                if replace(existing.scope, expires_at=expires_at) != scope:
                    raise ConflictError("browser profile already has an active lease")
                return BrowserLease(
                    lease_ref=self._lease_ref(existing.scope),
                    expires_at=existing.expires_at,
                    sequence=existing.sequence,
                )
            if self._active_ceremony_for_profile(profile_id) is not None:
                raise ConflictError("browser profile has an active authentication ceremony")
            if profile_id in self._pending_acquisitions:
                raise ConflictError("browser profile already has a pending lease")
            self._admit_browsers_locked(1)
            marker = object()
            self._pending_acquisitions[profile_id] = marker
            self._verification_browsers += 1
        identity = metadata.identity()
        runtime: BrowserSessionRuntime | None = None
        published = False
        try:
            material = await self._store.load(identity)
            runtime = self._runtime_factory(principal.tenant_id)
            await runtime.start(material, metadata.allowed_origins, headed=True)
            definition = next(
                (site for site in self._verification.sites if site.profile_id == profile_id),
                None,
            )
            if definition is not None:
                if definition.origin not in metadata.allowed_origins:
                    raise BrowserProviderError(
                        "tool.browser.authentication_required", retryable=False
                    )
                async with asyncio.timeout(min(self._verification_seconds, 10.0)):
                    evidence = await runtime.load_page_evidence(
                        definition.origin + definition.protected_path,
                        on_stage=_VerificationLoad("with_session").enter,
                    )
                    if (
                        not evidence.on_allowed_origin
                        or evidence.challenge_visible
                        or not definition.confirms(await runtime.observe())
                    ):
                        raise BrowserProviderError(
                            "tool.browser.authentication_required", retryable=False
                        )
            async with self._lock:
                current = await self._owned_metadata(profile_id, principal, provider_ref)
                if (
                    current.revoked
                    or current.identity() != identity
                    or self._pending_acquisitions.get(profile_id) is not marker
                    or expires_at <= self._now()
                ):
                    raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
                self._leases[self._lookup_digest(lease_ref)] = _LeaseState(
                    scope=scope,
                    identity=identity,
                    runtime=runtime,
                    acquired_at=now,
                    expires_at=expires_at,
                )
                published = True
                self._verification_browsers -= 1
                self._pending_acquisitions.pop(profile_id)
        finally:
            if not published:
                try:
                    if runtime is not None:
                        await runtime.close()
                finally:
                    # Capacity remains reserved until the browser has stopped.
                    async with self._lock:
                        self._verification_browsers -= 1
                        if self._pending_acquisitions.get(profile_id) is marker:
                            self._pending_acquisitions.pop(profile_id)
        return BrowserLease(lease_ref=lease_ref, expires_at=expires_at)

    async def navigate(self, lease_ref: str, url: str) -> BrowserObservation:
        return (await self.navigate_snapshot(lease_ref, url)).observation

    async def observe(self, lease_ref: str) -> BrowserObservation:
        return (await self.observe_snapshot(lease_ref)).observation

    async def act(
        self,
        lease_ref: str,
        action: BrowserAction,
        *,
        sequence: int,
        constraint: BrowserDispatchConstraint | None = None,
    ) -> BrowserObservation:
        snapshot = await self.act_snapshot(
            lease_ref, action, sequence=sequence, constraint=constraint
        )
        return snapshot.observation

    async def navigate_snapshot(self, lease_ref: str, url: str) -> BrowserSnapshot:
        """Navigate, returning the observation and its element facts (ADR-0129)."""
        state = await self._require_lease(lease_ref)
        if browser_origin(url) not in state.identity.allowed_origins:
            raise BrowserProviderError("tool.browser.url_disallowed", retryable=False)
        async with state.lock:
            if state.closed:
                raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
            return _snapshot(state.runtime, await state.runtime.navigate(url))

    async def observe_snapshot(
        self,
        lease_ref: str,
        expansion: BrowserObservationExpansion | None = None,
        extraction: BrowserExtractionRequest | None = None,
    ) -> BrowserSnapshot:
        state = await self._require_lease(lease_ref)
        async with state.lock:
            if state.closed:
                raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
            if extraction is not None and expansion is not None:
                raise BrowserProviderError("tool.browser.action_not_allowed", retryable=False)
            observation = (
                await extract_browser_observation(state.runtime, extraction)
                if extraction is not None
                else (
                    await state.runtime.observe()
                    if expansion is None
                    else await expand_browser_observation(state.runtime, expansion)
                )
            )
            return _snapshot(state.runtime, observation)

    async def act_snapshot(
        self,
        lease_ref: str,
        action: BrowserAction,
        *,
        sequence: int,
        constraint: BrowserDispatchConstraint | None = None,
        image: BrowserImageFile | None = None,
    ) -> BrowserSnapshot:
        """Act once on the lease; a grant's constraint can only refuse (ADR-0129).

        Inside the lease lock and after the sequence check, a constraint that
        has expired or names an origin outside the lease is refused, and the
        runtime rechecks the live page under it. Every refusal leaves the
        sequence where it was.
        """
        state = await self._require_lease(lease_ref)
        async with state.lock:
            if state.closed:
                raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
            if sequence != state.sequence + 1:
                raise ConflictError("browser lease action sequence is invalid")
            now = self._now()
            if constraint is not None and (
                now >= constraint.not_after
                or not set(constraint.origins) <= set(state.identity.allowed_origins)
            ):
                raise BrowserProviderError("tool.browser.grant_not_applicable", retryable=False)
            try:
                if image is not None:
                    if constraint is not None:
                        raise BrowserProviderError(
                            "tool.browser.grant_not_applicable", retryable=False
                        )
                    observation = await upload_browser_image(state.runtime, action, image)
                elif constraint is None:
                    observation = await state.runtime.act(action)
                else:
                    observation = await state.runtime.act_within_grant(action, constraint, now=now)
            except BrowserProviderError:
                raise
            except Exception as exc:
                raise BrowserProviderError(
                    "tool.browser.outcome_unknown",
                    retryable=False,
                ) from exc
            state.sequence = sequence
            return _snapshot(state.runtime, observation)

    async def renew(self, lease_ref: str, *, deadline_at: datetime) -> BrowserLease:
        """Extend a live lease by at most fifteen minutes, never past an hour.

        An expired or revoked lease is closed, unsealed, and refused.
        """

        state = await self._require_lease(lease_ref)
        async with self._lock:
            if state.closed:
                raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
            now = self._now()
            extended = min(
                deadline_at,
                now + timedelta(seconds=MAXIMUM_LEASE_SECONDS),
                state.acquired_at + timedelta(seconds=MAXIMUM_LEASE_LIFETIME_SECONDS),
            )
            state.expires_at = max(state.expires_at, extended)
            return BrowserLease(
                lease_ref=lease_ref,
                expires_at=state.expires_at,
                sequence=state.sequence,
            )

    async def close(self, lease_ref: str) -> None:
        async with self._lock:
            key, state = self._find_lease(lease_ref)
            if state is None or key is None:
                return
            self._leases.pop(key)
            expired = state.expires_at <= self._now()
        if expired:
            # An expired lease closes without sealing, whoever asks and when.
            await self._close_lease_state(state)
            return
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
            self._pending_acquisitions.pop(profile_id, None)
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
                    await _close_ceremony_runtime(ceremony_state)
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
        mode: BrowserAuthenticationMode = BrowserAuthenticationMode.REMOTE,
    ) -> BrowserAuthenticationView:
        """Start one scoped login ceremony and preserve safe navigation failures.

        A remote ceremony opens the service's headed browser at ``login_url``. A
        device ceremony starts no browser: its capability authorizes one handoff
        of a session the owner's own client signed in (ADR-0128).
        """
        device = mode is BrowserAuthenticationMode.DEVICE
        if device and not self._device_sign_in_enabled:
            raise BrowserProviderError("tool.browser.provider_unavailable", retryable=False)
        metadata = await self._owned_metadata(profile_id, principal, provider_ref)
        if metadata.revoked:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        if browser_origin(login_url) not in metadata.allowed_origins:
            raise BrowserProviderError("tool.browser.url_disallowed", retryable=False)
        await self._expire()
        async with self._lock:
            if (
                self._lease_for_profile(profile_id) is not None
                or profile_id in self._pending_acquisitions
            ):
                raise ConflictError("browser profile already has an active or pending lease")
            if self._active_ceremony_for_profile(profile_id) is not None:
                raise ConflictError("browser profile already has an authentication ceremony")
            capability = _ceremony_capability()
            ceremony_id = UUID(bytes=self._mac(b"ceremony-id:" + capability.encode())[:16])
            expires_at = self._now() + timedelta(seconds=AUTHENTICATION_CEREMONY_SECONDS)
            identity = metadata.identity()
            if device:
                self._ceremonies[ceremony_id] = _CeremonyState(
                    id=ceremony_id,
                    profile_id=profile_id,
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    identity=identity,
                    expires_at=expires_at,
                    runtime=None,
                    status=BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
                    capability_digest=self._lookup_digest("ceremony:" + capability),
                    mode=BrowserAuthenticationMode.DEVICE,
                )
                return BrowserAuthenticationView(
                    id=ceremony_id,
                    profile_id=profile_id,
                    status=BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
                    expires_at=expires_at,
                    launch_url=(
                        f"{self._ceremony_base_url}/authentication/{ceremony_id}/handoff"
                        f"#capability={capability}"
                    ),
                )
            self._admit_browsers_locked(1)
            runtime = self._runtime_factory(principal.tenant_id)
            runtime.set_interactive_authentication()
            try:
                await runtime.start(
                    await self._store.load(identity),
                    metadata.allowed_origins,
                    headed=True,
                )
                await runtime.navigate(login_url)
            except BrowserProviderError:
                with suppress(Exception):
                    await runtime.close()
                raise
            except Exception as exc:
                with suppress(Exception):
                    await runtime.close()
                raise BrowserProviderError(
                    "tool.browser.provider_unavailable",
                    retryable=True,
                ) from exc
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
        await self._expire()
        async with self._lock:
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
        await self._expire()
        async with self._lock:
            terminal = self._owned_terminal_ceremony(ceremony_id, principal)
            if terminal is not None:
                return _terminal_ceremony_view(terminal)
            state = self._owned_ceremony(ceremony_id, principal)
            runtime = state.runtime
            if state.status in _TERMINAL_AUTH_STATUSES or runtime is None:
                # A device ceremony's status changes only through its handoff.
                return _ceremony_view(state)
        async with state.runtime_lock:
            async with asyncio.timeout(self._verification_seconds):
                status = await runtime.authentication_status()
                sealed = None
                if status is BrowserAuthenticationStatus.READY:
                    try:
                        sealed = await self._verify_remote_material(state)
                    except DeviceSessionRejected:
                        status = BrowserAuthenticationStatus.NEEDS_USER
            async with self._lock:
                if self._ceremonies.get(state.id) is not state:
                    terminal = self._owned_terminal_ceremony(ceremony_id, principal)
                    if terminal is not None:
                        return _terminal_ceremony_view(terminal)
                    raise CeremonyCapabilityRejected
                if sealed is not None:
                    metadata = await self._store.find_by_profile(state.profile_id)
                    if (
                        metadata is None
                        or metadata.revoked
                        or metadata.identity() != state.identity
                    ):
                        raise BrowserProviderError(
                            "tool.browser.profile_unavailable", retryable=False
                        )
                    try:
                        await self._store.write(state.identity, sealed)
                    except BaseException:
                        state.status = BrowserAuthenticationStatus.CANCELLED
                        await self._finish_ceremony_locked(state)
                        raise
                    finally:
                        with suppress(Exception):
                            await runtime.close()
                    state.status = BrowserAuthenticationStatus.READY
                    return await self._finish_ceremony_locked(state)
                state.status = status
                return _ceremony_view(state)

    async def _verify_remote_material(self, state: _CeremonyState) -> bytes:
        """A preliminary READY claim is insufficient: confirm against an empty session."""
        runtime = state.runtime
        if runtime is None:
            raise DeviceSessionRejected("session_unconfirmed")
        observation = await runtime.observe()
        if browser_origin(observation.url) not in state.identity.allowed_origins:
            raise DeviceSessionRejected("session_signed_out")
        material = await runtime.storage_state()
        return await self._verified_material(state, material, observation.url)

    async def _verified_material(self, state: _CeremonyState, material: bytes, page: str) -> bytes:
        async with self._lock:
            self._admit_browsers_locked(2)
            self._verification_browsers += 2
        try:
            return await self._remote_verification_loads(state, material, page)
        finally:
            async with self._lock:
                self._verification_browsers -= 2

    async def _remote_verification_loads(
        self, state: _CeremonyState, material: bytes, page: str
    ) -> bytes:
        origins = state.identity.allowed_origins
        definition = self._site_definition(state)
        if definition is not None:
            page = definition.origin + definition.protected_path
        signed_in = self._runtime_factory(state.tenant_id)
        signed_out = self._runtime_factory(state.tenant_id)
        loads = [
            asyncio.create_task(
                _page_evidence(
                    signed_in, material, origins, page, _VerificationLoad("with_session")
                )
            ),
            asyncio.create_task(
                _page_evidence(
                    signed_out,
                    _NO_SESSION_MATERIAL,
                    origins,
                    page,
                    _VerificationLoad("without_session"),
                )
            ),
        ]
        try:
            async with asyncio.timeout(self._verification_seconds):
                with_session, without_session = await asyncio.gather(*loads)
                await self._verify_page_pair(
                    definition, signed_in, signed_out, with_session, without_session, page
                )
                return await signed_in.storage_state()
        finally:
            for load in loads:
                load.cancel()
            await asyncio.gather(*loads, return_exceptions=True)
            for candidate in (signed_in, signed_out):
                with suppress(Exception):
                    await candidate.close()

    def _site_definition(self, state: _CeremonyState) -> BrowserSiteVerification | None:
        definition = next(
            (site for site in self._verification.sites if site.profile_id == state.profile_id), None
        )
        if definition is not None and definition.origin not in state.identity.allowed_origins:
            raise DeviceSessionRejected("session_unconfirmed")
        return definition

    async def _verify_page_pair(
        self,
        definition: BrowserSiteVerification | None,
        signed_in: BrowserSessionRuntime,
        signed_out: BrowserSessionRuntime,
        with_session: BrowserPageEvidence,
        without_session: BrowserPageEvidence,
        page: str,
    ) -> None:
        if definition is None:
            _decide(with_session, without_session, confirmed_path=urlsplit(page).path)
            return
        if (
            not with_session.on_allowed_origin
            or with_session.challenge_visible
            or not definition.confirms(await signed_in.observe())
        ):
            raise DeviceSessionRejected("session_unconfirmed")
        if (
            without_session.on_allowed_origin
            and not without_session.challenge_visible
            and definition.confirms(await signed_out.observe())
        ):
            raise DeviceSessionRejected("session_unconfirmed")

    async def cancel_authentication(
        self,
        ceremony_id: UUID,
        principal: Principal,
    ) -> BrowserAuthenticationView:
        await self._expire()
        async with self._lock:
            terminal = self._owned_terminal_ceremony(ceremony_id, principal)
            if terminal is not None:
                return _terminal_ceremony_view(terminal)
            state = self._owned_ceremony(ceremony_id, principal)
            if state.status in _TERMINAL_AUTH_STATUSES:
                return _ceremony_view(state)
            state.status = BrowserAuthenticationStatus.CANCELLED
            await _close_ceremony_runtime(state)
            return await self._finish_ceremony_locked(state)

    async def sweep(self) -> None:
        """Close what has expired, and keep a finished remote sign-in (ADR-0128 D16).

        Run on a timer by the service's lifespan. Expired leases and
        ceremonies close without waiting for traffic. An open remote ceremony
        in its last SWEEP_WINDOW_SECONDS is checked once: when two samples,
        taken ``sweep_sample_seconds`` apart, both find the sign-in ready, its
        state is sealed as a status refresh would; otherwise it is left to
        expire. The manual READY rule is unchanged.
        """
        await self._expire()
        async with self._lock:
            now = self._now()
            window = timedelta(seconds=SWEEP_WINDOW_SECONDS)
            due = [
                state
                for state in self._ceremonies.values()
                if state.mode is BrowserAuthenticationMode.REMOTE
                and state.runtime is not None
                and not state.swept
                and state.status not in _TERMINAL_AUTH_STATUSES
                and state.expires_at - now <= window
            ]
            for state in due:
                state.swept = True
        for state in due:
            # An unready or failed sample leaves the ceremony to expire.
            with suppress(Exception):
                await self._sweep_ceremony(state)

    async def _sweep_ceremony(self, state: _CeremonyState) -> None:
        if await self._sample(state) is not BrowserAuthenticationStatus.READY:
            return
        await asyncio.sleep(self._sweep_sample_seconds)
        if await self._sample(state) is not BrowserAuthenticationStatus.READY:
            return
        async with state.runtime_lock:
            try:
                async with asyncio.timeout(self._verification_seconds):
                    sealed = await self._verify_remote_material(state)
            except DeviceSessionRejected:
                return
            async with self._lock:
                runtime = state.runtime
                if (
                    runtime is None
                    or self._ceremonies.get(state.id) is not state
                    or state.status in _TERMINAL_AUTH_STATUSES
                ):
                    return
                metadata = await self._store.find_by_profile(state.profile_id)
                if metadata is None or metadata.revoked or metadata.identity() != state.identity:
                    return
                try:
                    try:
                        await self._store.write(state.identity, sealed)
                    finally:
                        with suppress(Exception):
                            await runtime.close()
                except Exception:
                    # The browser is gone either way; end the ceremony, not a lease.
                    state.status = BrowserAuthenticationStatus.CANCELLED
                    await self._finish_ceremony_locked(state)
                    raise
                state.status = BrowserAuthenticationStatus.READY
                await self._finish_ceremony_locked(state)

    async def _sample(self, state: _CeremonyState) -> BrowserAuthenticationStatus | None:
        async with state.runtime_lock:
            runtime = state.runtime
            if runtime is None or self._ceremonies.get(state.id) is not state:
                return None
            return await runtime.authentication_status()

    async def accept_device_session(
        self,
        ceremony_id: UUID,
        capability: str,
        handoff: DeviceSessionHandoff,
    ) -> None:
        """Verify one handed-off session and seal the verifying browser's state (ADR-0128).

        The first well-formed handoff spends the capability. The service
        loads the confirmed page twice, with the in-scope session and with
        none, and seals only when the site tells the two apart. Every other
        outcome ends the ceremony cancelled and writes nothing.
        """
        await self._expire()
        async with self._lock:
            state = self._ceremonies.get(ceremony_id)
            if state is None or not self._accepts_handoff_locked(state, capability):
                raise CeremonyCapabilityRejected
            if not confirmed_url_in_scope(handoff, state.identity.allowed_origins):
                raise DeviceHandoffInvalid
            # Counted before this ceremony reserves its own two browsers.
            room = self._live_browsers_locked() + 2 <= MAXIMUM_LIVE_BROWSERS
            state.consumed = True
        try:
            if not room:
                raise BrowserProviderError("tool.browser.provider_unavailable", retryable=True)
            await self._verify_and_seal(state, handoff)
        finally:
            async with self._lock:
                # Any exit short of READY, cancellation included, ends the
                # ceremony so it no longer blocks leases and begins.
                if (
                    self._ceremonies.get(state.id) is state
                    and state.status not in _TERMINAL_AUTH_STATUSES
                ):
                    state.status = BrowserAuthenticationStatus.CANCELLED
                    await self._finish_ceremony_locked(state)

    async def _verify_and_seal(self, state: _CeremonyState, handoff: DeviceSessionHandoff) -> None:
        metadata = await self._store.find_by_profile(state.profile_id)
        if metadata is None or metadata.revoked:
            raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
        try:
            material = device_session_material(handoff, state.identity.allowed_origins, self._now())
        except ValueError as exc:
            raise BrowserProviderError("tool.browser.provider_unavailable", retryable=True) from exc
        if material is None:
            raise DeviceSessionRejected("session_empty")
        page = handoff.confirmed_page()
        definition = self._site_definition(state)
        if definition is not None:
            page = definition.origin + definition.protected_path
        origins = state.identity.allowed_origins
        signed_in = self._runtime_factory(state.tenant_id)
        signed_out = self._runtime_factory(state.tenant_id)
        progress = (_VerificationLoad("with_session"), _VerificationLoad("without_session"))
        clock = asyncio.get_running_loop().time
        began = clock()
        loads = [
            asyncio.create_task(_page_evidence(signed_in, material, origins, page, progress[0])),
            asyncio.create_task(
                _page_evidence(signed_out, _NO_SESSION_MATERIAL, origins, page, progress[1])
            ),
        ]
        try:
            try:
                async with asyncio.timeout(self._verification_seconds):
                    with_session, without_session = await asyncio.gather(*loads)
                    await self._verify_page_pair(
                        definition, signed_in, signed_out, with_session, without_session, page
                    )
                    sealed = await signed_in.storage_state()
            except DeviceSessionRejected:
                raise
            except Exception as exc:
                # A provider error, a timeout, an oversized state or a browser
                # crash: the check could not finish. The cause stays chained
                # and is never rendered.
                _log_unfinished_verification(progress, clock() - began)
                raise BrowserProviderError(
                    "tool.browser.provider_unavailable", retryable=True
                ) from exc
            async with self._lock:
                if self._ceremonies.get(state.id) is not state:
                    # A cancel, a revocation or an expiry won the race.
                    current = await self._store.find_by_profile(state.profile_id)
                    if current is None or current.revoked:
                        raise BrowserProviderError(
                            "tool.browser.profile_unavailable", retryable=False
                        )
                    raise CeremonyCapabilityRejected
                current = await self._store.find_by_profile(state.profile_id)
                if current is None or current.revoked:
                    raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
                if current.identity() != state.identity:
                    raise ConflictError("browser profile scope changed during verification")
                await self._store.write(state.identity, sealed)
                state.status = BrowserAuthenticationStatus.READY
                await self._finish_ceremony_locked(state)
        finally:
            # A load still starting its browser would otherwise outlive close().
            for load in loads:
                load.cancel()
            await asyncio.gather(*loads, return_exceptions=True)
            for runtime in (signed_in, signed_out):
                with suppress(Exception):
                    await runtime.close()

    def _live_browsers_locked(self) -> int:
        """Browsers running or reserved: leases, remote ceremonies, a verification's two."""
        return (
            len(self._leases)
            + self._verification_browsers
            + sum(
                2 if state.mode is BrowserAuthenticationMode.DEVICE else 1
                for state in self._ceremonies.values()
                if state.runtime is not None or state.consumed
            )
        )

    def _admit_browsers_locked(self, count: int) -> None:
        """Refuse a browser the container has no room for, before it starts (ADR-0145)."""
        if self._live_browsers_locked() + count > MAXIMUM_LIVE_BROWSERS:
            raise BrowserProviderError("tool.browser.provider_unavailable", retryable=True)

    def _accepts_handoff_locked(self, state: _CeremonyState, capability: str) -> bool:
        return (
            state.mode is BrowserAuthenticationMode.DEVICE
            and not state.consumed
            and self._surface_live_locked(state, capability)
        )

    def _surface_live_locked(self, state: _CeremonyState, capability: str) -> bool:
        """A non-terminal, unexpired ceremony whose capability matches, in constant time."""
        return bool(
            state.status not in _TERMINAL_AUTH_STATUSES
            and state.expires_at > self._now()
            and 32 <= len(capability) <= 128
            and hmac.compare_digest(
                state.capability_digest,
                self._lookup_digest("ceremony:" + capability),
            )
        )

    async def authenticate_surface(
        self,
        ceremony_id: UUID,
        capability: str,
        operation: SurfaceOperation = "frame",
    ) -> bool:
        """Whether ``capability`` authorizes ``operation`` on this ceremony now.

        The headed surface's frame and events belong to remote ceremonies; the
        handoff belongs to an unspent device ceremony. Expiry is checked here
        too, whether or not a sweep has run.
        """
        if not 32 <= len(capability) <= 128:
            return False
        await self._expire()
        async with self._lock:
            state = self._ceremonies.get(ceremony_id)
            if state is None:
                return False
            if operation == "handoff":
                return self._accepts_handoff_locked(state, capability)
            return (
                state.mode is BrowserAuthenticationMode.REMOTE
                and state.runtime is not None
                and self._surface_live_locked(state, capability)
            )

    async def authentication_frame(
        self,
        ceremony_id: UUID,
        capability: str,
    ) -> bytes:
        await self._expire()
        async with self._lock:
            state, runtime = self._surface_runtime_locked(ceremony_id, capability)
        async with state.runtime_lock:
            async with self._lock:
                self._surface_runtime_locked(ceremony_id, capability)
            return await runtime.interactive_frame()

    async def authentication_event(
        self,
        ceremony_id: UUID,
        capability: str,
        event: BrowserInteractiveEvent,
    ) -> None:
        await self._expire()
        async with self._lock:
            state, runtime = self._surface_runtime_locked(ceremony_id, capability)
        async with state.runtime_lock:
            async with self._lock:
                self._surface_runtime_locked(ceremony_id, capability)
            await runtime.interactive_event(event)

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

    async def _require_lease(self, lease_ref: str) -> _LeaseState:
        await self._expire()
        invalid: _LeaseState | None = None
        async with self._lock:
            key, state = self._find_lease(lease_ref)
            if state is None or key is None:
                raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)
            metadata = await self._store.find_by_profile(state.scope.profile_id)
            if state.expires_at <= self._now() or metadata is None or metadata.revoked:
                self._leases.pop(key)
                invalid = state
            else:
                return state
        assert invalid is not None
        await self._close_lease_state(invalid)
        raise BrowserProviderError("tool.browser.profile_unavailable", retryable=False)

    async def _expire(self) -> None:
        expired_leases: list[_LeaseState] = []
        async with self._lock:
            now = self._now()
            for key, lease_state in tuple(self._leases.items()):
                if lease_state.expires_at <= now:
                    self._leases.pop(key)
                    expired_leases.append(lease_state)
            for _ceremony_id, ceremony_state in tuple(self._ceremonies.items()):
                # A verifying handoff is ended by its own budget, never by time.
                if ceremony_state.expires_at <= now and not ceremony_state.consumed:
                    ceremony_state.status = BrowserAuthenticationStatus.EXPIRED
                    await _close_ceremony_runtime(ceremony_state)
                    await self._finish_ceremony_locked(ceremony_state)
            for ceremony_id, terminal_state in tuple(self._terminal_ceremonies.items()):
                if terminal_state.retained_until <= now:
                    self._terminal_ceremonies.pop(ceremony_id, None)
        for lease_state in expired_leases:
            await self._close_lease_state(lease_state)

    @staticmethod
    async def _close_lease_state(state: _LeaseState) -> None:
        async with state.lock:
            state.closed = True
            with suppress(Exception):
                await state.runtime.close()

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
            retained_until=self._now() + timedelta(seconds=TERMINAL_CEREMONY_RETENTION_SECONDS),
            status=state.status,
        )
        self._terminal_ceremonies[state.id] = terminal
        while len(self._terminal_ceremonies) > MAXIMUM_TERMINAL_CEREMONIES:
            # Insertion order is finishing order: the oldest outcome goes first.
            self._terminal_ceremonies.pop(next(iter(self._terminal_ceremonies)))
        return _terminal_ceremony_view(terminal)

    def _surface_runtime_locked(
        self,
        ceremony_id: UUID,
        capability: str,
    ) -> tuple[_CeremonyState, BrowserSessionRuntime]:
        """The headed browser of a live remote ceremony whose capability matches."""
        state = self._ceremonies.get(ceremony_id)
        if (
            state is None
            or state.runtime is None
            or state.mode is not BrowserAuthenticationMode.REMOTE
            or not self._surface_live_locked(state, capability)
        ):
            raise ConflictError("browser authentication capability is invalid")
        return state, state.runtime

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


def _snapshot(runtime: BrowserSessionRuntime, observation: BrowserObservation) -> BrowserSnapshot:
    """The observation and, when the runtime has them, its element facts (ADR-0129)."""
    return BrowserSnapshot(
        observation=observation, facts=_bounded_facts(runtime.facts(observation.revision))
    )


def _bounded_facts(facts: BrowserObservationFacts | None) -> BrowserObservationFacts | None:
    """Keep facts, in element order, within MAXIMUM_FACTS_BYTES serialized.

    An element whose facts do not fit is dropped with every later one, never
    cut: an element without facts is never covered by a grant.
    """
    if facts is None:
        return None
    used = len(BrowserObservationFacts(revision=facts.revision).model_dump_json().encode())
    kept: dict[str, BrowserElementFacts] = {}
    for ref, element in facts.elements.items():
        # The key, its quotes and colon, the value, and a separating comma.
        size = len(json.dumps(ref).encode()) + 2 + len(element.model_dump_json().encode())
        if used + size > MAXIMUM_FACTS_BYTES:
            break
        kept[ref] = element
        used += size
    return BrowserObservationFacts(revision=facts.revision, elements=kept)


async def _page_evidence(
    runtime: BrowserSessionRuntime,
    material: bytes,
    origins: tuple[str, ...],
    url: str,
    progress: _VerificationLoad,
) -> BrowserPageEvidence:
    try:
        # Both loads use the browser a lease will use, so the control is fair (ADR-0145).
        runtime.set_interactive_authentication()
        await runtime.start(material, origins, headed=True)
        evidence = await runtime.load_page_evidence(url, on_stage=progress.enter)
    except Exception:
        # The deadline arrives as a cancellation, which is not a failure of this load.
        progress.failed = True
        raise
    progress.finished = True
    return evidence


def _log_unfinished_verification(
    progress: tuple[_VerificationLoad, ...], elapsed_seconds: float
) -> None:
    """Say which load stopped a verification, at which stage, after how long.

    That is the whole record (ADR-0128, amendment of 2026-10-06): load names, stage
    names and a number. A load that failed is named alone; otherwise every
    load the deadline left unfinished is named.
    """
    stopped = [load for load in progress if load.failed] or [
        load for load in progress if not load.finished
    ]
    named = " ".join(f"{load.name}={load.stage.value}" for load in stopped)
    _LOGGER.warning(
        "device verification did not finish (%s) after %.1f s",
        # Both pages loaded: what remained was reading the verifying browser's state.
        named or f"with_session={BrowserVerificationStage.CAPTURE.value}",
        elapsed_seconds,
    )


def _normalized_path(path: str) -> str:
    """Strip one trailing slash, except from the root; case-sensitive."""
    path = path or "/"
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


def _stays(evidence: BrowserPageEvidence, confirmed: str) -> bool:
    path = _normalized_path(evidence.path)
    return evidence.on_allowed_origin and (path == confirmed or path.startswith(confirmed + "/"))


def _decide(
    with_session: BrowserPageEvidence,
    without_session: BrowserPageEvidence,
    *,
    confirmed_path: str,
) -> None:
    """Ready only when the site tells the session apart (ADR-0128 section 4.4)."""
    confirmed = _normalized_path(confirmed_path)
    if with_session.challenge_visible or not _stays(with_session, confirmed):
        raise DeviceSessionRejected("session_signed_out")
    if not without_session.challenge_visible and _stays(without_session, confirmed):
        raise DeviceSessionRejected("session_unconfirmed")


async def _close_ceremony_runtime(state: _CeremonyState) -> None:
    if state.runtime is not None:
        with suppress(Exception):
            await state.runtime.close()


def _ceremony_capability() -> str:
    """256 random bits: a restarted service never issues one again (ADR-0128 D3)."""

    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


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
