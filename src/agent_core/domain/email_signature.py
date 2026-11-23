"""A writer's own signature block, found in text without a model (ADR-0336).

The Email reader leaves the block out of the reading text; People reads a known
sender's contact details from it. Both use the one rule here, so what the owner
no longer sees and what People reads are the same lines. Which lines of a text
are code, fenced or indented, is decided here too and nowhere else, from the
lines as written, so no caller's stripping or reflow can hide a code block.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
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
# A fence line: up to three spaces, then three or more backticks or tildes.
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")

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
# What a writer closes with before signing, with or without its comma.
_CLOSING = re.compile(
    r"(?:all the best|all best|best(?: regards| wishes)?|(?:kind|warm|warmest|many) regards|"
    r"regards|sincerely|yours(?: sincerely| truly)?|cheers|warmly|cordially|respectfully|"
    r"(?:many |with )?thanks(?: again| so much| very much)?|thank you(?: so much| very much)?|"
    r"take care|talk soon|speak soon)",
    re.I,
)
# A name or company suffix ends in a period without ending a sentence.
_SUFFIXES = frozenset({"jr", "sr", "inc", "corp", "ltd", "co", "llc", "plc", "esq", "phd", "md"})
_LIST_ITEM = re.compile(r"(?:[-*+\u2022]|\d{1,3}[.)])\s")
_HEADING = re.compile(r"#{1,6}\s")
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


def fence_after(line: str, fence: tuple[str, int] | None) -> tuple[str, int] | None:
    """The code fence open after this line: its mark and length, as CommonMark closes it.

    The one definition of what a fence holds, for the reader's placement and for
    signatures. A fence that is never closed holds everything under it.
    """
    marker = _FENCE.fullmatch(line)
    if not marker:
        return fence
    run, rest = marker[1], marker[2]
    if fence is None:
        # A backtick fence's info string cannot itself hold a backtick.
        return (run[0], len(run)) if run[0] != "`" or "`" not in rest else None
    return None if run[0] == fence[0] and len(run) >= fence[1] and not rest.strip() else fence


def _columns(line: str) -> int:
    """How far a line is set in: spaces, and tabs to the next stop of four."""
    columns = 0
    # Past this a line is deep whatever follows, and the count stays cheap.
    for character in line[:64]:
        if character == " ":
            columns += 1
        elif character == "\t":
            columns += 4 - columns % 4
        else:
            break
    return columns


def code_lines(source: Sequence[str]) -> list[bool]:
    """Which lines of a text, as it was written, stand in a code block (ADR-0336).

    The one place that decides it. Callers pass the lines before stripping or
    reflowing them and carry the answer with the lines, so a signature-shaped
    example in a code block is never taken for a signature.

    A fenced block is code, closed or not. So is an indented block: lines set
    four columns or a tab in from the text's own margin, starting under a blank
    line or under a line that ends in a colon, and running across blank lines
    while the indentation holds. An indented line is not code where a mailer
    indents ordinary text: continuing a paragraph, under a list item, in a
    quoted line, or under a reply or forward header.
    """
    texts = [line.strip() for line in source]
    columns = [_columns(line) for line in source]
    margin = min(
        (columns[index] for index, text in enumerate(texts) if text and text[0] != ">"),
        default=0,
    )
    coded = [False] * len(texts)
    fence: tuple[str, int] | None = None
    # In an indented block; under a list item; under a reply or forward header.
    code = listed = history = False
    # What the line above allows: it was blank, or it ends in a colon.
    blank, colon = True, False
    for index, text in enumerate(texts):
        inside, fence = fence, fence_after(text, fence)
        if inside or fence:
            coded[index] = True
            code, blank, colon = False, True, False
            continue
        if not text:
            blank = True
            continue
        deep = columns[index] - margin >= 4
        if text[0] == ">":
            code = listed = False
        elif deep and code:
            coded[index] = True
        elif history:
            code = False
        elif starts_history(text) and (text[0] != "-" or text[-1] == "-"):
            # A header proper. A line that only starts with dashes, such as the
            # comment `-- get users`, is not one, and may be code.
            history, code = True, False
        elif _LIST_ITEM.match(text):
            code, listed = False, True
        elif deep and (blank or colon) and not listed:
            coded[index] = code = True
        elif not deep:
            code = False
            if blank:
                # A paragraph of its own ends the list above it.
                listed = False
        blank = False
        colon = not coded[index] and text.rstrip("*_\\ ").endswith(":")
    return coded


def _list_continues(line: str, listed: bool) -> bool:
    """Whether the lines under this one are entries of a list that a line above introduced.

    A list is introduced by a line ending in a colon, a short label and a colon
    before its first entry (`Cc: Alex Rivera`), a bold line or a heading. It runs
    across blank lines until a finished sentence, the closing a writer signs
    under, a rule or a quoted line. Every test here is linear in the line.
    """
    if not line or line.isspace():
        return listed
    if line.startswith(">") or len(line) > 2000 or not line.strip("-_* "):
        # A quote, a paragraph, or the delimiter a signature is written under.
        return False
    text = line.rstrip("*_\\ ")
    last = text[-1:]
    if last == "!" or (last == "?" and len(text.split(None, 3)) >= 3):
        # `Sam Example?` is an entry someone is unsure of, not a question.
        return False
    if last == ".":
        # `Alex R.` and `Sam Example Jr.` are entries too: an initial or a suffix.
        word = text.rsplit(None, 1)[-1].strip(".,").replace(".", "")
        if len(word) > 1 and word.casefold() not in _SUFFIXES:
            return False
    if len(text) <= 40 and _CLOSING.fullmatch(text.lstrip("*_ ").rstrip(",")):
        return False
    colon = text.find(":")
    return (
        listed
        or last == ":"
        or bool(_HEADING.match(line))
        # A whole line in emphasis, short enough to be a heading and not a bullet.
        or (
            len(line) <= SIGNATURE_LINE_WIDTH
            and line[0] in "*_"
            and line[-1] in "*_"
            and not _LIST_ITEM.match(line)
        )
        or (
            0 < colon <= 40
            and text[colon + 1 : colon + 4].lstrip("*_")[:1].isspace()
            and len(text[:colon].split()) <= 3
        )
    )


def named_signature(
    lines: Sequence[str],
    name: str,
    *,
    visible: Callable[[str], str],
    own_text_only: bool = False,
    code: Sequence[bool] | None = None,
) -> tuple[int, int] | None:
    """The writer's own name line and the end of the short lines under it.

    The name alone is a sign-off. Prose, a postscript or a long block under it
    is the message, and so is anything followed by more than history. A name
    inside a list is an entry of it, wherever in the list it stands. A code
    block is the message: nothing in it is a signature, and code or a table
    under the name makes that block the message too. `code` says which lines
    are code, as `code_lines` read them from the text before it was stripped;
    without it only a fence can be seen in `lines`. With
    `own_text_only`, the name must come before any quoted line or reply or
    forward header, so a signature inside history is never found.
    """
    coded = code if code is not None and len(code) == len(lines) else code_lines(lines)
    above = ""
    below = False
    for index, line in enumerate(lines):
        if coded[index]:
            # A line of a code block. The block is one unit of the message, so
            # it also ends a list and is no heading of one.
            above, below = "", False
            continue
        # The nearest line with text above this one, across any blank lines, and
        # whether this line is an entry of a list that a line above introduced.
        heading, above = above, line or above
        listed, below = below, _list_continues(line, below)
        if own_text_only and (line.startswith(">") or starts_history(line)):
            return None
        # A cheap bound first: the scan must stay linear on a hostile long line.
        if line.startswith(">") or not len(name) <= len(line) <= len(name) + 8:
            continue
        if visible(line).casefold() != name:
            continue
        if (
            listed
            or _LIST_ITEM.match(line)
            or (len(heading) <= 2000 and visible(heading).rstrip("\\").endswith(":"))
        ):
            # A list's entry, bulleted or under the line that introduces the list.
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
                    # A code block or a table row under the name is the message.
                    or coded[end]
                    or (following.startswith("|") and following.endswith("|"))
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
    source = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [line.strip() for line in source]
    found = named_signature(
        lines, name, visible=_written, own_text_only=True, code=code_lines(source)
    )
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


def signature_end(
    sent_at: datetime, key: str, signatures: Mapping[datetime, Collection[str]]
) -> datetime | None:
    """When a detail one signature stated stops holding; None while it holds.

    `signatures` is what each of the sender's remaining signatures stated of
    one kind of detail, by sent time. The detail ends at the earliest later one
    that omits it. Only what remains is read, so reading a message late and
    erasing one both leave the intervals the remaining mail alone would give.
    """
    return min(
        (at for at, said in signatures.items() if at > sent_at and key not in said), default=None
    )
