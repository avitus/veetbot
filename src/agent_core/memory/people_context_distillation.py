"""Context-preserving owner extraction outside the frozen evaluation implementation."""

import json
import re
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import (
    EvidenceSpan,
    IntegratedEpisode,
    MemoryCandidate,
    MemoryExtractionResult,
)
from agent_core.domain.people_extraction import InteractionEvidence
from agent_core.memory.distillation import (
    PeopleAssistedCandidateExtractor,
    _AnticipationResponse,
    _CoverageDisposition,
    _DistillationResponse,
    _DistilledCandidate,
    _PeopleDistilledCandidate,
    owned_user_events,
)
from agent_core.memory.formation import _event_text

_CONTEXT: ContextVar[tuple[EventEnvelope, ...]] = ContextVar("formation_context", default=())


def _require_context(schema: dict[str, Any]) -> None:
    """Require live model output while allowing older recorded responses to parse."""
    schema.setdefault("required", []).append("context_anchors")


class _ContextualCandidate(_PeopleDistilledCandidate):
    model_config = ConfigDict(extra="forbid", json_schema_extra=_require_context)
    context_anchors: list[EvidenceSpan] = Field(default_factory=list, max_length=8)


class _ContextualResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[_ContextualCandidate] = Field(max_length=32)
    coverage: list[_CoverageDisposition] = Field(min_length=1, max_length=512)
    interactions: list[InteractionEvidence] = Field(max_length=32)


class ContextualPeopleCandidateExtractor(PeopleAssistedCandidateExtractor):
    """Use bounded owner context with exact citations and per-invocation isolation."""

    async def extract(
        self, events: list[EventEnvelope], *, principal: Principal, scope: str
    ) -> list[MemoryCandidate] | MemoryExtractionResult:
        events = await self._eligible_events(events, principal)
        selected = owned_user_events(events, principal)
        context: list[EventEnvelope] = []
        if selected and not self._source_isolated:
            async with self._uow_factory() as uow:
                history = await uow.events.list_after(
                    selected[0].session_id, max(0, selected[0].sequence - 257), principal, limit=256
                )
            eligible = await self._eligible_events(owned_user_events(history, principal), principal)
            context_bytes = 0
            for event in reversed(eligible):
                if event.sequence >= selected[0].sequence:
                    continue
                size = len(_event_text(event).encode("utf-8"))
                if len(context) == 8 or context_bytes + size > 8192:
                    break
                context.append(event)
                context_bytes += size
            context.reverse()
        token = _CONTEXT.set(tuple(context))
        try:
            result = await super().extract(events, principal=principal, scope=scope)
            if isinstance(result, MemoryExtractionResult):
                result.context_event_ids = tuple(event.sequence for event in context)
            return result
        finally:
            _CONTEXT.reset(token)

    @staticmethod
    def _distillation_schema() -> dict[str, Any]:
        return _ContextualResponse.model_json_schema()

    @staticmethod
    def _parse_distillation(
        raw: str,
    ) -> tuple[_DistillationResponse, list[InteractionEvidence], int]:
        data = json.loads(raw)
        # Keep the original People parser's independent interaction validation,
        # candidate count, mention limits and coverage contract unchanged.
        if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
            raise ValueError("contextual distillation requires candidates")
        candidates = [_ContextualCandidate.model_validate(item) for item in data["candidates"]]
        legacy = {
            **data,
            "candidates": [
                {k: v for k, v in item.items() if k != "context_anchors"}
                for item in data["candidates"]
            ],
        }
        response, interactions, rejected = PeopleAssistedCandidateExtractor._parse_distillation(
            json.dumps(legacy)
        )
        return response.model_copy(update={"candidates": candidates}), interactions, rejected

    @staticmethod
    def _distillation_prompt(
        events: Sequence[EventEnvelope],
        episodes: Sequence[IntegratedEpisode],
        anticipation: _AnticipationResponse,
        scope: str,
    ) -> str:
        payload = json.loads(
            PeopleAssistedCandidateExtractor._distillation_prompt(
                events, episodes, anticipation, scope
            )
        )
        payload["context_events"] = [
            {"source_event_id": event.sequence, "text": _event_text(event)}
            for event in _CONTEXT.get()
        ]
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _normalize_candidate(
        candidate: _DistilledCandidate, *, by_sequence: dict[int, EventEnvelope], scope: str
    ) -> MemoryCandidate:
        if isinstance(candidate, _ContextualCandidate):
            for anchor in candidate.context_anchors:
                if (
                    anchor.source_event_id not in candidate.source_event_ids
                    or not any(
                        span.source_event_id == anchor.source_event_id and anchor.text in span.text
                        for span in candidate.evidence_spans
                    )
                    or not re.search(
                        rf"(?<!\w){re.escape(anchor.text)}(?!\w)",
                        candidate.statement,
                        re.IGNORECASE,
                    )
                ):
                    raise ValueError("context anchor must be cited and preserved in the statement")
        sources = {event.sequence: event for event in _CONTEXT.get()}
        return PeopleAssistedCandidateExtractor._normalize_candidate(
            candidate, by_sequence={**sources, **by_sequence}, scope=scope
        )

    @staticmethod
    def _instructions(stage: str) -> str:
        original = PeopleAssistedCandidateExtractor._instructions(stage)
        if stage != "prediction_error_distillation":
            return original
        return original + (
            " Every statement must stand alone outside the conversation. Preserve which app, "
            "product, project, person or service a report concerns, and any time qualification. "
            "Never turn a complaint about one app into a general user state. Resolve omitted "
            "subjects only from unambiguous owner source_events or context_events. Return "
            "context_anchors containing exact name spans for every referent needed to understand "
            "the claim, or [] when none is needed. Include each anchor verbatim in the statement "
            "and cite its event in source_event_ids and evidence_spans alongside the observation. "
            "Context events are earlier owner messages, not new facts to re-extract: every "
            "candidate must also cite current source_events. Questions may establish the topic "
            "but never prove an assertion in the question. If the referent is ambiguous, "
            "do not guess."
        )
