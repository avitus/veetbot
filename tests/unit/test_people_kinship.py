"""Owner-stated kinship becomes a People relationship (ADR-0259).

Production, 2026-09-29: the owner wrote "Erin is my wife. We married in 2005.
Kyrri is my second daughter." The chat saved "Andy Vitus says Erin is his
wife" linked to Erin's People record, but no owner-to-Erin edge was ever
written, so every family overview left out the owner's wife.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import (
    PeopleEndpoint,
    PeopleQuery,
    Person,
    RelationshipAssertion,
)
from agent_core.domain.policies import TrustLevel
from agent_core.memory.formation import GovernedMemoryService
from agent_core.tools.memory_remember import PeopleMemoryRememberTool
from tests.contract.memory_fixtures import user_event
from tests.contract.support import ids, memory_uow_factory, principal, tool_context

OWNER = principal().model_copy(update={"scopes": {"people.read", "people.write"}})


async def _person(factory: Any, clock: Any, name: str) -> Person:
    person = Person(
        id=uuid4(),
        display_name=name,
        state="active",
        tenant_id=OWNER.tenant_id,
        principal_id=OWNER.principal_id,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
    return person


async def _edges(factory: Any) -> list[RelationshipAssertion]:
    async with factory() as uow:
        rows = await uow.people.query(
            PeopleQuery(
                tenant_id=OWNER.tenant_id,
                principal_id=OWNER.principal_id,
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                kinds=["relationship"],
                limit=100,
            )
        )
    return [row for row in rows if isinstance(row, RelationshipAssertion)]


async def _save(
    message: str,
    name: str,
    statement: str,
    *,
    belief_type: str = "relationship",
    role: str = "subject",
    others: tuple[str, ...] = (),
) -> tuple[Any, Person, dict[str, Any]]:
    clock, factory = await memory_uow_factory()
    person = await _person(factory, clock, name)
    for other in others:
        await _person(factory, clock, other)
    await user_event(factory, message)
    service = GovernedMemoryService(factory, clock, ids(), OWNER, people_enabled=True)
    result = await PeopleMemoryRememberTool(service).execute(
        {
            "scope": "veetbot",
            "subject": f"Andy Vitus family: {name}",
            "statement": statement,
            "belief_type": belief_type,
            "person_refs": [{"person_id": str(person.id), "expected_revision": 1, "role": role}],
        },
        replace(tool_context(), principal=OWNER, origin_trust=TrustLevel.MEMORY),
    )
    assert result.ok and result.structured is not None
    return factory, person, result.structured


OWNER_END = PeopleEndpoint(kind="owner")


@pytest.mark.parametrize("role", ["subject", "object"])
@pytest.mark.parametrize(
    ("message", "name", "statement", "predicate", "owner_first"),
    [
        (
            "Erin is my wife. We married in 2005. Kyrri is my second daughter.",
            "Erin",
            "Andy Vitus says Erin is his wife and they married in 2005.",
            "spouse",
            True,
        ),
        (
            "Erin is my wife. We married in 2005. Kyrri is my second daughter.",
            "Kyrri",
            "Andy Vitus says Kyrri is his second daughter.",
            "parent",
            True,
        ),
        (
            "Kyrri and Riv are my daughters. Erin is my wife",
            "Riv",
            "Riv is Andy's daughter.",
            "parent",
            True,
        ),
        (
            "My mother, Cheryl, lives in Marbella.",
            "Cheryl",
            "Cheryl is Andy's mother.",
            "parent",
            False,
        ),
        ("My brother Sam is visiting.", "Sam", "Sam is Andy's brother.", "sibling", True),
        ("My partner is Jules.", "Jules", "Jules is Andy's partner.", "partner", True),
        ("Erin is my wife.", "Erin", "Erin is Andy's wife, not his sister.", "spouse", True),
    ],
)
async def test_owner_stated_kinship_on_a_linked_save_creates_the_owner_relationship(
    role: str, message: str, name: str, statement: str, predicate: str, owner_first: bool
) -> None:
    factory, person, saved = await _save(message, name, statement, role=role)
    [edge] = await _edges(factory)
    person_end = PeopleEndpoint(kind="person", id=person.id)
    assert edge.predicate == predicate
    assert (edge.subject, edge.object) == (
        (OWNER_END, person_end) if owner_first else (person_end, OWNER_END)
    )
    assert edge.belief_id == UUID(saved["belief_id"])
    assert not edge.unresolved


@pytest.mark.parametrize(
    ("message", "name", "statement", "belief_type", "others"),
    [
        ("Erin is not my wife.", "Erin", "Erin is not Andy's wife.", "relationship", ()),
        ("Erin is my ex-wife.", "Erin", "Erin is Andy's ex-wife.", "relationship", ()),
        ("Erin was my wife.", "Erin", "Erin was Andy's wife.", "relationship", ()),
        ("Erin is my wife.", "Erin", "Erin is not Andy's wife.", "relationship", ()),
        ("Erin is my wife.", "Erin", "Erin is Andy's ex-wife.", "relationship", ()),
        ("Erin is my wife.", "Erin", "Erin likes tea.", "fact", ()),
        ("Erin is my wife.", "Erin", "Andy and Erin went to Paris.", "relationship", ()),
        ("Maya is my friend. Erin is my wife.", "Maya", "Maya is Andy's wife.", "relationship", ()),
        (
            "My wife's friend Erin is visiting.",
            "Erin",
            "Erin is Andy's wife's friend.",
            "relationship",
            (),
        ),
    ],
)
async def test_kinship_edge_needs_the_owners_own_words_and_a_relationship_save(
    message: str, name: str, statement: str, belief_type: str, others: tuple[str, ...]
) -> None:
    factory, _person_row, _saved = await _save(
        message, name, statement, belief_type=belief_type, others=others
    )
    assert await _edges(factory) == []


@pytest.mark.parametrize(
    ("message", "recorded"),
    [
        ("Erin is my wife; we divorced last year.", False),
        ("My wife Erin passed away last year.", False),
        ("Erin is my wife. My wife died last year.", False),
        ("Erin is my wife. Erin and I separated.", False),
        ("Erin is my wife. I divorced her in 2010.", False),
        ("My wife Erin, who died in 2019, loved roses.", False),
        ("Erin is my wife. She was ill and died last year.", False),
        ("Erin is my wife. I didn't know she died.", False),
        # Said of someone else or negated: still no link (ADR-0259).
        ("Erin is my wife. My father died in 1990.", False),
        ("Erin is my wife. Erin's father died last year.", False),
        ("Erin is my wife. We never divorced.", False),
        ("Erin is my wife. Sam and Beth divorced last year.", False),
        ("Erin is my wife. We married in 2005.", True),
    ],
)
async def test_any_ending_in_the_message_records_no_link(message: str, recorded: bool) -> None:
    factory, _person_row, _saved = await _save(message, "Erin", "Erin is Andy's wife.")
    assert [edge.predicate for edge in await _edges(factory)] == (["spouse"] if recorded else [])


@pytest.mark.parametrize(
    ("message", "recorded"),
    [("\u674e is my wife.", True), ("\u674e is my wife. \u674e died last year.", False)],
)
async def test_an_ending_is_found_for_a_name_in_any_script(message: str, recorded: bool) -> None:
    factory, _person_row, _saved = await _save(message, "\u674e", "\u674e is Andy's wife.")
    assert [edge.predicate for edge in await _edges(factory)] == (["spouse"] if recorded else [])


async def test_kinship_edge_is_not_duplicated_when_the_tie_is_saved_again() -> None:
    clock, factory = await memory_uow_factory()
    erin = await _person(factory, clock, "Erin")
    await user_event(factory, "Erin is my wife.")
    tool = PeopleMemoryRememberTool(
        GovernedMemoryService(factory, clock, ids(), OWNER, people_enabled=True)
    )
    context = replace(tool_context(), principal=OWNER, origin_trust=TrustLevel.MEMORY)
    for statement in ("Erin is Andy's wife.", "Andy confirmed that Erin is his wife."):
        result = await tool.execute(
            {
                "scope": "veetbot",
                "subject": "Erin",
                "statement": statement,
                "belief_type": "relationship",
                "person_refs": [{"person_id": str(erin.id), "expected_revision": 1}],
            },
            context,
        )
        assert result.ok
    [edge] = await _edges(factory)
    assert edge.predicate == "spouse"
    assert edge.object == PeopleEndpoint(kind="person", id=erin.id)


async def test_kinship_edge_finds_the_current_tie_past_one_page_of_edges() -> None:
    """Erin's current tie sorts after a full page of her other edges."""

    clock, factory = await memory_uow_factory()
    erin = await _person(factory, clock, "Erin")
    sam = await _person(factory, clock, "Sam")
    erin_end = PeopleEndpoint(kind="person", id=erin.id)
    common: dict[str, Any] = {
        "tenant_id": OWNER.tenant_id,
        "principal_id": OWNER.principal_id,
        "created_at": clock.now(),
        "updated_at": clock.now(),
        "belief_id": uuid4(),
    }
    current = UUID(int=2**128 - 1)
    async with factory() as uow:
        for index in range(1, 102):
            await uow.people.put(
                RelationshipAssertion(
                    id=UUID(int=index),
                    subject=erin_end,
                    object=PeopleEndpoint(kind="person", id=sam.id),
                    predicate="friend",
                    **common,
                ),
                expected_revision=0,
            )
        await uow.people.put(
            RelationshipAssertion(
                id=current, subject=OWNER_END, object=erin_end, predicate="spouse", **common
            ),
            expected_revision=0,
        )
    await user_event(factory, "Erin is my wife.")
    result = await PeopleMemoryRememberTool(
        GovernedMemoryService(factory, clock, ids(), OWNER, people_enabled=True)
    ).execute(
        {
            "scope": "veetbot",
            "subject": "Erin",
            "statement": "Erin is Andy's wife.",
            "belief_type": "relationship",
            "person_refs": [{"person_id": str(erin.id), "expected_revision": 1}],
        },
        replace(tool_context(), principal=OWNER, origin_trust=TrustLevel.MEMORY),
    )
    assert result.ok
    async with factory() as uow:
        later = await uow.people.query(
            PeopleQuery(
                tenant_id=OWNER.tenant_id,
                principal_id=OWNER.principal_id,
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                kinds=["relationship"],
                after=UUID(int=101),
                limit=100,
            )
        )
    assert [row.id for row in later] == [current]


async def test_kinship_repair_previews_then_records_ties_behind_earlier_saves() -> None:
    """Erin's production shape: a save from before ADR-0259, linked by role
    "object" to an "Erin Vitus" record the owner later merged into "Erin"."""

    from tests.contract.people_kinship_cases import kinship_repair_case
    from tests.contract.support import SESSION_ID

    clock, factory = await memory_uow_factory()
    sequence = await user_event(factory, "Erin is my wife. We married in 2005.")
    await kinship_repair_case(factory, clock, OWNER, SESSION_ID, sequence)


async def test_kinship_repair_preview_counts_a_restated_tie_once() -> None:
    from tests.contract.people_kinship_cases import kinship_repair_case
    from tests.contract.support import SESSION_ID

    clock, factory = await memory_uow_factory()
    sequence = await user_event(factory, "Erin is my wife. We married in 2005.")
    await kinship_repair_case(
        factory,
        clock,
        OWNER,
        SESSION_ID,
        sequence,
        restatements=("Andy confirmed that Erin is his wife.",),
    )


async def test_kinship_repair_records_nothing_when_the_owner_says_the_tie_ended() -> None:
    from tests.contract.people_kinship_cases import kinship_repair_case
    from tests.contract.support import SESSION_ID

    clock, factory = await memory_uow_factory()
    sequence = await user_event(factory, "Erin is my wife; we divorced last year.")
    await kinship_repair_case(factory, clock, OWNER, SESSION_ID, sequence, ended=True)
