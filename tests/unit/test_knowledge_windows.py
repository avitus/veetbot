"""Chunk embedding windows: a chunk longer than the encoder admits is embedded in pieces.

knowledge-documents.md ("Indexing") embeds a chunk as its heading path over
its text. The local encoder refuses an input over its token capacity rather
than truncating it, and a chunk targets more tokens than that capacity, so a
chunk becomes one or more windows: the heading path over a run of the text,
each under the budget, every character of the text in exactly one window.
Without a budget the one window is the text that was embedded before.
"""

from __future__ import annotations

import pytest

from agent_core.domain.knowledge import (
    KNOWLEDGE_EMBEDDING_TEXT_BYTES,
    KNOWLEDGE_EMBEDDING_TEXT_VERSION,
    knowledge_embedding_text,
    knowledge_embedding_windows,
)

pytestmark = pytest.mark.static

PARAGRAPHS = (
    "The cooling fan may run for several minutes after the engine is shut off. "
    "This is normal and keeps the exhaust particulate filter from overheating.\n\n"
    "A ticking sound from under the car after parking is the exhaust system "
    "contracting as it cools. It needs no attention.\n\n"
    "If a noise persists for more than an hour, have the vehicle checked."
)


def words(text: str) -> int:
    """A word count stands in for the encoder's tokenizer."""

    return len(text.split())


def content(text: str) -> str:
    return "".join(text.split())


def test_without_a_budget_the_one_window_is_the_text_embedded_before() -> None:
    windows = knowledge_embedding_windows(["Guide", "Fans"], PARAGRAPHS, budget_tokens=None)
    assert windows == [knowledge_embedding_text(["Guide", "Fans"], PARAGRAPHS)]
    assert KNOWLEDGE_EMBEDDING_TEXT_VERSION == "knowledge-text@2"


def test_a_chunk_under_the_budget_is_one_window() -> None:
    windows = knowledge_embedding_windows(["Guide"], "Short.", budget_tokens=10, count_tokens=words)
    assert windows == ["Guide\nShort."]


def test_every_window_fits_the_budget_and_every_character_lands_once() -> None:
    windows = knowledge_embedding_windows(
        ["Guide", "Driving tips"], PARAGRAPHS, budget_tokens=24, count_tokens=words
    )
    assert len(windows) > 1
    assert all(window.startswith("Guide > Driving tips\n") for window in windows)
    assert all(words(window) <= 24 for window in windows)
    bodies = [window.split("\n", 1)[1] for window in windows]
    assert all(body.strip() == body and body for body in bodies)
    assert content("".join(bodies)) == content(PARAGRAPHS)


def test_windows_prefer_paragraph_then_sentence_boundaries() -> None:
    windows = knowledge_embedding_windows([], PARAGRAPHS, budget_tokens=40, count_tokens=words)
    # Forty words hold the first paragraph (26 words) but not the second with it.
    assert windows[0] == PARAGRAPHS.split("\n\n")[0]
    tight = knowledge_embedding_windows([], PARAGRAPHS, budget_tokens=16, count_tokens=words)
    # Sixteen words hold one sentence at a time, never a paragraph.
    assert tight[0] == "The cooling fan may run for several minutes after the engine is shut off."
    assert all(words(window) <= 16 for window in tight)


def test_a_single_run_over_the_budget_splits_on_whitespace() -> None:
    text = " ".join(f"word{index}" for index in range(30))
    windows = knowledge_embedding_windows(["H"], text, budget_tokens=8, count_tokens=words)
    assert all(words(window) <= 8 for window in windows)
    assert content("".join(window.split("\n", 1)[1] for window in windows)) == content(text)
    assert windows == knowledge_embedding_windows(["H"], text, budget_tokens=8, count_tokens=words)


def test_the_estimate_serves_when_no_tokenizer_is_given() -> None:
    windows = knowledge_embedding_windows([], PARAGRAPHS, budget_tokens=40)
    assert len(windows) > 1
    assert content("".join(windows)) == content(PARAGRAPHS)


def test_each_window_stays_within_the_byte_bound() -> None:
    windows = knowledge_embedding_windows(
        ["Guide"], "é" * (3 * KNOWLEDGE_EMBEDDING_TEXT_BYTES), budget_tokens=100_000
    )
    assert all(len(window.encode("utf-8")) <= KNOWLEDGE_EMBEDDING_TEXT_BYTES for window in windows)
    assert "�" not in "".join(windows)


def test_a_heading_longer_than_the_budget_yields_to_the_text() -> None:
    """A heading path has no length limit; the windows still fit the budget.

    The prefix drops its outer levels, deepest heading last, until a window
    has room for text; a prefix no level of which fits is left out. Every
    character of the text still lands once.
    """

    path = ["Manual", " ".join(f"word{i}" for i in range(60)), "Fan noise"]
    windows = knowledge_embedding_windows(path, PARAGRAPHS, budget_tokens=24, count_tokens=words)
    assert windows and all(words(window) <= 24 for window in windows)
    assert all(window.startswith("Fan noise\n") for window in windows)
    assert "".join(content(window.split("\n", 1)[1]) for window in windows) == content(PARAGRAPHS)
    hopeless = [" ".join(f"word{i}" for i in range(60))]
    bare = knowledge_embedding_windows(hopeless, PARAGRAPHS, budget_tokens=24, count_tokens=words)
    assert bare and all(words(window) <= 24 for window in bare)
    assert "".join(content(window) for window in bare) == content(PARAGRAPHS)


def test_a_heading_and_body_over_the_byte_bound_are_still_windowed_whole() -> None:
    """The byte bound truncates one text; windows never lose a character to it.

    A heading near the bound is left out and the body is cut into windows
    that each fit the bound; a heading that fits is kept and the body is cut
    so the prefixed window still fits.
    """

    huge_heading = ["x" * 3000]
    body = "yy " * 700
    windows = knowledge_embedding_windows(huge_heading, body, budget_tokens=300, count_tokens=words)
    assert all(len(window.encode("utf-8")) <= KNOWLEDGE_EMBEDDING_TEXT_BYTES for window in windows)
    assert all(words(window) <= 300 for window in windows)
    assert "".join(content(window) for window in windows) == content(body)
    heading = ["h" * 1000]
    long_body = "word " * 1000
    kept = knowledge_embedding_windows(heading, long_body, budget_tokens=10_000, count_tokens=words)
    assert len(kept) > 1
    assert all(window.startswith("h" * 1000 + "\n") for window in kept)
    assert all(len(window.encode("utf-8")) <= KNOWLEDGE_EMBEDDING_TEXT_BYTES for window in kept)
    assert "".join(content(window.split("\n", 1)[1]) for window in kept) == content(long_body)


def test_a_chunk_under_the_whole_bound_stays_one_window() -> None:
    """Windows rescue long mixed chunks; a short topical chunk keeps its whole embedding.

    Cutting a short section sharpens its distracting sentences into fragments
    (ADR-0290 measured the thermostat guide's schedule section overtaking its
    answer once cut), so a chunk under `whole_tokens` is one window even when
    it is over the budget; a chunk at or over it is cut under the budget.
    """

    short = " ".join(f"word{i}" for i in range(40))  # 40 words, over a 24-word budget
    whole = knowledge_embedding_windows(
        ["Guide"], short, budget_tokens=24, count_tokens=words, whole_tokens=48
    )
    assert whole == [knowledge_embedding_text(["Guide"], short)]
    long = " ".join(f"word{i}" for i in range(60))  # at and over the whole bound
    cut = knowledge_embedding_windows(
        ["Guide"], long, budget_tokens=24, count_tokens=words, whole_tokens=48
    )
    assert len(cut) > 1 and all(words(window) <= 24 for window in cut)
    # Without a whole bound the budget alone decides, as the local encoder's capacity must.
    budget_only = knowledge_embedding_windows(
        ["Guide"], short, budget_tokens=24, count_tokens=words
    )
    assert len(budget_only) > 1
