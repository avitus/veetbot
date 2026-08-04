"""Shared deterministic values for memory and knowledge port contracts."""

import hashlib
from datetime import datetime, timedelta
from uuid import UUID

from agent_core.domain.knowledge import (
    DocumentAuthority,
    KnowledgeDocument,
    KnowledgeIngestPrepared,
    KnowledgeVisibility,
)
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    Portability,
    RecallMoment,
    RecallProfile,
    RecallQuery,
    RecallTrace,
    Sensitivity,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef
from agent_core.knowledge.chunking import DeterministicChunker
from tests.contract.support import NOW, PRINCIPAL_ID, RUN_ID, SESSION_ID, TENANT


def memory(
    *, belief_id: int = 501, statement: str = "User prefers concise answers"
) -> MemoryRecord:
    return MemoryRecord(
        id=UUID(int=belief_id),
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        scope="project-a",
        subject="answer style",
        statement=statement,
        source_session_id=SESSION_ID,
        source_event_ids=[1],
        confidence=0.9,
        sensitivity=Sensitivity.INTERNAL,
        valid_from=NOW,
        status=MemoryStatus.ACTIVE,
        belief_type=BeliefType.PREFERENCE,
        polarity=Polarity.ASSERT,
        portability=Portability.PORTABLE,
        origin_scopes=["project-a"],
        corroboration_count=1,
        last_reinforced_at=NOW,
        formation_run_id=UUID(int=502),
        consolidation_policy_version="formation@1",
        authority=MemoryAuthority.USER,
        store_position=1,
        created_at=NOW,
        updated_at=NOW,
    )


def recall_query(
    *,
    tenant_id: str = TENANT,
    principal_id: str = PRINCIPAL_ID,
    current_scope: str = "project-a",
    text: str | None = "concise answers",
    subjects: list[str] | None = None,
    belief_types: list[BeliefType] | None = None,
    as_of: datetime | None = None,
    include_superseded: bool = False,
    profile: RecallProfile = RecallProfile.TASK,
    budget_tokens: int = 500,
    max_items: int = 10,
    min_score: float = 0.1,
    sensitivity_ceiling: Sensitivity = Sensitivity.RESTRICTED,
) -> RecallQuery:
    return RecallQuery(
        tenant_id=tenant_id,
        principal_id=principal_id,
        current_scope=current_scope,
        text=text,
        subjects=[] if subjects is None else subjects,
        belief_types=[] if belief_types is None else belief_types,
        as_of=as_of,
        include_superseded=include_superseded,
        profile=profile,
        budget_tokens=budget_tokens,
        max_items=max_items,
        min_score=min_score,
        sensitivity_ceiling=sensitivity_ceiling,
    )


def trace() -> RecallTrace:
    return RecallTrace(
        id=UUID(int=503),
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        turn_id=RUN_ID,
        moment=RecallMoment.IN_TURN,
        query=recall_query(),
        surface_id="private",
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        rendered="<memory></memory>",
        rendered_sha256=hashlib.sha256(b"<memory></memory>").hexdigest(),
        candidates=0,
        retrieval_policy_version="retrieval@1",
        created_at=NOW,
        operator_fields_expire_at=NOW + timedelta(days=30),
    )


def prepared_knowledge() -> KnowledgeIngestPrepared:
    source = ArtifactRef(
        id=UUID(int=510),
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        name="guide.md",
        media_type="text/markdown",
        storage_uri="artifact://guide",
        sha256="1" * 64,
        size_bytes=32,
        origin="knowledge_source",
        trust=TrustLevel.USER,
        expires_at=None,
        created_at=NOW,
    )
    document = KnowledgeDocument(
        row_id=UUID(int=511),
        document_id=UUID(int=512),
        tenant_id=TENANT,
        ingested_by_principal_id=PRINCIPAL_ID,
        visibility=KnowledgeVisibility.PRINCIPAL,
        title="Guide",
        source_ref=source,
        media_type=source.media_type,
        authority=DocumentAuthority.PRINCIPAL_SUPPLIED,
        version=1,
        chunker_version="knowledge-chunker@1",
        valid_from=NOW,
        ingested_at=NOW,
        sensitivity=Sensitivity.INTERNAL,
    )
    chunks = DeterministicChunker().chunk(
        "# Operations\n\nRestart the service safely after deployment.",
        document.title,
        document_row_id=document.row_id,
        document_id=document.document_id,
        version=1,
    )
    return KnowledgeIngestPrepared(document=document, chunks=chunks)
