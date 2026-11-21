"""A list of names in a known sender's mail writes no contact details to People (ADR-0336)."""

from __future__ import annotations

import pytest

from tests.unit.test_people_signature_contacts import _current, _details, _receive, _stack


@pytest.mark.parametrize(
    "body",
    [
        # The sender is one of the people a colon introduces, below the first of them.
        "Attendees:\n\nAlex Rivera\nRory O'Driscoll\nSam Example\n",
        "Cc: Alex Rivera\nRory O'Driscoll\nSam Example\n",
        "*Attendees*\n\nAlex Rivera\n\nRory O'Driscoll\n\nSam Example\n",
        "Present\n* Alex Rivera\n* Rory O'Driscoll\n* Sam Example\n",
    ],
)
async def test_the_senders_name_in_a_list_gives_people_no_title(body: str) -> None:
    """The entry under the sender's name is another person, not the sender's title."""
    factory, service = await _stack()
    await _receive(factory, service, "m-list", body=body)
    assert await _details(factory) == []


async def test_a_signature_under_a_list_that_ended_still_updates_people() -> None:
    factory, service = await _stack()
    body = (
        "Attendees:\nAlex Rivera\nSam Example\n\nSee you all there.\n\n"
        "Rory O'Driscoll\nPartner, Scale\n650.378.6038\n"
    )
    await _receive(factory, service, "m-signed", body=body)
    assert _current(await _details(factory)) == {
        ("phone", "650.378.6038"),
        ("role", "Partner, Scale"),
    }
