"""Select whole deliberate results before spending the remaining automatic allowance."""

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from agent_core.domain.memory import RecallQuery, RecallResult
from agent_core.domain.messages import (
    ContextRecallRanking,
    ConversationItem,
    MemoryRecallUsage,
    RecallRankingBlock,
    TextPart,
    ToolCallItem,
    ToolResultItem,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunCheckpoint
from agent_core.domain.tool_output import context_view

OMITTED = (
    "[Memory search result withheld: current evidence or shared recall allowance unavailable.]"
)


@dataclass(frozen=True)
class RecallAllowance:
    tokens: int = 2000
    items: int = 20
    exclude_ids: tuple[UUID, ...] = ()
    preserve_identity: bool = False

    def bound(self, query: RecallQuery) -> RecallQuery | None:
        """Fit recall to remaining capacity while preserving independent identity discovery."""
        if not self.tokens or not self.items:
            return None
        return query.model_copy(
            update={
                "budget_tokens": min(query.budget_tokens, self.tokens),
                "max_items": min(query.max_items, self.items),
                "exclude_ids": tuple(sorted(set(query.exclude_ids) | set(self.exclude_ids))),
                **(
                    {
                        "identity_max_items": query.identity_max_items or min(query.max_items, 20),
                        "identity_exclude_ids": query.exclude_ids
                        if query.identity_exclude_ids is None
                        else query.identity_exclude_ids,
                    }
                    if self.preserve_identity
                    else {}
                ),
            }
        )


FULL_ALLOWANCE = RecallAllowance()
NO_RESERVE = RecallAllowance(tokens=0, items=0)
AUTOMATIC_RESERVE = RecallAllowance(tokens=500, items=5)


def automatic_reserve(checkpoint: RunCheckpoint) -> RecallAllowance:
    """New search pins leave room for fresh automatic evidence (ADRs 0309/0310)."""
    spec = checkpoint.pinned_tool_specs.get("memory.search")
    version = spec.version if spec else checkpoint.pinned_tool_versions.get("memory.search")
    return AUTOMATIC_RESERVE if version in {"2.3.0", "2.4.0"} else NO_RESERVE


def ranking_block(result: RecallResult) -> RecallRankingBlock | None:
    if result.stages is None:
        return None
    return RecallRankingBlock(
        trace_id=result.trace_id,
        content_sha256=hashlib.sha256(result.rendered.encode()).hexdigest(),
        ranked_ids=result.stages.ranked_ids[:20],
    )


def context_ranking(
    conversation: Sequence[ConversationItem],
    automatic: Sequence[RecallRankingBlock | None],
) -> ContextRecallRanking | None:
    """Record the same block priority that allocated the shared context allowance.

    This operates on the final retained body, after pressure and excerpting. It
    never repairs an unknown ranking or reads evidence to improve a measurement.
    """
    blocks = []
    for index in reversed(_search_indexes(conversation)):
        item = conversation[index]
        assert isinstance(item, ToolResultItem)
        if item.memory_recall is None:
            # Withheld placeholders no longer contain a search block.
            if item.content == [TextPart(text=OMITTED)]:
                continue
            return None
        usage = item.memory_recall
        if (
            usage.trace_id is None
            or usage.ranked_ids is None
            or len(item.content) != 1
            or not isinstance(item.content[0], TextPart)
            or hashlib.sha256(item.content[0].text.encode()).hexdigest() != usage.content_sha256
        ):
            return None
        blocks.append(
            RecallRankingBlock(
                trace_id=usage.trace_id,
                content_sha256=usage.content_sha256,
                ranked_ids=usage.ranked_ids,
            )
        )
    for block in automatic:
        if block is None:
            return None
        blocks.append(block)
    if len(blocks) > 4 or len({block.trace_id for block in blocks}) != len(blocks):
        return None
    return ContextRecallRanking(
        blocks=tuple(blocks),
        ranked_ids=tuple(dict.fromkeys(key for block in blocks for key in block.ranked_ids))[:20],
    )


def select_search_results(
    conversation: Sequence[ConversationItem],
    *,
    permitted_receipts: set[MemoryRecallUsage] | None = None,
    reserve: RecallAllowance = NO_RESERVE,
) -> tuple[list[ConversationItem], RecallAllowance, bool]:
    """Do not mutate canonical evidence or infer item counts from rendered prose."""
    result = [context_view(item) for item in conversation]
    indexes = _search_indexes(result)
    allowance = RecallAllowance(preserve_identity=reserve != NO_RESERVE)
    seen: set[str] = set()
    seen_evidence: set[str] = set()
    omitted = False
    blocks = 0
    for index in reversed(indexes):
        item = result[index]
        assert isinstance(item, ToolResultItem)
        text = (
            item.content[0].text
            if len(item.content) == 1 and isinstance(item.content[0], TextPart)
            else None
        )
        digest = None if text is None else hashlib.sha256(text.encode()).hexdigest()
        # Unreceipted legacy blocks occupy the complete item class. Excerpted
        # or changed bytes cannot reuse counts measured against another payload.
        tokens, items = 2000, 20
        valid = text is not None and (len(text.encode()) + 3) // 4 <= 2000
        if item.memory_recall is not None:
            try:
                usage = MemoryRecallUsage.model_validate(item.memory_recall.model_dump())
                tokens = max(usage.tokens, 0 if text is None else (len(text.encode()) + 3) // 4)
                items = usage.items
                valid = valid and usage.content_sha256 == digest
            except (ValueError, AttributeError):
                valid = False
        if permitted_receipts is not None:
            valid = (
                valid
                and item.memory_recall is not None
                and item.memory_recall in permitted_receipts
            )
        # Use only keys verified against authoritative traces. The first pass
        # must still validate both receipts, so an invalid newer block cannot
        # suppress an older valid presentation of the same evidence.
        evidence_digest = (
            item.memory_recall.current_evidence_sha256
            if valid and permitted_receipts is not None and item.memory_recall is not None
            else None
        )
        if (
            valid
            and blocks < 4
            and digest not in seen
            and evidence_digest not in seen_evidence
            and tokens <= allowance.tokens - reserve.tokens
            and items <= allowance.items - reserve.items
        ):
            blocks += 1
            assert digest is not None
            seen.add(digest)
            if evidence_digest is not None:
                seen_evidence.add(evidence_digest)
            # Only currently validated, actually rendered beliefs can suppress a
            # fresh read. Ranked-but-unrendered evidence is not already in context.
            returned = (
                item.memory_recall.returned_ids or ()
                if permitted_receipts is not None and item.memory_recall is not None
                else ()
            )
            allowance = RecallAllowance(
                allowance.tokens - tokens,
                allowance.items - items,
                tuple(sorted(set(allowance.exclude_ids) | set(returned))),
                allowance.preserve_identity,
            )
        else:
            result[index] = item.model_copy(
                update={
                    "content": [TextPart(text=OMITTED)],
                    "context_content": None,
                    "memory_recall": None,
                }
            )
            omitted = True
    return result, allowance, omitted


def _search_indexes(conversation: Sequence[ConversationItem]) -> list[int]:
    calls: dict[str, bool] = {}
    indexes = []
    for index, item in enumerate(conversation):
        if isinstance(item, ToolCallItem):
            calls[item.call_id] = item.name == "memory.search" or (
                item.name == "tool.call" and item.arguments.get("name") == "memory.search"
            )
        elif (
            isinstance(item, ToolResultItem)
            and calls.get(item.call_id, False)
            and not item.is_error
            and item.trust is TrustLevel.MEMORY
        ):
            indexes.append(index)
    return indexes


async def select_current_search_results(
    conversation: Sequence[ConversationItem],
    retriever: object,
    *,
    session_id: UUID,
    run_id: UUID,
    reserve: RecallAllowance = NO_RESERVE,
) -> tuple[list[ConversationItem], RecallAllowance, bool]:
    """At most four local dependency validations under one shared deadline."""
    candidates, _, _ = select_search_results(conversation, reserve=reserve)
    permitted: set[MemoryRecallUsage] = set()
    validator = getattr(retriever, "validate_retained", None)
    if callable(validator):
        try:
            async with asyncio.timeout(1):
                for index in _search_indexes(candidates):
                    item = candidates[index]
                    assert isinstance(item, ToolResultItem)
                    usage = item.memory_recall
                    if (
                        usage is not None
                        and usage.trace_id is not None
                        and await validator(usage, session_id=session_id, run_id=run_id)
                    ):
                        permitted.add(usage)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "context_memory_validation_failed", extra={"error_class": type(exc).__name__}
            )
    return select_search_results(conversation, permitted_receipts=permitted, reserve=reserve)
