"""A sender's own signature block and the contact details it states (ADR-0336)."""

from __future__ import annotations

import pytest

from agent_core.domain.email_signature import (
    SIGNATURE_NAMESPACE,
    SignatureContact,
    sender_contact,
)
from agent_core.domain.people import normalize_identifier

RORY = "Rory O'Driscoll <rory@scalevp.test>"
SIGNED = (
    "Thanks, see you Thursday.\n"
    "\n"
    "-- \n"
    "Rory O'Driscoll\n"
    "Partner, Scale\n"
    "650.378.6038 <(650)%20378-6038>\n"
)


def test_the_block_under_the_senders_name_gives_a_title_and_a_number() -> None:
    assert sender_contact(SIGNED, RORY) == SignatureContact(
        title="Partner, Scale", phones=("650.378.6038",)
    )


def test_every_value_is_an_exact_substring_of_the_body() -> None:
    body = "Sounds good.\n\n*Rory O'Driscoll*\n*Partner, Scale*\nm: +1 (650) 378-6038\n"
    contact = sender_contact(body, RORY)
    assert contact == SignatureContact(title="Partner, Scale", phones=("+1 (650) 378-6038",))
    assert contact is not None and contact.title is not None
    assert contact.title in body and all(phone in body for phone in contact.phones)


@pytest.mark.parametrize(
    "body",
    [
        # Quoted history: the sender's reply has no signature of its own.
        "Agreed.\n\nOn Tue, Nov 17, 2026 at 9:00 AM Rory O'Driscoll wrote:\n"
        "> Rory O'Driscoll\n> Partner, Scale\n> 650.378.6038\n",
        # An unquoted reply header, as Outlook writes history.
        "Agreed.\n\nFrom: Rory O'Driscoll <rory@scalevp.test>\nSent: Tuesday\n\n"
        "Rory O'Driscoll\nPartner, Scale\n650.378.6038\n",
        # A forward carries its text unquoted under an envelope.
        "FYI\n\n---------- Forwarded message ---------\n"
        "From: Rory O'Driscoll <rory@scalevp.test>\nSubject: Deck\n\n"
        "Rory O'Driscoll\nPartner, Scale\n650.378.6038\n",
        "FYI\n\nBegin forwarded message:\n\nRory O'Driscoll\nPartner, Scale\n650.378.6038\n",
    ],
)
def test_a_signature_in_quoted_or_forwarded_text_is_never_read(body: str) -> None:
    assert sender_contact(body, RORY) is None


@pytest.mark.parametrize(
    "body",
    [
        # Someone else's block: the name line is not the sender's.
        "Call him.\n\nAlex Rivera\nAnalyst\n650.555.0100\n",
        # A first name alone is a sign-off, not a signature.
        "Thanks.\n\nRory\n650.378.6038\n",
        # Prose under the name is the message.
        "Rory O'Driscoll\nI wanted to follow up on the note you sent over to us last week.\n",
        # The name introduces a list.
        "Attendees:\nRory O'Driscoll\nPartner, Scale\n",
        # No line structure: Gmail supplied only flattened HTML.
        "Thanks. Rory O'Driscoll Partner, Scale 650.378.6038",
    ],
)
def test_text_that_is_not_the_senders_own_signature_gives_nothing(body: str) -> None:
    assert sender_contact(body, RORY) is None


def test_a_sender_without_a_two_word_display_name_gives_nothing() -> None:
    assert sender_contact(SIGNED, "rory@scalevp.test") is None
    assert sender_contact(SIGNED.replace("Rory O'Driscoll", "Rory"), "Rory <r@x.test>") is None


@pytest.mark.parametrize(
    "line",
    [
        "Sent from my iPhone",
        "650.378.6038",
        "rory@scalevp.test",
        "www.scalevp.test",
        "https://scalevp.test/rory",
        "950 Tower Lane, Foster City",
    ],
)
def test_the_line_under_the_name_is_a_title_only_when_it_reads_as_one(line: str) -> None:
    contact = sender_contact(f"Thanks.\n\nRory O'Driscoll\n{line}\nScale\n", RORY)
    assert contact is None or contact.title is None


def test_only_the_line_directly_under_the_name_is_the_title() -> None:
    contact = sender_contact("Thanks.\n\nRory O'Driscoll\n650.378.6038\nPartner, Scale\n", RORY)
    assert contact == SignatureContact(title=None, phones=("650.378.6038",))


def test_numbers_are_recognised_by_shape_and_fax_is_skipped() -> None:
    body = (
        "Thanks.\n\nRory O'Driscoll\nPartner, Scale\n"
        "Office (650) 555-0100 | Mobile +44 20 7946 0958\n"
        "Fax: 650-555-0199\n"
        "950 Tower Lane, Foster City, CA 94404-1234\n"
        "Founded 2026-11-20, room 020 7946 0958\n"
    )
    contact = sender_contact(body, RORY)
    assert contact is not None
    assert contact.phones == ("(650) 555-0100", "+44 20 7946 0958")


def test_at_most_four_numbers_are_kept_and_a_repeat_counts_once() -> None:
    numbers = ["650.555.0100", "650-555-0100", *(f"650.555.01{n:02d}" for n in range(1, 7))]
    contact = sender_contact("Hi.\n\nRory O'Driscoll\n" + "\n".join(numbers) + "\n", RORY)
    assert contact is not None
    assert contact.phones == ("650.555.0100", "650.555.0101", "650.555.0102", "650.555.0103")


def test_a_hostile_long_line_is_not_a_signature() -> None:
    body = "Rory O'Driscoll\n" + "9" * 200_000 + "\n"
    assert sender_contact(body, RORY) is None


def test_signature_numbers_compare_by_digits_and_never_as_e164() -> None:
    written = normalize_identifier("phone", SIGNATURE_NAMESPACE, "650.378.6038")
    assert written == normalize_identifier("phone", SIGNATURE_NAMESPACE, "(650) 378-6038")
    assert written == "6503786038"
    # The owner namespace still requires an explicit international prefix.
    with pytest.raises(ValueError, match="international prefix"):
        normalize_identifier("phone", "owner", "650.378.6038")
    with pytest.raises(ValueError):
        normalize_identifier("phone", SIGNATURE_NAMESPACE, "call me")
