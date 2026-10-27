"""ADR-0261: a capture's sections and search are pure functions of its bytes."""

from __future__ import annotations

import json
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st

from agent_core.domain.captures import (
    CAPTURE_SECTION_BYTES,
    CaptureSections,
    capture_text,
)
from agent_core.domain.messages import FileReferencePart, TextPart
from agent_core.domain.tool_output import content_bytes

ARTIFACT = UUID("9d02c2b4-1f6e-4c9a-8b1e-2f7d3a5c6e8f")
TOOL = "web.fetch"


def _escaped(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode()) - 2


def _sections(text: str, section_bytes: int = CAPTURE_SECTION_BYTES) -> CaptureSections:
    return CaptureSections(text, artifact_id=ARTIFACT, tool=TOOL, section_bytes=section_bytes)


def _check_partition(text: str, section_bytes: int) -> None:
    sections = _sections(text, section_bytes)
    bodies = [sections.body(n) for n in range(1, sections.count + 1)]
    assert "".join(bodies) == text, "sections must cover the text exactly once, in order"
    for number, body in enumerate(bodies, start=1):
        rendered = content_bytes([TextPart(text=sections.section_text(number))])
        assert len(rendered) <= section_bytes
        if number == sections.count:
            continue
        following = text[len("".join(bodies[:number])) :]
        if body.endswith("\n"):
            # The longest run of whole lines: the next whole line does not fit.
            next_line = following.split("\n", 1)[0] + ("\n" if "\n" in following else "")
            assert _escaped(body + next_line) > sections.body_bytes
        else:
            # Only a line longer than a section is cut, and as late as fits.
            assert _escaped(body + following[0]) > sections.body_bytes


def test_capture_text_joins_text_parts_and_skips_other_parts() -> None:
    raw = content_bytes(
        [
            TextPart(text="Provider: fake\n\nFirst"),
            FileReferencePart(artifact_id=ARTIFACT, media_type="image/png", filename="a.png"),
            TextPart(text="Second"),
        ]
    )
    assert capture_text(raw) == "Provider: fake\n\nFirst\n\nSecond"


def test_a_partial_capture_reads_as_utf8_with_invalid_bytes_replaced() -> None:
    raw = content_bytes([TextPart(text="雪" * 10)])[:-5]
    text = capture_text(raw)
    assert text.startswith('[{"kind":"text","text":"雪')
    assert text.endswith("\ufffd")


def test_sections_fit_the_section_ceiling_for_escape_heavy_text() -> None:
    text = ('\\"\n雪😀\x01\t' * 30_000) + "TAIL"
    _check_partition(text, CAPTURE_SECTION_BYTES)


def test_a_line_longer_than_a_section_is_cut_at_character_boundaries() -> None:
    text = "head\n" + "😀" * 20_000 + "\nafter\n"
    sections = _sections(text)
    # "head" is a whole line, so it ends section 1; the long line starts section 2.
    assert sections.count == 6
    assert sections.body(1) == "head\n"
    assert sections.body(2) == "😀" * len(sections.body(2))
    _check_partition(text, CAPTURE_SECTION_BYTES)


def test_the_header_names_capture_section_total_and_tool() -> None:
    sections = _sections("one\ntwo\n")
    assert sections.count == 1
    assert sections.section_text(1) == (
        f"artifact:{ARTIFACT} section 1 of 1, from web.fetch\n\none\ntwo\n"
    )


def test_an_empty_capture_is_one_empty_section() -> None:
    sections = _sections("")
    assert sections.count == 1
    assert sections.body(1) == ""


line = st.text(
    alphabet=st.sampled_from(["a", "Z", " ", '"', "\\", "\x07", "\t", "é", "雪", "😀", "\u2028"]),
    max_size=120,
)


@settings(max_examples=150, deadline=None)
@given(lines=st.lists(line, max_size=60), trailing=st.booleans())
def test_sections_partition_any_text(lines: list[str], trailing: bool) -> None:
    text = "\n".join(lines) + ("\n" if trailing and lines else "")
    _check_partition(text, 512)


def test_the_same_capture_always_sections_the_same_way() -> None:
    text = "".join(f"Line {n}: revenue grew {n}%\n" for n in range(5_000))
    first, second = _sections(text), _sections(text)
    assert first.count == second.count > 1
    assert [first.section_text(n) for n in range(1, first.count + 1)] == [
        second.section_text(n) for n in range(1, second.count + 1)
    ]


def test_find_is_a_case_folded_literal_search_with_sections_and_snippets() -> None:
    text = "".join(
        f"Row {n}: {'Total Revenue' if n % 50 == 0 else 'cost'} ({n})\n" for n in range(3_000)
    )
    sections = _sections(text)
    result = sections.find("total revenue", from_section=1)
    assert result.total == 60
    assert len(result.matches) == 20
    first = result.matches[0]
    assert first.section == 1
    assert "Total Revenue" in first.snippet
    assert len(first.snippet) <= 160
    assert "\n" not in first.snippet
    later = sections.find("TOTAL REVENUE", from_section=sections.count)
    assert later.total < result.total
    assert all(match.section == sections.count for match in later.matches)


def test_find_folds_characters_whose_case_folding_is_longer() -> None:
    sections = _sections("Die Straße ist lang.\nSTRASSE\n")
    result = sections.find("strasse", from_section=1)
    assert result.total == 2
    assert result.matches[0].snippet == "Die Straße ist lang. STRASSE"


def test_find_patterns_are_literal_text() -> None:
    sections = _sections("a.c abc a.c\n")
    assert sections.find("a.c", from_section=1).total == 2
    assert sections.find("[a]", from_section=1).total == 0


def test_find_results_fit_the_section_ceiling() -> None:
    text = ('"雪😀\\' * 40 + " needle ") * 400
    sections = _sections(text)
    rendered = content_bytes([TextPart(text=sections.find_text("needle", from_section=1))])
    assert len(rendered) <= CAPTURE_SECTION_BYTES
    assert sections.find("needle", from_section=1).total == 400
