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
        # A blank line under the heading does not make the list a signature.
        "Attendees:\n\nRory O'Driscoll\nAlex Rivera\nSam Example\n",
        "*Attendees:*\n\nRory O'Driscoll\nAlex Rivera\n",
        # No line structure: Gmail supplied only flattened HTML.
        "Thanks. Rory O'Driscoll Partner, Scale 650.378.6038",
    ],
)
def test_text_that_is_not_the_senders_own_signature_gives_nothing(body: str) -> None:
    assert sender_contact(body, RORY) is None


@pytest.mark.parametrize(
    "body",
    [
        # Below the first entry of a list a colon introduces.
        "Attendees:\n\nAlex Rivera\nRory O'Driscoll\nSam Example\n",
        # Wherever in the list it stands, with blank lines between the entries.
        "Attendees:\nAlex Rivera\n\nRory O'Driscoll\n\nSam Example\n",
        "Attendees:\nAlex Rivera\nJamie Example\nRiley Example\nRory O'Driscoll\nSam Example\n",
        # An entry that ends in a suffix, an initial or a doubt does not end the list.
        "Attendees:\nSam Example Jr.\nRory O'Driscoll\nAlex Rivera\n",
        "Attending:\nAcme Inc.\nRory O'Driscoll\nAlex Rivera\n",
        "Attendees:\nAlex R.\nRory O'Driscoll\nSam E.\n",
        "Attendees:\nSam Example?\nRory O'Driscoll\nAlex Rivera\n",
        # A bold line or a heading introduces a list as a colon does.
        "*Attendees*\n\nAlex Rivera\nRory O'Driscoll\nSam Example\n",
        "## Attendees\n\nAlex Rivera\nRory O'Driscoll\nSam Example\n",
        "*Alex Rivera*\n*Rory O'Driscoll*\n*Sam Example*\n",
        # So does a label with the first name beside it, as in a recipient line.
        "Cc: Alex Rivera\nRory O'Driscoll\nSam Example\n",
        "cc:\nAlex Rivera\nRory O'Driscoll\nSam Example\n",
        # A bulleted name is an entry, with or without anything above the list.
        "Present\n* Alex Rivera\n* Rory O'Driscoll\n* Sam Example\n",
        "* Rory O'Driscoll\n* Sam Example\n",
    ],
)
def test_the_senders_name_inside_a_list_is_not_a_signature(body: str) -> None:
    assert sender_contact(body, RORY) is None


@pytest.mark.parametrize(
    "body",
    [
        "Present\n- Alex Rivera\n- Rory O'Driscoll\n- Sam Example\n",
        "Present\n\u2022 Alex Rivera\n\u2022 Rory O'Driscoll\n\u2022 Sam Example\n",
        "Present\n1. Alex Rivera\n2. Rory O'Driscoll\n3. Sam Example\n",
        "To: Alex Rivera, Rory O'Driscoll, Sam Example\n",
        "To: Alex Rivera,\nRory O'Driscoll,\nSam Example\n",
    ],
)
def test_a_marked_or_inline_list_of_names_never_was_a_signature(body: str) -> None:
    """These lines are not exactly the sender's name, so they were safe before the list rule."""
    assert sender_contact(body, RORY) is None


@pytest.mark.parametrize(
    "above",
    [
        # A finished sentence ends the list a colon introduced.
        "Thanks:\nI'll bring the deck.\n\n",
        "Andy:\n\nYes.\n\n",
        "Attendees:\nAlex Rivera\nSam Example\n\nSee you all there!\n\n",
        "Attendees:\nAlex Rivera\nSam Example\n\nDoes Thursday still work?\n\n",
        "Call details: Thursday at three, in the usual room.\n\n",
        # So does the closing a writer signs under, and the signature delimiter.
        "The files:\n- deck.pdf\n- model.xlsx\n\nBest,\n",
        "The files:\n- deck.pdf\n- model.xlsx\n\nMany thanks\n",
        "The files:\n- deck.pdf\n- model.xlsx\n\n-- \n",
    ],
)
def test_a_signature_after_a_list_has_ended_is_still_read(above: str) -> None:
    body = above + "Rory O'Driscoll\nPartner, Scale\n650.378.6038\n"
    assert sender_contact(body, RORY) == SignatureContact(
        title="Partner, Scale", phones=("650.378.6038",)
    )


def test_a_name_straight_under_an_open_list_is_left_alone() -> None:
    """Nothing ends the list, so the block is not read: a wrong title is the worse error."""
    body = "The files:\n- deck.pdf\n- model.xlsx\n\nRory O'Driscoll\nPartner, Scale\n"
    assert sender_contact(body, RORY) is None


def test_the_list_rule_stays_quick_on_hostile_lines() -> None:
    import time

    hostile = "](https://x" * 20_000
    body = "\n".join(["Attendees:", hostile, "a: " * 60_000, "Rory O'Driscoll", "Partner, Scale"])
    started = time.monotonic()
    sender_contact(body, RORY)
    sender_contact("\n".join(["Cc: Alex Rivera", "Rory O'Driscoll"] * 20_000), RORY)
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    "body",
    [
        # A fenced example of a signature is the message, whoever it names.
        "Here is the format I mean.\n\n```\nRory O'Driscoll\nPartner, Scale\n650.378.6038\n```\n",
        "~~~\nRory O'Driscoll\nPartner, Scale\n650.378.6038\n~~~\n",
        # A longer fence closes only on its own marker, and may name a language.
        "````text\nRory O'Driscoll\nPartner, Scale\n```\n650.378.6038\n````\n",
        "~~~\nRory O'Driscoll\nPartner, Scale\n```\n",
        # A fence that is never closed runs to the end of the text.
        "Like this.\n\n```\nRory O'Driscoll\nPartner, Scale\n650.378.6038\n",
        "Thanks.\n\n~~~~~~~~\nRory O'Driscoll\nPartner, Scale\n650.378.6038\n",
        # A code block under the name is the message too, not the sender's details.
        "Thanks.\n\nRory O'Driscoll\nPartner, Scale\n```\nprint('x')\n```\n",
        # So is a table under the name.
        "Rory O'Driscoll\n| Metric | Value |\n| --- | --- |\n| Revenue | 42 |\n",
        # An indented block is code as well: People reads the retained indentation.
        "Here is the format I mean.\n\n    Rory O'Driscoll\n    Partner, Scale\n    650.378.6038\n",
        "Here is the format I mean.\n\n\tRory O'Driscoll\n\tPartner, Scale\n",
    ],
)
def test_a_signature_shaped_example_in_a_code_block_is_not_read(body: str) -> None:
    assert sender_contact(body, RORY) is None


@pytest.mark.parametrize(
    "body",
    [
        "Format it like this:\n\n> Rory O'Driscoll\n> Partner, Scale\n> 650.378.6038\n",
        "| Name | Title |\n| --- | --- |\n| Rory O'Driscoll | Partner, Scale |\n",
    ],
)
def test_a_quoted_or_tabled_example_never_was_a_signature(body: str) -> None:
    """A quoted line is never the name line, and a table row is not exactly the name."""
    assert sender_contact(body, RORY) is None


@pytest.mark.parametrize(
    "above",
    [
        # A closed fence is over: the signature under it is the sender's own.
        "```\nmake check\n```\n\nThanks.\n\n",
        "Run this:\n\n~~~sh\nmake check\n~~~\n\nThanks.\n\n",
        # What a fence holds is code, so a header or a quote mark in it ends nothing.
        "Headers look like this:\n\n```\nFrom: someone\n> quoted\n```\n\nThanks.\n\n",
        # The fenced block is what the colon introduced, so no list is left open.
        "Run this:\n\n```\nmake check\n```\n\n",
        # A backtick line whose label holds a backtick opens no fence.
        "```a`b\n\nThanks.\n\n",
        # Up to three spaces of indentation are not a code block.
        "Thanks.\n\n   ",
    ],
)
def test_a_signature_outside_any_code_block_is_still_read(above: str) -> None:
    body = above + "Rory O'Driscoll\nPartner, Scale\n650.378.6038\n"
    assert sender_contact(body, RORY) == SignatureContact(
        title="Partner, Scale", phones=("650.378.6038",)
    )


def test_the_fence_rule_is_the_readers_and_stays_quick() -> None:
    """One definition says what a fence holds, for the reader and for People."""
    import time

    from agent_core.domain.email_signature import fence_after

    fence = None
    seen = []
    for line in ["````", "```", "~~~~", "````a", "`````  ", "```", "~~~", "```"]:
        fence = fence_after(line, fence)
        seen.append(fence)
    # Only as long a run of the same mark, with nothing after it, closes a fence.
    assert seen == [("`", 4), ("`", 4), ("`", 4), ("`", 4), None, ("`", 3), ("`", 3), None]
    assert fence_after("```a`b", None) is None and fence_after("~~~a`b", None) == ("~", 3)
    started = time.monotonic()
    sender_contact("\n".join(["```", "`" * 200_000, "~" * 200_000, "```"] * 50), RORY)
    sender_contact("\n".join(["```", "Rory O'Driscoll", "```"] * 20_000), RORY)
    assert time.monotonic() - started < 5


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
