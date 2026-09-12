"""Reusable contracts for evaluated email semantic adapters."""

from datetime import timedelta

import pytest

from agent_core.domain.email_semantics import EmailSemanticFact, EmailSemanticSource
from agent_core.domain.errors import ToolTrustRejectedError, ToolValidationError
from agent_core.domain.memory import MemoryAuthority, Portability, Sensitivity
from agent_core.ports.email import EmailSemanticPort
from tests.contract.memory_fixtures import semantic_stack


async def assert_email_semantic_port(
    service: EmailSemanticPort,
    source: EmailSemanticSource,
    fact: EmailSemanticFact,
) -> None:
    """Future adapters must preserve source authority, validity and replay identity."""
    assert service.enabled
    await service.register_source(source)
    await service.register_source(source)
    first = await service.form(source, [fact], scope="contract-project")
    assert len(first) == 1
    memory = first[0]
    assert memory.authority is MemoryAuthority.INFERRED
    assert memory.portability is Portability.CONTEXTUAL
    assert memory.sensitivity is Sensitivity.SENSITIVE
    assert memory.confidence <= 0.4
    assert memory.source_session_id == source.session_id
    assert memory.source_event_ids == [source.source_event_sequence]
    assert memory.valid_from == memory.last_evidence_at == source.sent_at
    assert memory.expires_at == source.sent_at + timedelta(days=30)
    assert source.sender in memory.statement and fact.value in memory.statement
    assert await service.form(source, [fact], scope="contract-project") == first
    with pytest.raises(ToolTrustRejectedError):
        await service.register_source(source.model_copy(update={"account_id": "unrelated"}))
    with pytest.raises(ToolValidationError):
        await service.form(source, [fact.model_copy(update={"quote": "Unobserved assertion"})])


async def assert_disabled_email_semantic_port(
    service: EmailSemanticPort,
    source: EmailSemanticSource,
    fact: EmailSemanticFact,
) -> None:
    """Registration may retain provenance while unevaluated formation stays off."""
    assert not service.enabled
    await service.register_source(source)
    assert await service.form(source, [fact]) == []


async def test_governed_email_semantics_satisfies_shared_port_contract() -> None:
    _, concrete, source, fact, _ = await semantic_stack(age=400)
    service: EmailSemanticPort = concrete
    await assert_email_semantic_port(service, source, fact)


async def test_unevaluated_email_semantics_satisfies_disabled_port_contract() -> None:
    _, concrete, source, fact, _ = await semantic_stack(enabled=False)
    service: EmailSemanticPort = concrete
    await assert_disabled_email_semantic_port(service, source, fact)
