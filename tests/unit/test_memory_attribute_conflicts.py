"""Attribute-keyed conflicts (ADR-0248, plan ADR-C conflict half).

Formation selected conflict partners by ``(subject, belief_type)`` and called
every lower-authority statement about the same subject a conflict, so the
owner's affirmed home address carried seven partners that were email facts and
restatements, none rendered, and the model asked which city the garden is in.
A conflict is two claims about the same attribute with different values; a
restated value corroborates; unrelated claims are independent; a marker is
rendered only beside its partner; false links are repaired through a governed
reconciliation, never in SQL.
"""

from __future__ import annotations

from uuid import UUID

from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryDerivation,
    MemoryEdit,
    MemoryLongevity,
    MemoryRecord,
    RecalledBelief,
    RecallQuery,
    Sensitivity,
)
from agent_core.memory.formation import GovernedMemoryService
from agent_core.memory.predicates import predicate_key
from tests.contract.memory_fixtures import formation_stack, session_events, user_event
from tests.contract.support import PRINCIPAL_ID, SESSION_ID, TENANT, principal

ADDRESS = "Andy Vitus's home address is 12 Oak Lane, Los Altos Hills."
OTHER_ADDRESS = "Andy Vitus's home address is 9 Pine Street, Palo Alto."
SHIPPING = "Shipping address for Andy Vitus: 12 Oak Lane, Los Altos Hills."
MOUTHPIECES = "Andy Vitus reported current mouthpieces to the band."
HOME_CITY = "Andy Vitus lives in Palo Alto."
TEA = "Andy Vitus likes green tea in the morning."


async def _form(
    factory: MemoryUnitOfWorkFactory,
    service: GovernedMemoryService,
    statement: str,
    *,
    authority: MemoryAuthority,
    explicit: bool,
) -> MemoryRecord:
    sequence = await user_event(factory, statement)
    return await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement=statement,
        subject="Andy Vitus",
        scope="project-a",
        belief_type=BeliefType.FACT,
        sensitivity=Sensitivity.INTERNAL,
        source_event_ids=[sequence],
        explicit=explicit,
        authority=authority,
    )


async def _get(factory: MemoryUnitOfWorkFactory, belief_id: UUID) -> MemoryRecord:
    async with factory() as uow:
        return await uow.memories.get(belief_id, principal())


async def test_unrelated_claims_about_one_subject_are_independent() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    address = await _form(
        factory, service, ADDRESS, authority=MemoryAuthority.AFFIRMED, explicit=True
    )
    mouthpieces = await _form(
        factory, service, MOUTHPIECES, authority=MemoryAuthority.INFERRED, explicit=False
    )

    assert mouthpieces.id != address.id
    assert mouthpieces.conflicts_with == []
    assert mouthpieces.flagged_for_review is False
    stored = await _get(factory, address.id)
    assert stored.conflicts_with == []
    assert stored.flagged_for_review is False
    assert "memory.needs_confirmation" not in [e.event_type for e in await session_events(factory)]


async def test_a_restated_value_corroborates_instead_of_conflicting() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    address = await _form(
        factory, service, ADDRESS, authority=MemoryAuthority.AFFIRMED, explicit=True
    )
    shipping = await _form(
        factory, service, SHIPPING, authority=MemoryAuthority.INFERRED, explicit=False
    )

    assert shipping.id == address.id
    assert shipping.corroboration_count == address.corroboration_count + 1
    assert shipping.conflicts_with == []
    assert shipping.flagged_for_review is False
    assert shipping.authority is MemoryAuthority.AFFIRMED


async def test_different_values_for_one_attribute_still_conflict_and_flag_both() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    address = await _form(
        factory, service, ADDRESS, authority=MemoryAuthority.AFFIRMED, explicit=True
    )
    other = await _form(
        factory, service, OTHER_ADDRESS, authority=MemoryAuthority.INFERRED, explicit=False
    )

    assert other.id != address.id
    assert other.conflicts_with == [address.id]
    assert other.flagged_for_review is True
    stored = await _get(factory, address.id)
    assert stored.conflicts_with == [other.id]
    assert stored.flagged_for_review is True


async def test_formation_keys_a_belief_by_its_statement_not_a_stale_stored_key() -> None:
    """The stored key is derived content: a row whose statement changed after
    the key was written still names the attribute its words claim. Here the
    stored key says `home_city` but the statement names no attribute, so the
    owner restating it corroborates instead of forming a twin beside it."""

    _clock, factory, service, _retriever = await formation_stack()
    tea = await _form(factory, service, TEA, authority=MemoryAuthority.USER, explicit=True)
    async with factory() as uow:
        await uow.memories.reinforce(
            tea.model_copy(
                update={"predicate_key": predicate_key(HOME_CITY, BeliefType.FACT)}, deep=True
            )
        )

    restated = await _form(factory, service, TEA, authority=MemoryAuthority.USER, explicit=True)

    assert restated.id == tea.id
    assert restated.corroboration_count == tea.corroboration_count + 1


async def test_rewriting_a_statement_stores_the_key_of_its_new_words() -> None:
    """An owner edit and a hypothesis promotion both rewrite a statement in
    place; the stored key follows the words, so it never names an attribute the
    belief no longer claims."""

    _clock, factory, service, _retriever = await formation_stack()
    city = await _form(factory, service, HOME_CITY, authority=MemoryAuthority.USER, explicit=True)
    assert city.predicate_key == "home_city"
    edited = await service.edit(city.id, MemoryEdit(statement=TEA))
    assert edited.predicate_key == predicate_key(TEA, BeliefType.FACT)
    assert (await _get(factory, city.id)).predicate_key == predicate_key(TEA, BeliefType.FACT)

    guess = "Andy Vitus may move the studio to Lisbon next year."
    settled = "Andy Vitus is moving the studio to Lisbon in March."
    sequence = await user_event(factory, guess)
    hypothesis = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement=guess,
        subject="Andy Vitus",
        scope="project-a",
        belief_type=BeliefType.FACT,
        source_event_ids=[sequence],
        explicit=False,
        authority=MemoryAuthority.INFERRED,
        derivation=MemoryDerivation.HYPOTHESIS,
        longevity=MemoryLongevity.TENTATIVE,
    )
    promoted = await _form(factory, service, settled, authority=MemoryAuthority.USER, explicit=True)
    assert promoted.id == hypothesis.id
    assert promoted.statement == settled
    assert promoted.predicate_key == predicate_key(settled, BeliefType.FACT)


async def test_a_conflict_marker_is_rendered_only_beside_its_partner() -> None:
    """The spec's "return both" is honoured, or the dispute is not shown as a
    one-sided doubt the model would hedge on."""

    _clock, factory, service, retriever = await formation_stack()
    address = await _form(
        factory, service, ADDRESS, authority=MemoryAuthority.AFFIRMED, explicit=True
    )
    other = await _form(
        factory, service, OTHER_ADDRESS, authority=MemoryAuthority.INFERRED, explicit=False
    )
    assert other.conflicts_with == [address.id]

    def query(budget_tokens: int) -> RecallQuery:
        return RecallQuery(
            tenant_id=TENANT,
            principal_id=PRINCIPAL_ID,
            current_scope="project-a",
            text="Andy Vitus home address",
            budget_tokens=budget_tokens,
            max_items=10,
            min_score=0.1,
        )

    wide = await retriever.recall(query(600), session_id=SESSION_ID, turn_id=UUID(int=71))
    assert {item.belief_id for item in wide.items} == {address.id, other.id}
    assert f"conflicts=[m:{str(other.id)[:8]}]" in wide.rendered
    assert f"conflicts=[m:{str(address.id)[:8]}]" in wide.rendered

    narrow = None
    for budget in range(30, 600, 4):
        candidate = await retriever.recall(
            query(budget), session_id=SESSION_ID, turn_id=UUID(int=72)
        )
        if len(candidate.items) == 1:
            narrow = candidate
            break
    assert narrow is not None, "no budget renders exactly one of the two beliefs"
    assert "conflicts=" not in narrow.rendered
    # The link itself is still recorded for the trace and the browser.
    only = narrow.items[0]
    assert isinstance(only, RecalledBelief) and only.conflict_with


async def test_reconcile_conflicts_unlinks_false_pairs_through_a_governed_path() -> None:
    """The production shape: an affirmed address linked both ways to an email
    fact about the same subject, both flagged. A dry run writes nothing; the
    committed run unlinks the pair, clears the flag where no doubt remains,
    and records one `memory.conflict_cleared` event; a true attribute conflict
    is kept."""

    _clock, factory, service, _retriever = await formation_stack()
    address = await _form(
        factory, service, ADDRESS, authority=MemoryAuthority.AFFIRMED, explicit=True
    )
    mouthpieces = await _form(
        factory, service, MOUTHPIECES, authority=MemoryAuthority.INFERRED, explicit=False
    )
    other = await _form(
        factory, service, OTHER_ADDRESS, authority=MemoryAuthority.INFERRED, explicit=False
    )
    async with factory() as uow:
        # Replay the legacy linkage formation used to write.
        for record, partners in (
            (address, [mouthpieces.id, other.id]),
            (mouthpieces, [address.id]),
            (other, [address.id]),
        ):
            current = await uow.memories.get(record.id, principal())
            await uow.memories.reinforce(
                current.model_copy(
                    update={"conflicts_with": partners, "flagged_for_review": True}, deep=True
                )
            )

    dry = await service.reconcile_conflicts(confirm=False)
    assert (dry.pairs_examined, dry.pairs_cleared, dry.flags_cleared) == (2, 1, 1)
    assert (await _get(factory, address.id)).conflicts_with == [mouthpieces.id, other.id]
    assert (await _get(factory, mouthpieces.id)).flagged_for_review is True

    committed = await service.reconcile_conflicts(confirm=True)
    assert (committed.pairs_examined, committed.pairs_cleared, committed.flags_cleared) == (
        2,
        1,
        1,
    )
    assert (await _get(factory, address.id)).conflicts_with == [other.id]
    assert (await _get(factory, address.id)).flagged_for_review is True
    cleared = await _get(factory, mouthpieces.id)
    assert cleared.conflicts_with == []
    assert cleared.flagged_for_review is False
    assert (await _get(factory, other.id)).conflicts_with == [address.id]
    events = [e for e in await session_events(factory) if e.event_type == "memory.conflict_cleared"]
    assert len(events) == 1
    assert set(events[0].payload["belief_ids"]) == {str(address.id), str(mouthpieces.id)}
    assert events[0].payload["rule"] == "predicate-key@1"
    again = await service.reconcile_conflicts(confirm=True)
    assert (again.pairs_examined, again.pairs_cleared, again.flags_cleared) == (1, 0, 0)
