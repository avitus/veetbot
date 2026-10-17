"""Project observed originals to authored labels only after retrieval finishes."""

import hashlib
import html
import json
from collections.abc import Mapping, Sequence
from uuid import UUID

from agent_core.domain.general_recall_release import RecallExpectedSource, RecallSourceWitness
from agent_core.domain.memory import MemoryRecord, RecalledBelief, RecallResult
from agent_core.domain.messages import ContextRecallRanking
from agent_core.evals.general_recall_corpus import ReaderAnswer, RecallFixture
from agent_core.memory.equivalence import statement_matches_claim, subject_matches
from agent_core.memory.general_recall_evidence import _unique_object


def observed_ranked_ids(
    results: Sequence[RecallResult], *, ranking: ContextRecallRanking | None = None
) -> tuple[UUID, ...] | None:
    """Certify only an unambiguous observed ordering, never invent cross-query ranks.

    Repeated identical orders and empty reads do not introduce another ordering.
    Different nonempty lists require a runtime merge observation before a release
    publisher can certify their global top twenty. Diagnostic lists stay intact.
    """
    if any(result.stages is None for result in results):
        return None
    if ranking is not None:
        try:
            ranking = ContextRecallRanking.model_validate(ranking.model_dump())
        except ValueError:
            return None
        by_trace = {result.trace_id: result for result in results}
        for block in ranking.blocks:
            result = by_trace.get(block.trace_id)
            if (
                result is None
                or result.stages is None
                or block.ranked_ids != result.stages.ranked_ids[:20]
                or block.content_sha256 != hashlib.sha256(result.rendered.encode()).hexdigest()
            ):
                return None
        return ranking.ranked_ids
    orders = {
        result.stages.ranked_ids
        for result in results
        if result.stages is not None and result.stages.ranked_ids
    }
    return next(iter(orders), ()) if len(orders) <= 1 else None


def rendered_noise_items(
    visible: Sequence[RecallResult],
    *,
    labels: Mapping[UUID, tuple[str, ...]],
    required: set[str],
    useful_anchors: set[UUID],
) -> int:
    """Count every distinct rendered item without mechanically proven required support.

    People context has no authored mapping to atomic labels in this evaluator.
    Count it as unlabeled rather than silently omitting it or granting evidence
    credit from a person's name alone. Revisions remain distinct observations.
    """
    keys = {item.belief_id for result in visible for item in result.items}
    usable = {item.belief_id for result in visible for item in result.items if not item.blocked}
    people = {(item.record_id, item.revision) for result in visible for item in result.people}
    return len(people) + sum(
        key not in usable
        or (not (set(labels.get(key, ())) & required) and key not in useful_anchors)
        for key in keys
    )


def recovered_source_witnesses(
    case: RecallFixture,
    results: Sequence[RecallResult],
    visible: Sequence[RecallResult],
    source_events: Mapping[str, tuple[UUID, int]],
    *,
    ranking: ContextRecallRanking | None = None,
) -> tuple[RecallSourceWitness, ...]:
    """Match complete admitted excerpts against authored history after the read."""
    by_event = {event: key for key, event in source_events.items()}
    turns = {turn.id: turn for turn in case.history}
    seen: dict[str, RecallSourceWitness] = {}
    visible_ids = {result.trace_id for result in visible}
    global_order = observed_ranked_ids(results, ranking=ranking)
    global_ranks = {key: index + 1 for index, key in enumerate(global_order or ())}
    for result in results:
        if result.stages is None:
            continue
        for item in result.items:
            if isinstance(item, RecalledBelief) and not item.blocked:
                for quote in item.sources:
                    if (
                        hashlib.sha256(quote.text.encode()).hexdigest() != quote.text_sha256
                        or html.escape(quote.text) not in result.rendered
                    ):
                        raise ValueError("Recovered rendered source is absent or changed")
        ranks = {key: index + 1 for index, key in enumerate(result.stages.ranked_ids)}
        rendered_sources = {
            (item.belief_id, source.session_id, source.event_sequence, source.text_sha256)
            for item in result.items
            if isinstance(item, RecalledBelief)
            and not item.blocked
            and result.trace_id in visible_ids
            for source in item.sources
            if hashlib.sha256(source.text.encode()).hexdigest() == source.text_sha256
        }
        for source in result.stages.source_candidates:
            key = by_event.get((source.session_id, source.event_sequence))
            if key is None:
                continue  # A load distractor supplies no authored evidence.
            digest = hashlib.sha256(turns[key].content.encode()).hexdigest()
            if source.text_sha256 != digest:
                raise ValueError("Recovered source digest differs from the authored history")
            rank = ranks.get(source.belief_id)
            if rank is None or source.belief_id not in {item.belief_id for item in result.items}:
                raise ValueError("Recovered source has no observed selected original")
            witness = RecallSourceWitness(
                source_id=key,
                text_sha256=digest,
                session_id=source.session_id,
                event_sequence=source.event_sequence,
                belief_id=source.belief_id,
                anchor_rank=global_ranks.get(source.belief_id),
                rendered=(source.belief_id, source.session_id, source.event_sequence, digest)
                in rendered_sources,
            )
            previous = seen.get(key)
            if previous is not None:
                best = (
                    witness
                    if (witness.anchor_rank or 10001) < (previous.anchor_rank or 10001)
                    else previous
                )
                witness = best.model_copy(
                    update={"rendered": witness.rendered or previous.rendered}
                )
            seen[key] = witness
    return tuple(seen.values())


def authored_source_evidence(case: RecallFixture) -> dict[str, tuple[RecallExpectedSource, ...]]:
    turns = {turn.id: turn for turn in case.history}
    return {
        label.id: tuple(
            RecallExpectedSource(
                source_id=key, text_sha256=hashlib.sha256(turns[key].content.encode()).hexdigest()
            )
            for key in label.source_ids
        )
        for label in case.evidence
    }


def evidence_labels(
    case: RecallFixture,
    records: Sequence[MemoryRecord],
    source_events: Mapping[str, tuple[UUID, int]],
) -> dict[UUID, tuple[str, ...]]:
    labels: dict[UUID, tuple[str, ...]] = {}
    for record in records:
        observed_sources = {(record.source_session_id, seq) for seq in record.source_event_ids}
        labels[record.id] = tuple(
            label.id
            for label in case.evidence
            if {source_events[key] for key in label.source_ids} <= observed_sources
            and subject_matches(
                record.subject,
                (label.subject, *label.equivalent_subjects),
                (label.statement, *label.equivalent_statements),
            )
            and any(
                statement_matches_claim(record.statement, reference)
                for reference in (label.statement, *label.equivalent_statements)
            )
        )
    return labels


def answer_violations(case: RecallFixture, raw: str) -> tuple[str, ...]:
    try:
        answer = ReaderAnswer.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
    except (ValueError, TypeError):
        return ()  # The answer scorer separately rejects malformed output.
    return tuple(
        label.id
        for label in case.evidence
        if label.invalid_current
        and any(
            answer.values.get(field) is not None
            and " ".join(str(answer.values[field]).casefold().split())
            in {" ".join(value.casefold().split()) for value in values}
            for field, values in label.invalid_answer_values.items()
        )
    )
