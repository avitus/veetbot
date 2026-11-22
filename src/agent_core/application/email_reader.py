"""Presentation-only email cleanup; never used as learning or reply evidence.

No model, network request or persistent derived copy is involved. Ambiguous prose
survives; the client always offers the unchanged retained source alongside this view.
"""

import re
from bisect import bisect_left
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from itertools import groupby
from urllib.parse import urlsplit

from agent_core.domain.email_signature import HISTORY as _HISTORY
from agent_core.domain.email_signature import POSTSCRIPT as _POSTSCRIPT
from agent_core.domain.email_signature import SIGNATURE_LINES as _SIGNATURE_LINES
from agent_core.domain.email_signature import (
    code_lines,
    fence_after,
    named_signature,
    sender_name,
)
from agent_core.domain.web import is_public_https_url

_INVISIBLE = str.maketrans("", "", "\u00ad\u034f\u200b\ufeff")
_URL_LINE = re.compile(r"^<?(https?://[^\s<>]+)>?([*_]?)$", re.I)
_HTML = re.compile(r"</?(?:html|body|div|p|h[1-6]|a|br|table|span|script|style|img)\b[^>]*>", re.I)
_PROMOTIONS = re.compile(
    r"^(?:forwarded this email\? subscribe for more|unsubscribe|manage (?:email )?preferences|"
    r"view (?:this email )?in (?:your )?browser|view in app|comment|like|share this story|"
    r"get shareable link|download on the app store|\[image: download on the app store\])$",
    re.I,
)
_QUOTE_PREFIX = re.compile(r"(?:>[ \t]?)+")
# Gmail's plain text writes an anchor as `text <url>` and repeats a phone number
# it detected as a percent-encoded target.
_LINK_TARGET = re.compile(r"<(https?://[^\s<>]+)>", re.I)
_PHONE_TARGET = re.compile(r"(?<=\d)\s*<\+?\(?\d[\d()%.+-]{5,}>")
_LABEL_BOUNDARY = re.compile(r"[\s|<>\[\]]")
_LABELED_LINE = re.compile(r"\[([^\]]+)\]\(https?://[^\s]+\)|([^<>]+)<https?://[^\s<>]+>")
_LINK_SEPARATOR = re.compile(r"\)\s*\|\s*\[")
# Mailers wrap only when the next word would not fit, and few wrap narrower than this.
_WRAP_WIDTH = 70
_BLOCK_START = re.compile(r" {4}|\t|#{1,6}(?:\s|$)|[-*+]\s|\d+[.)]\s")
_RULE = re.compile(r"(?:[-*_]\s*){3,}")
_UNDERLINE = re.compile(r"-+|=+")
_TABLE_RULE = re.compile(r"[\s|:-]+")
_ORDERED_MARKER = re.compile(r"^\d{1,3}[.)]\s+")
# A reader image marker, whose alt text is no word of the HTML text.
_IMAGE_MARKER = re.compile(r"!\[[^\]\n]*\]\([^)\s]*\)")
# Fewer lines than this can repeat by chance; a sign-off is not a signature.
_REPEATED_BLOCK_LINES = 3
# How Gmail, Outlook and Apple Mail mark the signature they insert.
_SIGNATURE_CLASSES = frozenset({"gmail_signature", "gmail_signature_prefix"})
_SIGNATURE_IDS = frozenset({"signature", "ms-outlook-mobile-signature", "applemailsignature"})
# Where a link destination stops: its closing parenthesis, or whitespace it cannot hold.
_DESTINATION_END = re.compile(r"[\s)]")
_HOST_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)\\?")


def _safe_url(value: str) -> str | None:
    """Allow explicit web destinations only; preserve signed queries without visiting them."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if any(ord(char) < 33 for char in value) or "\\" in value:
            return None
        return value.replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")
    except ValueError:
        return None


_LINK_SYNTAX = str.maketrans({"\\": r"\\", "[": r"\[", "]": r"\]", "<": r"\<"})


def _inert(text: str) -> str:
    """Escape what would let sender text author a Markdown link, image or markup.

    Emphasis and list markers stay: they format text and cannot make a link.
    """
    return text.translate(_LINK_SYNTAX)


def _link(label: str, url: str) -> str:
    """Link an already inert label to a checked destination."""
    safe = _safe_url(url)
    return f"[{label}]({safe})" if safe else label


def _host(url: str) -> str:
    """The link's host for its label, or a plain label when the address has none."""
    try:
        return urlsplit(url).hostname or "Open link"
    except ValueError:
        return "Open link"


def _may_name(label: str, url: str) -> bool:
    """A label shaped like an address may name only its own destination."""
    return not re.search(r"[./:@]", label) or (
        label.lower().removeprefix("www.") == _host(url).lower().removeprefix("www.")
    )


def _with_targets(text: str) -> str:
    """Escape sender text and link the word before each target a mailer wrote for an anchor."""
    text = _PHONE_TARGET.sub("", text)
    parts: list[str] = []
    position = 0
    for target in _LINK_TARGET.finditer(text):
        before = text[position : target.start()].rstrip()
        label = _LABEL_BOUNDARY.split(before)[-1]
        if label[-1:].isalnum() and _may_name(label, target[1]):
            parts += [_inert(before[: -len(label)]), _link(_inert(label), target[1])]
        else:
            parts += [_inert(before), " " if before else "", _link(_host(target[1]), target[1])]
        position = target.end()
    parts.append(_inert(text[position:]))
    return _LINK_SEPARATOR.sub(") | [", "".join(parts))


def _quoted(line: str) -> tuple[str, str]:
    """Split a line into its quote prefix and what it says."""
    prefix = _QUOTE_PREFIX.match(line)
    return (prefix[0], line[prefix.end() :]) if prefix else ("", line)


def _starts_block(content: str) -> bool:
    """Report a line that begins its own block, so it never continues a paragraph."""
    return bool(_BLOCK_START.match(content) or _RULE.fullmatch(content))


def _unwrap(lines: list[str], coded: list[bool]) -> tuple[list[str], list[bool]]:
    """Rejoin what a mailer wrapped; a line with room for the next word ended on purpose.

    `coded` says which lines are code and is returned for the lines that remain.
    A line of code is the writer's own line: it joins no line above it and no
    line below joins it, so each remaining line is code as a whole or not at all.
    """
    # Each line's pieces join once at the end: growing a string here is quadratic.
    result: list[list[str]] = []
    kept: list[bool] = []
    for index, line in enumerate(lines):
        previous = lines[index - 1] if index else ""
        prefix, content = _quoted(line)
        previous_prefix, before = _quoted(previous)
        if before and content and prefix.count(">") == previous_prefix.count(">"):
            if _UNDERLINE.fullmatch(content):
                # A sender's rule or signature delimiter must not underline a heading.
                result.append([prefix.rstrip()])
                kept.append(False)
            elif (
                not coded[index]
                and not coded[index - 1]
                and not _starts_block(content)
                and not _RULE.fullmatch(before)
                and not _UNDERLINE.fullmatch(before)
                and not _URL_LINE.fullmatch(before)
                # A lone URL labels its whole line before, unless that line has targets.
                and (not _URL_LINE.fullmatch(content) or _LINK_TARGET.search(before))
                and (
                    # A target beginning a line belongs to the word that ended the last.
                    content.lower().startswith("<http")
                    or len(previous) + 1 + len(content.split()[0]) > _WRAP_WIDTH
                )
            ):
                result[-1].append(content.strip())
                continue
        result.append([line])
        kept.append(coded[index])
    return [" ".join(parts) for parts in result], kept


def _without_repeated_blocks(lines: list[str], coded: list[bool]) -> list[str]:
    """Show a signature once, however each quote level wrapped it.

    A code block that repeats an earlier block is an example of it and stays.
    """

    def depth(line: str) -> int | None:
        """The quote level of a line with text, or None for a blank one."""
        prefix, content = _quoted(line)
        return prefix.count(">") if content else None

    result: list[str] = []
    seen: set[str] = set()
    for level, group in groupby(lines, key=depth):
        block = list(group)
        start = len(result)
        if level is not None and len(block) >= _REPEATED_BLOCK_LINES:
            key = "".join("".join(_quoted(line)[1].split()) for line in block).casefold()
            if key in seen and not any(coded[start : start + len(block)]):
                # Blank at its own depth, so the quote around it stays one quote.
                block = [">" * level] * len(block)
            seen.add(key)
        result += block
    return result


def _without_delimited_signatures(lines: list[str], coded: list[bool]) -> list[str]:
    """Blank a `-- ` delimiter and the unbroken lines under it, at each quote level.

    Only a block that ends its level's text is a signature or a list footer: a
    rule between two sections has more of the message below it. A postscript
    line in the block is the message and stays. Any other sentence there goes
    with the block: under a delimiter it is a disclaimer or a list footer.
    A delimiter in a code block is an example of one and starts no signature.
    """
    result = list(lines)
    for index, line in enumerate(lines):
        prefix, content = _quoted(line)
        if content != "--" or coded[index]:
            continue
        depth = prefix.count(">")
        end = index + 1
        while end < len(lines) and end - index <= _SIGNATURE_LINES + 1:
            inner_prefix, inner = _quoted(lines[end])
            if not inner or inner_prefix.count(">") != depth or coded[end]:
                break
            end += 1
        if end == index + 1 or end - index > _SIGNATURE_LINES + 1:
            continue
        after = end
        while after < len(lines) and not _quoted(lines[after])[1]:
            after += 1
        if after < len(lines):
            next_prefix, following = _quoted(lines[after])
            if next_prefix.count(">") == depth and not _HISTORY.match(following):
                continue
        result[index:end] = [
            kept if _POSTSCRIPT.match(_quoted(kept)[1].strip("*_ ")) else ">" * depth
            for kept in lines[index:end]
        ]
    return result


def _destinations(text: str) -> Iterator[tuple[int, int]]:
    """Each `](http…)` link destination, as where its `](` starts and where it ends.

    One pass over the text. A run with no closing parenthesis before its next
    whitespace holds no destination, wherever in it a link seems to start, so the
    scan resumes after the run instead of trying each start in it again. A
    sender can write such a run as long as the message.
    """
    position = 0
    while (start := text.find("](http", position)) != -1:
        begin = start + 2
        if not text.startswith(("http://", "https://"), begin):
            position = begin
            continue
        end = _DESTINATION_END.search(text, begin)
        if end is None:
            # No parenthesis or whitespace is left, so nothing later closes either.
            return
        if end[0] == ")":
            yield start, end.end()
        position = end.end()


def _visible(line: str) -> str:
    """A rendered line as its reader sees it: no destinations, markup or line break."""
    kept: list[str] = []
    position = 0
    for start, end in _destinations(line):
        kept.append(line[position : start + 1])
        position = end
    kept.append(line[position:])
    return re.sub(r"[\[\]*_\\]", "", "".join(kept)).strip()


def _without_named_signature(lines: list[str], sender: str, coded: list[bool]) -> list[str]:
    """Blank the writer's own name and the short lines under it that end their text.

    The name alone is a sign-off and stays. Prose, a postscript or a long block
    under it is the message, and so is anything followed by more than history.
    `coded` says which lines are code: a block there is an example and stays.
    People reads the same block for a sender's contact details (ADR-0336).
    """
    name = sender_name(sender)
    found = named_signature(lines, name, visible=_visible, code=coded) if name else None
    if found is None:
        return lines
    index, end = found
    # An HTML signature's delimiter is its own paragraph above the name.
    start = index
    while start and not lines[start - 1]:
        start -= 1
    delimited = start and lines[start - 1].rstrip("\\") == "--" and not coded[start - 1]
    start = start - 1 if delimited else index
    return [*lines[:start], *[""] * (end - start), *lines[end:]]


def _with_line_breaks(lines: list[str], flowing: set[int]) -> list[str]:
    """Mark each line the writer ended, except where its paragraph ends anyway."""
    result = list(lines)
    for index, line in enumerate(lines[:-1]):
        following = lines[index + 1]
        if index in flowing or line.endswith("\n") or following.startswith("\n"):
            continue
        prefix, content = _quoted(line)
        following_prefix, next_content = _quoted(following)
        after = _quoted(lines[index + 2])[1] if index + 2 < len(lines) else ""
        if (
            content
            and next_content
            and prefix.count(">") == following_prefix.count(">")
            and not _starts_block(content)
            and not _starts_block(next_content)
            and not _TABLE_RULE.fullmatch(after)
            and not ("|" in content and "|" in next_content)
        ):
            result[index] = line + "\\"
    return result


@dataclass
class _Table:
    """Offsets into sanitized parts; unsupported layouts keep their existing prose."""

    start: int
    eligible: bool = True
    rows: list[list[str]] = field(default_factory=list)
    cell_start: int | None = None
    column_span: int = 1

    def markdown(self) -> str | None:
        """Render a closed rectangular data table, leaving unsupported layouts as prose."""
        if not self.eligible or self.cell_start is not None or len(self.rows) < 2:
            return None
        width = len(self.rows[0])
        if width < 2 or any(len(row) != width for row in self.rows):
            return None
        rows = [self.rows[0], ["---"] * width, *self.rows[1:]]
        return "\n\n" + "\n".join("| " + " | ".join(row) + " |" for row in rows) + "\n\n"


class _HTMLText(HTMLParser):
    """Extract inert text and explicit links, without a WebView or resource loader."""

    def __init__(
        self, image_urls: list[str] | None = None, *, keep_signatures: bool = False
    ) -> None:
        """Optionally collect image destinations while emitting inert presentation text."""
        super().__init__(convert_charrefs=True)
        self.image_urls = image_urls
        self.keep_signatures = keep_signatures
        self.parts: list[str] = []
        self.hidden: list[str] = []
        self.anchor: tuple[int, str] | None = None
        self.list_items: list[int] = []
        self.tables: list[_Table] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Retain visible formatting and bounded public image references, never active HTML."""
        values = dict(attrs)
        void = tag in {"br", "hr", "img", "input", "meta", "link", "source", "wbr"}
        if self.hidden:
            if not void:
                self.hidden.append(tag)
            return
        style = re.sub(r"\s+", "", values.get("style") or "").lower()
        classes = set((values.get("class") or "").lower().split())
        # The mailer says which block is the signature; nothing is guessed here.
        signature = not self.keep_signatures and (
            bool(classes & _SIGNATURE_CLASSES)
            or (values.get("id") or "").lower() in _SIGNATURE_IDS
            or (values.get("data-smartmail") or "").lower() == "gmail_signature"
        )
        hidden = (
            tag in {"script", "style", "iframe", "object", "template", "head"}
            or "hidden" in values
            or "display:none" in style
            or "visibility:hidden" in style
            or (values.get("aria-hidden") or "").lower() == "true"
            or bool(classes & {"advertisement", "ad-banner", "preheader"})
            or signature
        )
        if hidden:
            if not void:
                self.hidden.append(tag)
            return
        if tag == "table":
            if self.tables:
                self.tables[-1].eligible = False
            self.tables.append(_Table(len(self.parts), values.get("role") != "presentation"))
        elif self.tables:
            table = self.tables[-1]
            if tag == "caption":
                table.eligible = False
            elif tag == "tr":
                table.rows.append([])
            elif tag in {"td", "th"}:
                if table.cell_start is not None or not table.rows:
                    table.eligible = False
                span = values.get("colspan") or "1"
                table.column_span = int(span) if re.fullmatch(r"[0-9]{1,2}", span) else 0
                if values.get("rowspan", "1") != "1" or not 1 <= table.column_span <= 64:
                    table.eligible = False
                table.cell_start = len(self.parts)
        if tag == "img" and self.image_urls is not None and len(self.image_urls) < 20:
            url = values.get("src") or ""
            dimensions = [values.get("width") or "", values.get("height") or ""]
            dimensions += re.findall(r"(?:^|;)(?:width|height):([0-9.]+)(?:px)?(?=;|$)", style)
            tiny = any(
                float(value) <= 2 for value in dimensions if re.fullmatch(r"\d+(?:\.\d+)?", value)
            )
            if is_public_https_url(url) and not tiny:
                index = len(self.image_urls)
                self.image_urls.append(url)
                label = _inert(re.sub(r"\s+", " ", values.get("alt") or "Image"))[:300]
                anchor = self.anchor
                if anchor:
                    start, href = anchor
                    text = "".join(self.parts[start:]).strip()
                    self.parts[start:] = [_link(text, href)] if text else []
                self.parts.append(f"\n\n![{label}](veetbot-email-image:{index})\n\n")
                if anchor:
                    self.anchor = (len(self.parts), anchor[1])
            return
        if tag in {"p", "div", "table", "tr", "blockquote", "ul", "ol"}:
            # A paragraph wrapper at the start of a list item must not separate
            # its bullet from its text. Later paragraphs keep their boundary.
            if (
                tag in {"p", "div"}
                and self.list_items
                and not any(part.strip() for part in self.parts[self.list_items[-1] :])
            ):
                del self.parts[self.list_items[-1] :]
            else:
                self.parts.append("\n\n")
        elif tag in {"br", "hr"}:
            self.parts.append("\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("\n- ")
            self.list_items.append(len(self.parts))
        elif tag in {"td", "th"}:
            self.parts.append(" ")
        elif tag in {"b", "strong"}:
            self.parts.append("**")
        elif tag in {"i", "em"}:
            self.parts.append("*")
        elif tag == "a":
            self.anchor = (len(self.parts), values.get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        """Close visible structures and render only complete supported tables."""
        if self.hidden:
            if tag in self.hidden:
                index = len(self.hidden) - 1 - self.hidden[::-1].index(tag)
                del self.hidden[index:]
            return
        if self.tables:
            table = self.tables[-1]
            if tag in {"td", "th"} and table.cell_start is not None:
                cell = " ".join("".join(self.parts[table.cell_start :]).split())
                if "veetbot-email-image:" in cell:
                    table.eligible = False
                if table.rows and table.eligible:
                    table.rows[-1].extend(
                        [cell.replace("|", r"\|")] + [""] * (table.column_span - 1)
                    )
                table.cell_start = None
            elif tag == "table":
                self.tables.pop()
                rendered = table.markdown()
                if rendered is not None:
                    self.parts[table.start :] = [rendered]
        if tag == "a" and self.anchor:
            start, url = self.anchor
            label = "".join(self.parts[start:]).strip()
            self.parts[start:] = [_link(label, url)] if label else []
            self.anchor = None
        if tag == "li" and self.list_items:
            self.list_items.pop()
        if tag in {"b", "strong"}:
            self.parts.append("**")
        elif tag in {"i", "em"}:
            self.parts.append("*")
        if tag in {"p", "div", "table", "tr", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(_inert(re.sub(r"\s+", " ", data)))


def _forwarded(lines: list[str], coded: list[bool]) -> tuple[list[str], list[bool]]:
    """Collapse only a recognized, bounded forwarding envelope; retain its source.

    `coded` says which lines are code and is returned for the lines that remain.
    """
    result: list[str] = []
    kept: list[bool] = []
    index = 0
    while index < len(lines):
        if re.fullmatch(
            r"(?:-+\s*Forwarded message\s*-+|Begin forwarded message:)", lines[index], re.I
        ):
            end = index + 1
            headers: list[str] = []
            while end < min(index + 16, len(lines)) and lines[end]:
                headers.append(lines[end])
                end += 1
            sender = next(
                (line[5:].strip() for line in headers if line.lower().startswith("from:")), None
            )
            if (
                sender
                and any(line.lower().startswith("subject:") for line in headers)
                and end < len(lines)
                and not lines[end]
            ):
                result.extend(["", "> Forwarded from " + sender, ""])
                kept.extend([False] * 3)
                index = end + 1
                continue
        result.append(lines[index])
        kept.append(coded[index])
        index += 1
    return result, kept


def reading_source(body: str, html: str) -> str:
    """Recover structure only when the body exactly matches Gmail's HTML fallback.

    The legacy Gmail converter strips tags but retains hidden/style text. Such a
    body is not independent plain MIME prose to merge back into sanitized HTML.
    Replay that conversion for identification only; never render its output.
    Unmatched plain alternatives and the retained source evidence stay intact.
    """
    if html:
        flattened = re.sub(r"<[^>]+>", " ", unescape(html))
        flattened = re.sub(r"[ \t\r\f\v]+", " ", flattened).strip()
        if body == flattened:
            return html
    return body


def reading_body(
    body: str,
    *,
    subject: str = "",
    image_urls: list[str] | None = None,
    sender: str = "",
    keep_signatures: bool = False,
) -> str:
    """Derive safe reading Markdown from retained text, preserving substantive prose.

    A signature is left out of the reading text only; the retained body keeps it.
    `sender` lets an unmarked block under the writer's own name be recognized.
    """
    text = body.translate(_INVISIBLE).replace("\r\n", "\n").replace("\r", "\n")
    # Extracted HTML text is already inert and carries the reader's own links.
    extracted = bool(_HTML.search(text))
    if extracted:
        parser = _HTMLText(image_urls, keep_signatures=keep_signatures)
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    source = text.split("\n")
    lines = [line.strip() for line in source]
    # Which lines are code is read here, from the text as written: the strip
    # above hides an indented block from every pass below. Each pass that moves
    # lines returns the answer for the lines it leaves (ADR-0336). Extracted
    # HTML text has no indentation of the writer's own, only fences.
    lines, coded = _forwarded(lines, code_lines(lines if extracted else source))
    # Lines inside a code fence are the writer's own; a mail with one keeps them all.
    reflows = not extracted and "```" not in text and "~~~" not in text
    if reflows:
        lines, coded = _unwrap(lines, coded)
    if not extracted and not keep_signatures:
        # Code is known line by line, so a fence elsewhere keeps no signature in view.
        lines = _without_delimited_signatures(lines, coded)
    title = re.sub(r"^(?:(?:fwd?|re):\s*)+", "", subject.strip(), flags=re.I)
    # Copyright/address furniture is removable only when directly paired with
    # a newsletter's unsubscribe control near its end, never by chopping a tail.
    for start, line in enumerate(lines):
        if start < len(lines) * 0.65 or not re.fullmatch(r"©\s*\d{4}\s+.{1,100}", line):
            continue
        for end in range(start + 1, min(start + 4, len(lines))):
            if lines[end].lower() == "unsubscribe":
                between = lines[start + 1 : end]
                if all(
                    not item or re.match(r"^\d+\s+.{0,150}\b\d{5}(?:-\d{4})?$", item)
                    for item in between
                ):
                    lines[start:end] = [""] * (end - start)
                break
    result: list[str] = []
    # Which lines of `result` are code, as `coded` says for `lines`.
    kept: list[bool] = []
    # A link line made from the line before it continues that line's sentence.
    flowing: set[int] = set()
    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            re.fullmatch(r"-{3,}", line)
            and index + 1 < len(lines)
            and lines[index + 1].lower() == "advertisement"
        ):
            ad_end = next(
                (
                    end
                    for end in range(index + 2, min(index + 14, len(lines)))
                    if lines[end] == line
                ),
                None,
            )
            if ad_end is not None:
                index = ad_end + 1
                continue
        if (
            line.lower()
            == (
                "this email and any attachments are confidential "
                "and intended solely for the named recipient."
            )
            and (index == 0 or not lines[index - 1])
            and (index + 1 == len(lines) or not lines[index + 1])
        ):
            index += 1
            continue
        following = _URL_LINE.fullmatch(lines[index + 1]) if index + 1 < len(lines) else None
        # Exact UI labels are clutter only when accompanied by a destination.
        embedded = _LABELED_LINE.fullmatch(line)
        label = (embedded[1] or embedded[2].rstrip()) if embedded else line
        if _PROMOTIONS.fullmatch(label) and (following or embedded):
            index += 2 if following else 1
            continue
        prefix, content = _quoted(line)
        match = _URL_LINE.fullmatch(content)
        if match:
            url, closing_emphasis = match.groups()
            previous_prefix, before = _quoted(result[-1]) if result else ("", "")
            if before and previous_prefix.count(">") == prefix.count(">") and "](" not in before:
                result[-1] = prefix + _link(before + closing_emphasis, url)
                # The line it labels now holds a line of code, so it is code.
                kept[-1] = kept[-1] or coded[index]
                flowing.add(len(result) - 1)
            else:
                result.append(prefix + _link(_host(url), url) + closing_emphasis)
                kept.append(coded[index])
        else:
            result.append(line if extracted else _with_targets(line))
            kept.append(coded[index])
        index += 1
    if reflows:
        result = _without_repeated_blocks(result, kept)
    if sender and not keep_signatures:
        result = _without_named_signature(result, sender, kept)
    title = _inert(title)
    if title:
        for index, line in enumerate(result):
            linked = re.fullmatch(r"\[([^\]]+)\]\(https?://[^\s]+\)", line)
            label = linked[1] if linked else line
            if label.casefold() == title.casefold():
                result[index] = "\n## " + line + "\n"
                # A nearby byline and paid badge belong to the article header.
                for byline in range(index + 1, min(index + 8, len(result))):
                    if re.match(r"^(?:\[)?By\s+\S", result[byline]):
                        result[byline] = "\n" + result[byline] + "\n"
                        if byline + 1 < len(result) and result[byline + 1] == "PAID":
                            result[byline + 1] = ""
                        break
                break
    if reflows:
        result = _with_line_breaks(result, flowing)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(result)).strip()
    return cleaned


def merge_image_body(
    plain: str, rich: str, html_text: str, *, require_overlap: bool = False
) -> str:
    """Retain plain MIME paragraphs absent from the image-bearing HTML alternative.

    Compare visible words rather than Markdown formatting, wrapping, punctuation,
    link destinations or list numbering. An unmatched paragraph keeps its place:
    it follows the HTML paragraph holding the last matched plain paragraph before
    it, and precedes the HTML only when no matched paragraph comes first. So a
    reply's history or signature the plain part words differently stays below the
    writer's note, while a note the HTML omits entirely still leads.
    A text-only alternative can require shared prose before replacing the reader.
    A paragraph is judged line by line, so a reply's quoted history is not missing
    for having a repeat shown once. A link the reader could only label by its
    host is there when the HTML links the same destination.
    """

    def words(value: str) -> str:
        """Normalize inert reader Markdown into a bounded visible-word sequence."""
        labels: list[str] = []
        for line in value.splitlines(keepends=True):
            position = 0
            while (start := line.find("](", position)) != -1:
                end = line.find(")", start + 2)
                if end == -1:
                    # Nothing later on this line can close a destination either.
                    break
                labels.append(line[position : start + 1])
                position = end + 1
            labels.append(line[position:])
        return " ".join(re.findall(r"\w+", "".join(labels).casefold()))

    represented = " " + words(html_text) + " "
    linked = {html_text[start + 1 : end] for start, end in _destinations(html_text)}

    def held(text: str) -> str | None:
        """This plain text's words as the HTML text holds them; None when it lacks them."""
        normalized = words(text)
        if not normalized or " " + normalized + " " in represented:
            return normalized
        # The HTML may show an ordered list as bullets; then its numbers are not prose.
        unnumbered = words(
            "\n".join(
                _ORDERED_MARKER.sub("", _quoted(line.strip())[1], count=1)
                for line in text.splitlines()
            )
        )
        if unnumbered != normalized and " " + unnumbered + " " in represented:
            return unnumbered
        return None

    # The plain part in order: the words of what matched, or a missing paragraph.
    plan: list[tuple[bool, str]] = []
    overlaps = False
    for paragraph in re.split(r"\n\s*\n", plain):
        whole = held(paragraph)
        if whole:
            overlaps = True
            plan.append((False, whole))
            continue
        if whole == "":
            continue
        # Not there as one run of words. Judge it line by line: a reply's quoted
        # history is not missing for having a repeat shown once, and a link the
        # reader could only label by its host is there when the HTML links it.
        anchors: list[str] = []
        absent = False
        for line in paragraph.splitlines():
            only_link = _HOST_LINK.fullmatch(_quoted(line.strip())[1])
            if only_link and only_link[1] == _host(only_link[2]) and f"({only_link[2]})" in linked:
                continue
            line_words = held(line)
            if line_words is None:
                absent = True
                break
            if line_words:
                anchors.append(line_words)
        if absent:
            plan.append((True, paragraph))
        elif anchors:
            overlaps = True
            plan.extend((False, anchor) for anchor in anchors)
    if require_overlap and plain and not overlaps:
        return plain
    if not any(is_missing for is_missing, _ in plan):
        return rich
    return _placed(plan, rich, words)


def _placed(plan: list[tuple[bool, str]], rich: str, words: Callable[[str], str]) -> str:
    """Insert each missing paragraph after the rich paragraph holding the match before it.

    Matched lines are found left to right, each after the last, so a repeated
    short line such as a sign-off resolves to its own occurrence and a paragraph
    never moves above one placed earlier. A fenced block is one unit nothing is
    placed inside. A line the rich text does not hold there moves nothing.
    """
    blocks: list[str] = []
    # The open fence's marker character and length, as CommonMark closes it.
    fence: tuple[str, int] | None = None
    for paragraph in rich.split("\n\n"):
        if fence:
            blocks[-1] += "\n\n" + paragraph
        else:
            blocks.append(paragraph)
        for line in paragraph.split("\n"):
            fence = fence_after(line, fence)
    # The word sequence of the rich text, and where each block's words end in it.
    # Image markers carry no words of the HTML text the anchors came from.
    sequence = " "
    ends: list[int] = []
    for block in blocks:
        normalized = words(_IMAGE_MARKER.sub(" ", block))
        if normalized:
            sequence += normalized + " "
        ends.append(len(sequence))
    after: dict[int, list[str]] = {}
    slot = -1
    position = 0
    for is_missing, value in plan:
        if is_missing:
            after.setdefault(slot, []).append(value)
            continue
        found = sequence.find(" " + value + " ", position)
        if found != -1:
            position = found + len(value) + 1
            slot = max(slot, bisect_left(ends, position + 1))
    if fence and after.get(len(blocks) - 1):
        # The HTML text ends inside a fence; close it so placed prose is not code.
        blocks[-1] += "\n" + fence[0] * fence[1]
    result = list(after.get(-1, []))
    for index, block in enumerate(blocks):
        result.append(block)
        result.extend(after.get(index, []))
    return "\n\n".join(result)
