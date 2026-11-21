"""A writer's own signature block, found in text without a model (ADR-0336).

The Email reader leaves the block out of the reading text; People reads a known
sender's contact details from it. Both use the one rule here, so what the owner
no longer sees and what People reads are the same lines.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from email.utils import parseaddr

# Where People keeps what a signature states: a reserved, display-only namespace.
SIGNATURE_NAMESPACE = "signature"
SIGNATURE_CONTEXT = "email signature"
# A longer block under a delimiter or a name is the message, not a signature.
SIGNATURE_LINES = 12
SIGNATURE_LINE_WIDTH = 100
# What may follow a signature: the reply or forward it was written above.
HISTORY = re.compile(
    r"On\s.*(?:\d{4}|wrote:)|-{2,}.*|_{5,}|From:\s.*|Begin forwarded message:.*", re.I
)
POSTSCRIPT = re.compile(r"P\.?\s?S\b", re.I)

# Gmail's plain text repeats an anchor or a detected number as a `<target>`.
_TARGET = re.compile(r"<[^<>]{0,512}>")
# Written with a `+`, or in the ten-digit North American shape. Any other
# national shape would need a country, which a signature does not give.
_PHONE = re.compile(
    r"(?<![\w+.-])"
    r"(?:\+\d[\d ().-]{6,18}\d|(?:1[ .-])?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4})"
    r"(?![\w-])"
)
_FAX_LABEL = re.compile(r"\b(?:fax|f)\s*[:.]?\s*$", re.I)
_NOT_A_TITLE = re.compile(r"\d|@|://|\bwww\.|^sent from\b|^get outlook\b", re.I)
_PHONES = 4


@dataclass(frozen=True)
class SignatureContact:
    """What a sender's own signature states about them, exactly as written."""

    title: str | None
    phones: tuple[str, ...]


def sender_name(sender: str) -> str:
    """The From display name a signature must repeat, or "" when it is too short to."""
    name = " ".join(parseaddr(sender)[0].split()).casefold()
    return name if len(name.split()) >= 2 else ""


def starts_history(line: str) -> bool:
    """A reply or forward header; a bare delimiter or rule is not one."""
    return len(line) < 400 and bool(HISTORY.match(line)) and bool(line.strip("-_ "))


def named_signature(
    lines: Sequence[str],
    name: str,
    *,
    visible: Callable[[str], str],
    own_text_only: bool = False,
) -> tuple[int, int] | None:
    """The writer's own name line and the end of the short lines under it.

    The name alone is a sign-off. Prose, a postscript or a long block under it
    is the message, and so is anything followed by more than history. With
    `own_text_only`, the name must come before any quoted line or reply or
    forward header, so a signature inside history is never found.
    """
    above = ""
    for index, line in enumerate(lines):
        # The nearest line with text above this one, across any blank lines.
        heading, above = above, line or above
        if own_text_only and (line.startswith(">") or starts_history(line)):
            return None
        # A cheap bound first: the scan must stay linear on a hostile long line.
        if line.startswith(">") or not len(name) <= len(line) <= len(name) + 8:
            continue
        if visible(line).casefold() != name:
            continue
        if len(heading) <= 2000 and visible(heading).rstrip("\\").endswith(":"):
            # A line ending in a colon introduces a list the name belongs to.
            continue
        end = index + 1
        block = 0
        while end < len(lines):
            following = lines[end]
            if following.startswith(">") or starts_history(following):
                break
            if following:
                text = "" if len(following) > 2000 else visible(following)
                block += 1
                if (
                    block > SIGNATURE_LINES
                    or not 0 < len(text) <= SIGNATURE_LINE_WIDTH
                    or POSTSCRIPT.match(text)
                    or (len(text.split()) > 8 and text[-1] in ".?!")
                ):
                    block = 0
                    break
            end += 1
        if block:
            return index, end
    return None


def _written(line: str) -> str:
    """A retained line without the emphasis marks a mailer's plain text adds."""
    return line.strip().strip("*_").strip()


def phone_key(value: str) -> str:
    """One number however it was punctuated; never an E.164 value."""
    return re.sub(r"[ ().-]", "", value)


def sender_contact(body: str, sender: str) -> SignatureContact | None:
    """The contact details in the sender's own signature, or None without one.

    `body` is the retained plain text. Every returned value is an exact
    substring of it, and nothing is read from quoted or forwarded text.
    """
    name = sender_name(sender)
    if not name:
        return None
    lines = [line.strip() for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    found = named_signature(lines, name, visible=_written, own_text_only=True)
    if found is None:
        return None
    block = [_written(line) for line in lines[found[0] + 1 : found[1]] if line]
    title = block[0] if block and not _NOT_A_TITLE.search(block[0]) else None
    if title is not None and not any(character.isalpha() for character in title):
        title = None
    phones: dict[str, str] = {}
    for line in block:
        text = _TARGET.sub(" ", line)
        position = 0
        for match in _PHONE.finditer(text):
            label, position = text[position : match.start()], match.end()
            written = match[0]
            key = phone_key(written)
            if (
                _FAX_LABEL.search(label)
                # A number split by a removed target is not text the sender wrote.
                or written not in line
                or not 7 <= len(key.lstrip("+")) <= 15
                or key in phones
                or len(phones) == _PHONES
            ):
                continue
            phones[key] = written
    if title is None and not phones:
        return None
    return SignatureContact(title=title, phones=tuple(phones.values()))
