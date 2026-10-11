"""Owner-confirmed inputs for the isolated ADR-0171 experiment, never bank replay."""

import hashlib
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from agent_core.domain.agents import Principal
from agent_core.domain.memory import Portability, Sensitivity
from agent_core.domain.messages import ResolvedModel
from agent_core.domain.reconsolidation import ReconValue
from agent_core.domain.reconsolidation_inputs import EgressSubject
from agent_core.memory.reconsolidation_policy import local_egress_policy


class FixtureSource(ReconValue):
    reference_id: UUID
    subject: str = Field(min_length=1, max_length=512)
    statement: str = Field(min_length=1, max_length=2048)
    scope: str = Field(min_length=1, max_length=512)
    portability: Portability
    sensitivity: Sensitivity
    status: Literal["active", "provisional", "unavailable"]
    excluded: bool = False
    # Context is displayed locally only; it is not substituted for confirmed input.
    retained_source_text: tuple[str, ...] = ()


class FixturePacket(ReconValue):
    version: Literal["owner-memory-fixture@1"] = "owner-memory-fixture@1"
    experiment_id: UUID
    owner_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    prepared_at: AwareDatetime
    model: ResolvedModel
    residency_provider: str
    sources: tuple[FixtureSource, ...] = Field(min_length=2, max_length=16)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class FixtureConfirmation(ReconValue):
    packet_digest: str
    reference_id: UUID
    confirmed_at: AwareDatetime
    human_confirmed: Literal[True]


def validate_confirmation(
    packet: FixturePacket,
    confirmations: tuple[FixtureConfirmation, ...],
    *,
    owner_digest: str,
    model: ResolvedModel,
    submitted_at: datetime,
    now: datetime,
) -> None:
    """Validate all human input before any provider or reservation is reachable."""
    # Revalidate even model_copy values; mutable model fields are included in the digest.
    packet = FixturePacket.model_validate_json(packet.model_dump_json())
    if owner_digest != packet.owner_digest or model != packet.model:
        raise ValueError("confirmation owner/model mismatch")
    if (
        submitted_at.tzinfo is None
        or not timedelta(0) <= now - submitted_at <= timedelta(minutes=30)
        or submitted_at < packet.prepared_at
    ):
        raise ValueError("confirmation submission is stale or future")
    expected = {source.reference_id for source in packet.sources}
    selected = {item.reference_id for item in confirmations}
    if (
        len(expected) != len(packet.sources)
        or len(selected) != len(confirmations)
        or len(selected) < 2
    ):
        raise ValueError("at least two distinct confirmations required")
    if not selected <= expected:
        raise ValueError("confirmation sources mismatch")
    for item in confirmations:
        if (
            item.human_confirmed is not True
            or item.packet_digest != packet.digest
            or not packet.prepared_at <= item.confirmed_at <= submitted_at
        ):
            raise ValueError("confirmation is stale or changed")
    for source in packet.sources:
        if source.reference_id not in selected:
            continue
        validate_fixture_source(packet, source, now)


def validate_fixture_source(packet: FixturePacket, source: FixtureSource, now: datetime) -> None:
    if source.excluded or source.status == "unavailable" or len(source.statement.encode()) > 2048:
        raise ValueError("source unavailable or oversized")
    owner = Principal(
        tenant_id="offline-fixture", principal_id=packet.owner_digest, roles=set(), scopes=set()
    )
    policy = local_egress_policy(packet.residency_provider)
    # Metadata is also exported by the existing request builder.
    for content in (source.subject, source.statement):
        subject = EgressSubject(
            kind="memory",
            id=source.reference_id,
            text=content,
            sensitivity_floor=source.sensitivity,
            scope=source.scope,
            portability=source.portability,
            attribution=(("speaker", "owner"),),
        )
        if not policy(owner, packet.model, subject, now).permitted:
            raise ValueError("source egress denied")
