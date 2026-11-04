"""Knowledge ingestion, retrieval, citation, version, and deletion service."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import timedelta
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.artifacts import StoredArtifactRef
from agent_core.domain.embeddings import EmbeddingError
from agent_core.domain.errors import ArtifactIntegrityError, NotFoundError, ToolValidationError
from agent_core.domain.events import NewEvent
from agent_core.domain.knowledge import (
    KnowledgeChunkEmbedding,
    KnowledgeDocument,
    KnowledgeIngestPrepared,
    KnowledgeIngestRequest,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeSemanticQuery,
    RetrievedPassage,
    knowledge_question_without_document_names,
)
from agent_core.domain.memory import (
    RecallMoment,
    RecallProfile,
    RecallQuery,
    RecallTrace,
    Sensitivity,
    TracedPassage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.knowledge.chunking import (
    MAX_KNOWLEDGE_SOURCE_BYTES,
    DeterministicChunker,
    PlainTextExtractor,
    normalize_text,
    token_estimate,
)
from agent_core.knowledge.embedding import KnowledgeAdmissionWithdrawnError, KnowledgeEmbedder
from agent_core.memory.profiles import DEFAULT_TRACE_PROFILE, TraceProfile
from agent_core.ports.artifacts import ArtifactStore
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.knowledge import Extractor
from agent_core.ports.persistence import UnitOfWorkFactory

KNOWLEDGE_POLICY_VERSION = "knowledge@1"
logger = logging.getLogger(__name__)
_SECRET = re.compile(
    r"(?:api[_-]?key|secret|password|token|authorization|credential|bearer)\s*[:=]\s*\S+",
    re.I,
)


class KnowledgeService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        artifacts: ArtifactStore,
        clock: Clock,
        ids: IdFactory,
        principal: Principal,
        *,
        extractor: Extractor | None = None,
        chunker: DeterministicChunker | None = None,
        trace_retention: TraceProfile = DEFAULT_TRACE_PROFILE,
        embedder: KnowledgeEmbedder | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifacts = artifacts
        self._clock = clock
        self._ids = ids
        self._principal = principal
        self._extractor = extractor or PlainTextExtractor()
        self._chunker = chunker or DeterministicChunker()
        self._trace_retention = trace_retention
        # The semantic arm (knowledge-documents.md, "Indexing"): present only
        # when the composition turned it on with an admitted provider.
        self._embedder = embedder

    @property
    def trace_retention(self) -> TraceProfile:
        """Expose the trace-retention profile the composition wired in."""

        return self._trace_retention

    async def ingest(
        self, request: KnowledgeIngestRequest, *, origin_trust: TrustLevel
    ) -> KnowledgeDocument:
        if origin_trust is not TrustLevel.USER:
            raise ToolValidationError("knowledge ingestion requires USER origin trust")
        source = request.source
        if source.tenant_id != self._principal.tenant_id or (
            source.principal_id != self._principal.principal_id
        ):
            raise ToolValidationError("knowledge source is outside the caller scope")
        if source.media_type not in self._extractor.media_types():
            raise ToolValidationError(f"unsupported knowledge media type {source.media_type!r}")
        if source.size_bytes > MAX_KNOWLEDGE_SOURCE_BYTES:
            raise ToolValidationError("knowledge source exceeds the byte ceiling")
        stored = StoredArtifactRef(
            artifact_id=source.id,
            sha256=source.sha256,
            size_bytes=source.size_bytes,
            media_type=source.media_type,
        )
        stream = await self._artifacts.open_verified(stored, tenant_id=source.tenant_id)
        extracted = await self._extractor.extract(stream, source.media_type)
        normalized = normalize_text(extracted)
        if not normalized:
            raise ToolValidationError("knowledge source has no extractable text")
        if _SECRET.search(normalized) is not None:
            raise ToolValidationError("knowledge source failed the secret scan")
        document_id = request.document_id or self._ids.new_id()
        # Chunk texts do not depend on the row that will hold them, so the
        # provider call happens here, before any unit of work is open.
        windows = await self._embed_chunk_texts(
            normalized, request.title, document_id, request.sensitivity
        )
        async with self._uow_factory() as uow:
            latest = await uow.knowledge.latest(source.tenant_id, document_id)
            if (
                latest is not None
                and latest.ingested_by_principal_id != self._principal.principal_id
            ):
                raise NotFoundError("knowledge document not found")
            if latest is not None and _same_ingest(latest, request):
                return latest
            version = 1 if latest is None else latest.version + 1
            row_id = self._ids.new_id()
            retained_source = source.model_copy(
                update={"origin": "knowledge_source", "expires_at": None}, deep=True
            )
            now = self._clock.now()
            document = KnowledgeDocument(
                row_id=row_id,
                document_id=document_id,
                tenant_id=source.tenant_id,
                ingested_by_principal_id=self._principal.principal_id,
                visibility=request.visibility,
                project_scope=request.project_scope,
                title=request.title,
                source_ref=retained_source,
                media_type=source.media_type,
                doc_date=request.doc_date,
                authority=request.authority,
                version=version,
                chunker_version=self._chunker.version,
                valid_from=now,
                ingested_at=now,
                sensitivity=request.sensitivity,
            )
            chunks = self._chunker.chunk(
                normalized,
                request.title,
                document_row_id=row_id,
                document_id=document_id,
                version=version,
            )
            if not chunks:
                raise ToolValidationError("knowledge source produced no chunks")
            retained = await uow.artifacts.retain_for_knowledge(
                source.id, self._principal, now=self._clock.now()
            )
            if retained.sha256 != source.sha256:
                raise ArtifactIntegrityError("knowledge artifact metadata changed during ingest")
            embeddings: tuple[KnowledgeChunkEmbedding, ...] = ()
            if windows is not None:
                assert self._embedder is not None
                embedder = self._embedder
                if [[text for text, _ in chunk_windows] for chunk_windows in windows] != [
                    embedder.window_texts(c.heading_path, c.text) for c in chunks
                ]:
                    raise ArtifactIntegrityError(
                        "knowledge chunks changed between embedding and ingest"
                    )
                embeddings = tuple(
                    KnowledgeChunkEmbedding(
                        chunk_id=chunk.chunk_id, space=embedder.space, window=index, vector=vector
                    )
                    for chunk, chunk_windows in zip(chunks, windows, strict=True)
                    for index, (_, vector) in enumerate(chunk_windows)
                )
            await uow.knowledge.ingest(
                KnowledgeIngestPrepared(document=document, chunks=chunks, embeddings=embeddings)
            )
            await uow.events.append(
                NewEvent(
                    session_id=source.session_id,
                    run_id=source.run_id,
                    event_type="knowledge.document.ingested",
                    actor_type="principal",
                    actor_id=self._principal.principal_id,
                    payload={
                        "document_id": str(document_id),
                        "version": version,
                        "chunks": len(chunks),
                        # Chunks with a vector, and the windows those vectors embed.
                        "embedded": len({embedding.chunk_id for embedding in embeddings}),
                        "windows": len(embeddings),
                    },
                )
            )
        return document

    async def _embed_chunk_texts(
        self, normalized: str, title: str, document_id: UUID, sensitivity: Sensitivity
    ) -> list[list[tuple[str, tuple[float, ...]]]] | None:
        """Embed the windows of the chunks the deterministic chunker will produce.

        The chunker binds ids to the row and version chosen inside the unit of
        work, but a chunk's heading path and text depend only on the bytes, so
        the window texts embedded here are the texts the stored chunks will
        carry; ingestion checks that before writing. The result lists, per
        chunk, each window's text with its vector. A document the provider
        may not see, or an admission that has been withdrawn, leaves the
        document lexical rather than refusing it. An embedding failure fails
        the ingest, as every other step does: a document half of whose
        passages the semantic arm can see is worse than one that is tried
        again.
        """

        if self._embedder is None:
            return None
        if not self._embedder.allows(sensitivity) or not self._embedder.admitted():
            logger.info(
                "knowledge_embedding_withheld",
                extra={
                    "document_id": str(document_id),
                    "reason": "sensitivity"
                    if not self._embedder.allows(sensitivity)
                    else "admission",
                },
            )
            return None
        drafts = self._chunker.chunk(
            normalized, title, document_row_id=UUID(int=0), document_id=document_id, version=1
        )
        windows = [self._embedder.window_texts(chunk.heading_path, chunk.text) for chunk in drafts]
        texts = [text for chunk_windows in windows for text in chunk_windows]
        if not texts:
            return None
        try:
            embedded = await self._embedder.embed_texts(texts)
        except KnowledgeAdmissionWithdrawnError:
            # Withdrawn between batches: the document stays lexical, as it
            # would had the withdrawal come a moment earlier.
            logger.info(
                "knowledge_embedding_withheld",
                extra={"document_id": str(document_id), "reason": "admission"},
            )
            return None
        vectors = iter(embedded)
        return [[(text, next(vectors)) for text in chunk_windows] for chunk_windows in windows]

    async def _run_provider(self, run_id: UUID | None) -> str | None:
        """The provider the run is pinned to; None outside a run or before its first model call."""

        if run_id is None or self._embedder is None or self._embedder.local:
            return None
        async with self._uow_factory() as uow:
            try:
                run = await uow.runs.get(run_id, self._principal)
            except NotFoundError:
                return None
        return None if run.provider_pin is None else run.provider_pin.provider

    async def _embed_question(
        self, text: str, reader_provider: str | None
    ) -> KnowledgeSemanticQuery | None:
        """Embed the question, or answer with the lexical arm alone when the provider cannot.

        A remote provider receives the question only when it is the run's
        own provider (`KnowledgeEmbedder.may_embed_question`).
        """

        if (
            self._embedder is None
            or not self._embedder.admitted()
            or not self._embedder.may_embed_question(reader_provider)
        ):
            return None
        try:
            return await self._embedder.embed_query(text)
        except KnowledgeAdmissionWithdrawnError:
            return None
        except EmbeddingError as error:
            # The message is content-free by contract; the question itself stays out.
            logger.warning("knowledge_semantic_query_unavailable", extra={"reason": str(error)})
            return None
        except ValueError as error:
            # A validation message can echo the question; only the class is logged.
            logger.warning(
                "knowledge_semantic_query_unavailable",
                extra={"reason": type(error).__name__},
            )
            return None

    async def search(
        self,
        query: KnowledgeQuery,
        *,
        session_id: UUID,
        run_id: UUID | None = None,
        turn_id: UUID | None = None,
        surface_id: str = "private",
    ) -> KnowledgeResult:
        authorized = query.tenant_id == self._principal.tenant_id and (
            query.principal_id == self._principal.principal_id
        )
        asked = query
        if authorized:
            # A question that names a document aims at the catalogue, not
            # the corpus: the name drops out before either arm runs, and
            # the trace keeps the question as asked (ADR-0293).
            async with self._uow_factory() as uow:
                titles = await uow.knowledge.visible_titles(query)
            text = knowledge_question_without_document_names(query.text, titles)
            if text != query.text:
                query = query.model_copy(update={"text": text})
        semantic = (
            await self._embed_question(query.text, await self._run_provider(run_id))
            if authorized
            else None
        )
        policy_version = KNOWLEDGE_POLICY_VERSION
        if semantic is not None:
            policy_version += f"+semantic@1:{semantic.space.key}:{semantic.channel}"
        async with self._uow_factory() as uow, uow.people.lock(self._principal):
            if not authorized:
                passages: list[RetrievedPassage] = []
            else:
                passages = await uow.knowledge.search(query, semantic=semantic)
            selected: list[RetrievedPassage] = []
            tokens = 0
            for passage in passages:
                cost = token_estimate(_passage_xml(passage))
                if len(selected) >= query.max_passages or tokens + cost > query.budget_tokens:
                    continue
                selected.append(passage)
                tokens += cost
            rendered = render_knowledge(selected, as_of=query.as_of or self._clock.now())
            trace_id = self._ids.new_id()
            recall_query = RecallQuery(
                tenant_id=self._principal.tenant_id,
                principal_id=self._principal.principal_id,
                current_scope=query.current_scope or "general",
                text=asked.text,
                profile=RecallProfile.TASK,
                budget_tokens=query.budget_tokens,
                max_items=query.max_passages,
                min_score=query.min_score,
                sensitivity_ceiling=query.sensitivity_ceiling,
            )
            trace = RecallTrace(
                id=trace_id,
                tenant_id=self._principal.tenant_id,
                principal_id=self._principal.principal_id,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                moment=RecallMoment.IN_TURN,
                query=recall_query,
                surface_id=surface_id,
                sensitivity_ceiling=query.sensitivity_ceiling,
                rendered=rendered,
                rendered_sha256=hashlib.sha256(rendered.encode()).hexdigest(),
                candidates=len(passages),
                passages=[
                    TracedPassage(
                        chunk_id=passage.chunk_id,
                        document_id=passage.document_id,
                        title=passage.title,
                        heading_path=list(passage.heading_path),
                        text=passage.text,
                        sensitivity=passage.sensitivity,
                    )
                    for passage in selected
                ],
                retrieval_policy_version=policy_version,
                created_at=self._clock.now(),
                operator_fields_expire_at=(
                    self._clock.now()
                    + timedelta(days=self._trace_retention.operator_retention_days)
                ),
            )
            await uow.traces.record(trace)
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=run_id,
                    event_type="knowledge.passages.retrieved",
                    actor_type="knowledge",
                    payload={
                        "trace_id": str(trace_id),
                        "chunks": [passage.chunk_id for passage in selected],
                    },
                )
            )
            return KnowledgeResult(
                passages=selected,
                rendered=rendered,
                tokens=token_estimate(rendered),
                truncated=len(selected) < len(passages),
                trace_id=trace_id,
            )

    async def delete(self, document_id: UUID) -> None:
        async with self._uow_factory() as uow, uow.people.lock(self._principal):
            sources = await uow.knowledge.delete(document_id, self._principal)
            await uow.traces.mark_document_deleted(self._principal.tenant_id, document_id)
            for source in sources:
                await uow.artifacts.expire(source.id, self._principal, self._clock.now())
            if sources:
                await uow.events.append(
                    NewEvent(
                        session_id=sources[0].session_id,
                        run_id=sources[0].run_id,
                        event_type="knowledge.document.deleted",
                        actor_type="principal",
                        actor_id=self._principal.principal_id,
                        payload={"document_id": str(document_id)},
                    )
                )


def render_knowledge(passages: list[RetrievedPassage], *, as_of: object) -> str:
    stamp = as_of.isoformat().replace("+00:00", "Z") if hasattr(as_of, "isoformat") else str(as_of)
    lines = [f'<knowledge as_of="{html.escape(stamp)}" policy="{KNOWLEDGE_POLICY_VERSION}">']
    lines.extend(f"  {_passage_xml(passage)}" for passage in passages)
    lines.append("</knowledge>")
    return "\n".join(lines)


def _passage_xml(passage: RetrievedPassage) -> str:
    date_value = "" if passage.doc_date is None else passage.doc_date.isoformat()
    path = " > ".join(passage.heading_path)
    return (
        f'<passage doc="{html.escape(passage.title, quote=True)}" '
        f'path="{html.escape(path, quote=True)}" chunk="{passage.chunk_id}" '
        f'doc_date="{date_value}" instruction_like="{str(passage.instruction_like).lower()}">'
        f"{html.escape(passage.text)}</passage>"
    )


def _same_ingest(document: KnowledgeDocument, request: KnowledgeIngestRequest) -> bool:
    return (
        document.source_ref.id == request.source.id
        and document.source_ref.sha256 == request.source.sha256
        and document.title == request.title
        and document.visibility is request.visibility
        and document.project_scope == request.project_scope
        and document.doc_date == request.doc_date
        and document.authority is request.authority
        and document.sensitivity is request.sensitivity
    )
