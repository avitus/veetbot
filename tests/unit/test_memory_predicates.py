"""The closed predicate-key set (ADR-0249): which attribute a statement claims."""

from __future__ import annotations

import pytest

from agent_core.domain.memory import BeliefType
from agent_core.memory.predicates import (
    PREDICATE_RULE_VERSION,
    comparable,
    predicate_claim,
    predicate_key,
    restates,
    same_attribute,
)


@pytest.mark.parametrize(
    ("statement", "belief_type", "key", "value"),
    [
        (
            "Andy Vitus's home address is 12 Oak Lane, Los Altos Hills.",
            BeliefType.FACT,
            "home_address",
            "12 oak lane, los altos hills",
        ),
        (
            "Shipping address for Andy Vitus: 12 Oak Lane, Los Altos Hills.",
            BeliefType.FACT,
            "home_address",
            "12 oak lane, los altos hills",
        ),
        (
            "User lives at 9 Pine Street, Palo Alto.",
            BeliefType.USER_MODEL_ATTR,
            "home_address",
            "9 pine street, palo alto",
        ),
        (
            "User lives in Los Altos Hills.",
            BeliefType.USER_MODEL_ATTR,
            "home_city",
            "los altos hills",
        ),
        (
            "User's phone number is +1 650 555 0100.",
            BeliefType.USER_MODEL_ATTR,
            "phone",
            "+1 650 555 0100",
        ),
        ("Email: andy@example.com", BeliefType.FACT, "email", "andy@example.com"),
        (
            "Andy works at Scale Venture Partners.",
            BeliefType.FACT,
            "employer",
            "scale venture partners",
        ),
        ("Matthew Scullion is CEO of Matillion.", BeliefType.FACT, "job_title:matillion", "ceo"),
        ("User's birthday is 3 March.", BeliefType.USER_MODEL_ATTR, "birthday", "3 march"),
        ("User's mother is Cheryl.", BeliefType.RELATIONSHIP, "relationship:mother", "cheryl"),
    ],
)
def test_known_attributes_key_and_value(
    statement: str, belief_type: BeliefType, key: str, value: str
) -> None:
    claim = predicate_claim(statement, belief_type)
    assert (claim.key, claim.value) == (key, value)
    assert claim.recognized


def test_unknown_shapes_fall_back_to_claim_identity_never_to_the_subject() -> None:
    first = predicate_claim("Andy Vitus reported current mouthpieces to the band.", BeliefType.FACT)
    second = predicate_claim("Andy Vitus plays trombone in two bands.", BeliefType.FACT)
    assert first.key.startswith("claim:") and second.key.startswith("claim:")
    assert not first.recognized
    assert not same_attribute(first, second)
    assert (
        predicate_key("  Andy Vitus  reported current mouthpieces to the band ", BeliefType.FACT)
        == first.key
    )


def test_restatement_is_same_attribute_same_value_only() -> None:
    home = predicate_claim("User's home address is 12 Oak Lane.", BeliefType.USER_MODEL_ATTR)
    shipping = predicate_claim("Shipping address: 12 Oak Lane", BeliefType.USER_MODEL_ATTR)
    other = predicate_claim("User's home address is 9 Pine Street.", BeliefType.USER_MODEL_ATTR)
    city = predicate_claim("User lives in Palo Alto.", BeliefType.USER_MODEL_ATTR)
    assert restates(home, shipping)
    assert same_attribute(home, other) and not restates(home, other)
    assert not same_attribute(home, city)
    # Two identical unknown claims share a key but have no value to compare.
    plain = predicate_claim("Andy plays trombone.", BeliefType.FACT)
    assert same_attribute(plain, predicate_claim("Andy plays trombone", BeliefType.FACT))
    assert not restates(plain, plain)


def test_preferences_are_not_keyed_so_a_later_one_supersedes_by_subject() -> None:
    """A topic has no reliable surface form ("dark mode everywhere" against
    "light mode now that the office is bright"), so preferences keep the legacy
    subject comparison, which the M16 update scenarios pin."""
    dark = predicate_claim("User prefers dark mode everywhere.", BeliefType.PREFERENCE)
    light = predicate_claim("User prefers light mode.", BeliefType.PREFERENCE)
    assert not dark.recognized and not light.recognized
    assert comparable(dark, light)


def test_long_claims_key_on_a_digest_so_the_key_always_fits_its_column() -> None:
    """A 512-character learned quote produced a 530-character key and the
    record failed validation; past 200 characters the claim keys on a digest."""
    long = "Source suggests: " + "a very long claim " * 40 + "[Source: https://example.com]"
    key = predicate_key(long, BeliefType.FACT)
    assert key.startswith("claim:sha256:") and len(key) < 100
    assert key == predicate_key(long + "  ", BeliefType.FACT)
    assert key != predicate_key(long + " more", BeliefType.FACT)


def test_rule_version_is_pinned() -> None:
    assert PREDICATE_RULE_VERSION == "predicate-key@1"
