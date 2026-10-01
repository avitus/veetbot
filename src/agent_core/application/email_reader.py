"""Presentation-only email cleanup; never used as learning or reply evidence.

No model, network request or persistent derived copy is involved. Ambiguous prose
survives; the client always offers the unchanged retained source alongside this view.
"""

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

_INVISIBLE = str.maketrans("", "", "\u00ad\u034f\u200b\ufeff")
_URL_LINE = re.compile(r"^<?(https?://[^\s<>]+)>?([*_]?)$", re.I)
_HTML = re.compile(r"</?(?:html|body|div|p|h[1-6]|a|br|table|span|script|style)\b[^>]*>", re.I)
_PROMOTIONS = re.compile(
    r"^(?:forwarded this email\? subscribe for more|unsubscribe|manage (?:email )?preferences|"
    r"view (?:this email )?in (?:your )?browser|view in app|comment|like|share this story|"
    r"get shareable link|download on the app store|\[image: download on the app store\])$",
    re.I,
)


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


def _link(label: str, url: str) -> str:
    safe = _safe_url(url)
    label = label.replace("[", r"\[").replace("]", r"\]")
    return f"[{label}]({safe})" if safe else label


class _HTMLText(HTMLParser):
    """Extract inert text and explicit links, without a WebView or resource loader."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []
        self.anchor: tuple[int, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        void = tag in {"br", "hr", "img", "input", "meta", "link", "source", "wbr"}
        if self.hidden:
            if not void:
                self.hidden.append(tag)
            return
        style = re.sub(r"\s+", "", values.get("style") or "").lower()
        hidden = (
            tag in {"script", "style", "iframe", "object", "template", "head"}
            or "hidden" in values
            or "display:none" in style
            or "visibility:hidden" in style
            or (values.get("aria-hidden") or "").lower() == "true"
            or bool(
                set((values.get("class") or "").lower().split())
                & {"advertisement", "ad-banner", "preheader"}
            )
        )
        if hidden:
            if not void:
                self.hidden.append(tag)
            return
        if tag in {"p", "div", "table", "tr", "blockquote", "ul", "ol"}:
            self.parts.append("\n\n")
        elif tag in {"br", "hr"}:
            self.parts.append("\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in {"td", "th"}:
            self.parts.append(" ")
        elif tag in {"b", "strong"}:
            self.parts.append("**")
        elif tag in {"i", "em"}:
            self.parts.append("*")
        elif tag == "a":
            self.anchor = (len(self.parts), values.get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag in self.hidden:
                index = len(self.hidden) - 1 - self.hidden[::-1].index(tag)
                del self.hidden[index:]
            return
        if tag == "a" and self.anchor:
            start, url = self.anchor
            label = "".join(self.parts[start:]).strip()
            self.parts[start:] = [_link(label, url)] if label else []
            self.anchor = None
        if tag in {"b", "strong"}:
            self.parts.append("**")
        elif tag in {"i", "em"}:
            self.parts.append("*")
        if tag in {"p", "div", "table", "tr", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(re.sub(r"\s+", " ", data))


def _forwarded(lines: list[str]) -> list[str]:
    """Collapse only a recognized, bounded forwarding envelope; retain its source."""
    result: list[str] = []
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
                index = end + 1
                continue
        result.append(lines[index])
        index += 1
    return result


def reading_body(body: str, *, subject: str = "") -> str:
    """Derive safe reading Markdown from retained text, preserving substantive prose."""
    text = body.translate(_INVISIBLE).replace("\r\n", "\n").replace("\r", "\n")
    if _HTML.search(text):
        parser = _HTMLText()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    lines = _forwarded([line.strip() for line in text.split("\n")])
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
        embedded = re.fullmatch(r"\[([^\]]+)\]\(https?://[^\s]+\)", line)
        label = embedded[1] if embedded else line
        if _PROMOTIONS.fullmatch(label) and (following or embedded):
            index += 2 if following else 1
            continue
        match = _URL_LINE.fullmatch(line)
        if match:
            url, closing_emphasis = match.groups()
            if result and result[-1] and not result[-1].startswith(">") and "](" not in result[-1]:
                result[-1] = _link(result[-1] + closing_emphasis, url)
            else:
                try:
                    host = urlsplit(url).hostname or "Open link"
                except ValueError:
                    host = "Open link"
                result.append(_link(host, url) + closing_emphasis)
        else:
            result.append(line)
        index += 1
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
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(result)).strip()
    return cleaned
