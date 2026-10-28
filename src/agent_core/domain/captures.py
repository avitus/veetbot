"""Sections of a tool-output capture, and a literal search over them (ADR-0261).

Admission's marker counts sections and `artifact.read_capture` returns them, both
through this module, so a section number names the same bytes wherever the
model meets it. Everything here is a pure function of the captured bytes: a
retried or replayed read returns identical text, never a summary.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from uuid import UUID

from agent_core.domain.messages import TextPart
from agent_core.domain.tool_output import content_bytes

CAPTURE_READER_TOOL_NAME = "artifact.read_capture"
# Admission marks a capture's artifact row with the producing tool and the
# scopes it required; only a marked capture opens for the reader.
CAPTURE_METADATA_KEY = "capture"
CAPTURE_SECTION_BYTES = 16_384
MAXIMUM_CAPTURE_SECTIONS = 65_536
FIND_MATCH_LIMIT = 20
FIND_SNIPPET_CHARACTERS = 160


def reader_call(artifact_id: UUID) -> str:
    """The exact ``tool.call`` a marker prints to read a capture's first section.

    Admission prints it and the truncation note looks for it, so one function
    keeps the two from drifting apart (ADR-0261).
    """

    return json.dumps(
        {
            "name": CAPTURE_READER_TOOL_NAME,
            "arguments": {"artifact": str(artifact_id), "section": 1},
        },
        separators=(",", ":"),
    )


def capture_text(raw: bytes) -> str:
    """The text of a capture's text parts, joined by a blank line.

    A partial capture was cut at the hard ceiling and is not valid JSON, so it
    reads as UTF-8 with invalid sequences replaced.
    """

    try:
        parts = json.loads(raw)
    except ValueError:
        parts = None
    if isinstance(parts, list):
        texts = [
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and part.get("kind") == "text"
            and isinstance(part.get("text"), str)
        ]
        text = "\n\n".join(texts)
    else:
        text = raw.decode("utf-8", errors="replace")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", errors="replace").decode("utf-8")
    return text


def _escaped_bytes(text: str) -> int:
    """What `text` adds to a rendered text part: JSON escaping is per character."""

    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def _header(artifact_id: UUID, section: int, total: int, tool: str) -> str:
    return f"artifact:{artifact_id} section {section} of {total}, from {tool}"


@dataclass(frozen=True, slots=True)
class CaptureMatch:
    section: int
    snippet: str


@dataclass(frozen=True, slots=True)
class CaptureFind:
    total: int
    matches: tuple[CaptureMatch, ...]


class CaptureSections:
    """A capture's text cut into sections whose rendered results fit `section_bytes`.

    Each section is the longest run of whole lines whose result, header
    included, fits; a line longer than a section is cut at the last character
    boundary that fits. The header is measured at its widest, so the cut never
    depends on the section's own number.
    """

    def __init__(
        self,
        text: str,
        *,
        artifact_id: UUID,
        tool: str,
        section_bytes: int = CAPTURE_SECTION_BYTES,
    ) -> None:
        self._text = text
        self._artifact_id = artifact_id
        self._tool = tool
        self._section_bytes = section_bytes
        widest = _header(UUID(int=0), MAXIMUM_CAPTURE_SECTIONS, MAXIMUM_CAPTURE_SECTIONS, tool)
        self.body_bytes = section_bytes - len(content_bytes([TextPart(text=widest + "\n\n")]))
        if self.body_bytes < 8:
            raise ValueError("a capture section cannot hold its header")
        self._starts = self._section_starts()
        if len(self._starts) > MAXIMUM_CAPTURE_SECTIONS:
            raise ValueError("a capture has more sections than a header can number")

    def _fitting_prefix(self, start: int, end: int) -> tuple[int, int]:
        """The longest prefix of text[start:end] that fits a section body, and its cost."""

        low, high = 1, min(end - start, self.body_bytes)
        best = (1, _escaped_bytes(self._text[start : start + 1]))
        while low <= high:
            middle = (low + high) // 2
            cost = _escaped_bytes(self._text[start : start + middle])
            if cost <= self.body_bytes:
                best = (middle, cost)
                low = middle + 1
            else:
                high = middle - 1
        return best

    def _section_starts(self) -> list[int]:
        text = self._text
        starts = [0]
        used = 0
        position = 0
        while position < len(text):
            newline = text.find("\n", position)
            end = len(text) if newline < 0 else newline + 1
            cost = _escaped_bytes(text[position:end])
            if used + cost <= self.body_bytes:
                used += cost
                position = end
                continue
            if used:
                starts.append(position)
            cut = position
            while cost > self.body_bytes:
                length, piece = self._fitting_prefix(cut, end)
                cut += length
                cost -= piece
                starts.append(cut)
            used = cost
            position = end
        return starts

    @property
    def count(self) -> int:
        return len(self._starts)

    def _bounds(self, section: int) -> tuple[int, int]:
        if not 1 <= section <= self.count:
            raise IndexError("capture section out of range")
        start = self._starts[section - 1]
        end = self._starts[section] if section < self.count else len(self._text)
        return start, end

    def body(self, section: int) -> str:
        start, end = self._bounds(section)
        return self._text[start:end]

    def section_text(self, section: int) -> str:
        header = _header(self._artifact_id, section, self.count, self._tool)
        return f"{header}\n\n{self.body(section)}"

    def find(self, needle: str, *, from_section: int) -> CaptureFind:
        """Case-folded literal matches from the start of `from_section`, in order."""

        begin, _ = self._bounds(from_section)
        target = needle.casefold()
        folded = self._text.casefold()
        origin: list[int] | None = None
        if len(folded) != len(self._text):
            # Folding only ever lengthens a character, so equal lengths map
            # one to one; otherwise keep where each folded character came from.
            pieces: list[str] = []
            origin = []
            for index, character in enumerate(self._text):
                piece = character.casefold()
                pieces.append(piece)
                origin.extend([index] * len(piece))
            folded = "".join(pieces)
        folded_begin = begin if origin is None else bisect.bisect_left(origin, begin)
        total = 0
        matches: list[CaptureMatch] = []
        position = folded.find(target, folded_begin)
        while position >= 0 and target:
            total += 1
            if len(matches) < FIND_MATCH_LIMIT:
                last = position + len(target) - 1
                start = position if origin is None else origin[position]
                end = (last if origin is None else origin[last]) + 1
                matches.append(
                    CaptureMatch(
                        section=bisect.bisect_right(self._starts, start),
                        snippet=self._snippet(start, end),
                    )
                )
            position = folded.find(target, position + len(target))
        return CaptureFind(total=total, matches=tuple(matches))

    def _snippet(self, start: int, end: int) -> str:
        width = FIND_SNIPPET_CHARACTERS
        left = max(0, start - max(0, width - (end - start)) // 2)
        right = min(len(self._text), left + width)
        left = max(0, right - width)
        return " ".join(self._text[left:right].split())

    def find_text(self, needle: str, *, from_section: int) -> str:
        """The rendered search result, dropping trailing matches until it fits."""

        found = self.find(needle, from_section=from_section)
        quoted = json.dumps(needle, ensure_ascii=False)
        lead = (
            f"artifact:{self._artifact_id}, {self.count} sections, from {self._tool}: "
            f"{found.total} matches for {quoted} from section {from_section}"
        )
        shown = list(found.matches)
        while True:
            lines = [
                lead + (f"; the first {len(shown)}:" if shown else "."),
                *(f"§{match.section}: {match.snippet}" for match in shown),
            ]
            text = "\n".join(lines)
            if len(content_bytes([TextPart(text=text)])) <= self._section_bytes or not shown:
                return text
            shown.pop()
