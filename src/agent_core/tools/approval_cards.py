"""Show a whole change inside what the approval view keeps.

The approval view keeps 50 items a list, cuts longer strings, and replaces a whole
string that holds a credential shape. A card that publishes something pages its diff
within those limits, so what the owner reads is everything the approval covers.
"""

from agent_core.domain.argument_views import APPROVAL_ARGUMENT_CEILING, CREDENTIAL_SHAPE

# The most a card can show whole. A change past this cannot be approved into publication.
CARD_DIFF_ENTRIES = 400
_CARD_PAGE_ENTRIES = 50
_CARD_ENTRY_CHARACTERS = APPROVAL_ARGUMENT_CEILING - 2
_CARD_CONTINUED = "> "
_CARD_HIDDEN = "[credential-shaped text hidden]"


def card_entries(text: str) -> tuple[list[str], int]:
    """Split text into entries the approval view keeps whole, and count what was masked.

    The view replaces a whole string that holds a credential shape, which would let a
    line hide its code behind one `token: x`. Masking only the match keeps the rest.
    """
    masked, hidden = CREDENTIAL_SHAPE.subn(_CARD_HIDDEN, text)
    entries = [masked[:_CARD_ENTRY_CHARACTERS]]
    for start in range(_CARD_ENTRY_CHARACTERS, len(masked), _CARD_ENTRY_CHARACTERS):
        entries.append(_CARD_CONTINUED + masked[start : start + _CARD_ENTRY_CHARACTERS])
    return entries, hidden


def card_diff(lines: list[str]) -> tuple[list[str], int]:
    """The diff's card entries, and how many credential shapes they mask."""
    entries: list[str] = []
    hidden = 0
    for line in lines:
        pieces, masked = card_entries(line)
        entries.extend(pieces)
        hidden += masked
    return entries, hidden


def fits_card(lines: list[str]) -> bool:
    """Whether the owner's approval card can show this whole diff."""
    return len(card_diff(lines)[0]) <= CARD_DIFF_ENTRIES


def card_pages(entries: list[str]) -> dict[str, list[str]]:
    """The entries as named pages, each short enough for the view to keep."""
    return {
        f"{page // _CARD_PAGE_ENTRIES + 1:03d}": entries[page : page + _CARD_PAGE_ENTRIES]
        for page in range(0, len(entries), _CARD_PAGE_ENTRIES)
    }


def hidden_sentence(hidden: int) -> str:
    """The words a card adds when it masks credential-shaped text."""
    return f"{hidden} piece(s) of text shaped like a credential are hidden here."
