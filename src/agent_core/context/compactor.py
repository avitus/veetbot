"""Bounded structured compaction with provenance and untrusted-span elision."""

from __future__ import annotations

from typing import assert_never

from agent_core.context.history import select_history
from agent_core.domain.context import CompactionResult, ContextBudget
from agent_core.domain.errors import ContextOverflow
from agent_core.domain.messages import (
    AssistantMessage,
    ConversationItem,
    FileReferencePart,
    ProviderReasoningItem,
    SystemMessage,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.provenance import ElidedSpan
from agent_core.domain.runs import RunCheckpoint
from agent_core.ports.context import TokenEstimator

COMPACTOR_VERSION = "structured-extractive@1"


def _trust(item: ConversationItem) -> TrustLevel:
    if isinstance(item, (SystemMessage, UserMessage, AssistantMessage, ToolResultItem)):
        return item.trust
    if isinstance(item, ProviderReasoningItem):
        return item.trust_level
    if isinstance(item, ToolCallItem):
        return TrustLevel.EXTERNAL_UNTRUSTED
    assert_never(item)


def _sequence(item: ConversationItem) -> int | None:
    return getattr(item, "source_event_sequence", None)


def _text(item: ConversationItem) -> str:
    if not isinstance(item, (UserMessage, AssistantMessage, ToolResultItem)):
        return ""
    return "\n".join(part.text for part in item.content if isinstance(part, TextPart))


def _artifact(item: ConversationItem) -> str | None:
    if not isinstance(item, (UserMessage, AssistantMessage, ToolResultItem)):
        return None
    for part in item.content:
        if isinstance(part, FileReferencePart):
            return f"artifact:{part.artifact_id}"
    return None


class StructuredCompactor:
    def __init__(self, estimator: TokenEstimator, *, maximum_depth: int = 2) -> None:
        self._estimator = estimator
        self._maximum_depth = maximum_depth

    async def compact(
        self,
        checkpoint: RunCheckpoint,
        budget: ContextBudget,
        reason: str,
    ) -> tuple[RunCheckpoint, CompactionResult]:
        del reason
        depth = checkpoint.summary_depth + 1
        if depth > self._maximum_depth:
            raise ContextOverflow("context summary depth cap reached")
        items = checkpoint.conversation
        if not items:
            raise ContextOverflow("context pressure has no history to compact")
        raw_model_id = checkpoint.budget_state.get("context_model_id")
        if not isinstance(raw_model_id, str) or not raw_model_id:
            raise ContextOverflow("checkpoint has no context model identifier")
        model_id = raw_model_id
        tokens_before = self._estimator.estimate(items, model_id)
        raw_seed = checkpoint.budget_state.get("context_seed_event_sequence")
        if not isinstance(raw_seed, int) or isinstance(raw_seed, bool) or raw_seed <= 0:
            raise ContextOverflow("checkpoint has no context seed event sequence")
        seed_sequence = raw_seed

        def is_boundary(item: ConversationItem) -> bool:
            sequence = _sequence(item)
            if sequence is None:
                return True
            return seed_sequence is not None and sequence >= seed_sequence

        compactable_end = next(
            (index for index, item in enumerate(items) if is_boundary(item)),
            len(items),
        )
        compactable_items = items[:compactable_end]
        if not compactable_items:
            raise ContextOverflow("context pressure has no inactive history to compact")
        cut = select_history(
            compactable_items,
            checkpoint.replaced_through_sequence,
            budget.history_tokens,
            self._estimator,
            model_id,
        )
        if cut <= 0:
            raise ContextOverflow("history pressure could not identify a compactable prefix")
        compacted = list(items[:cut])
        assert all(_sequence(item) is not None for item in compacted)

        source_ids = set(checkpoint.summary_source_event_ids)
        elided = [item.model_copy(deep=True) for item in checkpoint.summary_elided]
        lines: list[str] = []
        if checkpoint.compacted_summary:
            lines.append(checkpoint.compacted_summary)
        for item in compacted:
            sequence = _sequence(item)
            assert sequence is not None
            source_ids.add(sequence)
            trust = _trust(item)
            raw_text = _text(item)
            if trust is TrustLevel.EXTERNAL_UNTRUSTED:
                span = ElidedSpan(
                    item_id=(
                        item.call_id
                        if isinstance(item, ToolResultItem)
                        else f"{item.kind}:event:{sequence}"
                    ),
                    trust_level=trust,
                    byte_length=len(raw_text.encode("utf-8")),
                    artifact_ref=_artifact(item),
                    event_id=sequence,
                )
                elided.append(span)
                lines.append(
                    f"[elided] {item.kind} {span.item_id} ({trust.value}, "
                    f"{span.byte_length} bytes) -> {span.artifact_ref or f'event:{sequence}'}"
                )
                continue
            if raw_text:
                excerpt = raw_text if len(raw_text) <= 600 else raw_text[:600] + "…"
                lines.append(f"[event:{sequence} trust:{trust.value}] {excerpt}")
        if not lines:
            lines.append("No compactable textual content; provenance retained in source ids.")
        summary = "Structured context summary:\n" + "\n".join(lines)
        summary_item = UserMessage(
            content=[TextPart(text=summary)],
            trust=TrustLevel.PLATFORM,
            principal_id=None,
        )
        replaced = max(source_ids, default=checkpoint.replaced_through_sequence)
        result = CompactionResult(
            summary=summary,
            source_event_ids=tuple(sorted(source_ids)),
            elided=tuple(elided),
            replaced_through_sequence=replaced,
            depth=depth,
            tokens_before=tokens_before,
            tokens_after=self._estimator.estimate(items[cut:], model_id)
            + self._estimator.estimate([summary_item], model_id),
            compactor_version=COMPACTOR_VERSION,
        )
        updated = checkpoint.model_copy(
            update={
                "conversation": [item.model_copy(deep=True) for item in items[cut:]],
                "compacted_summary": summary,
                "summary_source_event_ids": list(result.source_event_ids),
                "summary_elided": [item.model_copy(deep=True) for item in result.elided],
                "replaced_through_sequence": result.replaced_through_sequence,
                "summary_depth": result.depth,
                "compactor_version": result.compactor_version,
            },
            deep=True,
        )
        return updated, result
