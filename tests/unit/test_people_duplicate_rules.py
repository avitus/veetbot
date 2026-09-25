"""Duplicate evidence between two people, before anything is merged (ADR-0125)."""

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.domain.people_duplicates import (
    Standing,
    family_surnames,
    merge_direction,
    name_match,
    name_tokens,
    shares_family_name,
)
from tests.contract.support import NOW


@pytest.mark.parametrize(
    "left, right, reason",
    [
        ("Sabina Smith", "Sabina Smith", "same_name"),
        ("Hayley J. Hoad", "Hayley Hoad", "same_name"),
        ("José Álvarez", "Jose Alvarez", "same_name"),
        ("John Canady Jr.", "John Canady", "same_name"),
        ("Erin", "Erin Vitus", "first_name"),
        ("Cheryl", "cheryl", "first_name"),
        ("Kyrri", "Kyrriana Vitus", "nickname"),
        ("Riv", "Rivonia Vitus", "nickname"),
        ("Al", "Alex Vitus", None),
        ("Erin Walsh", "Erin Vitus", None),
        ("Dana Reyes", "Maya Reyes", None),
        ("Sam", "Samantha", None),
    ],
)
def test_name_agreement(left: str, right: str, reason: str | None) -> None:
    assert name_match([name_tokens(left)], [name_tokens(right)]) == reason
    assert name_match([name_tokens(right)], [name_tokens(left)]) == reason


def test_the_strongest_agreement_across_aliases_wins() -> None:
    erin = [name_tokens("Erin"), name_tokens("Erin Vitus")]
    assert name_match(erin, [name_tokens("Erin Vitus")]) == "same_name"


def test_family_name_comes_from_the_owner_address() -> None:
    surnames = family_surnames(frozenset({"email:avitus@gmail.com", "handle:avitus"}))
    assert shares_family_name(name_tokens("Erin Vitus"), surnames)
    assert not shares_family_name(name_tokens("Erin Walsh"), surnames)
    assert not shares_family_name(name_tokens("Vitus"), surnames)


def test_the_weaker_identity_merges_into_the_stronger() -> None:
    older, newer = NOW - timedelta(days=30), NOW
    owner_made = Standing(UUID(int=1), "active", False, False, 0, newer)
    chat_named = Standing(UUID(int=2), "provisional", False, True, 0, older)
    written_to = Standing(UUID(int=3), "provisional", False, False, 11, older)
    busier = Standing(UUID(int=4), "provisional", False, False, 20, newer)
    assert merge_direction(written_to, owner_made) == (written_to.person_id, owner_made.person_id)
    assert merge_direction(chat_named, written_to) == (written_to.person_id, chat_named.person_id)
    assert merge_direction(written_to, busier) == (written_to.person_id, busier.person_id)
