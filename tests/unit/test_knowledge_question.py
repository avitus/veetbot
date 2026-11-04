"""A question names a passage, not a document: a visible document's name drops out (ADR-0293)."""

from __future__ import annotations

from agent_core.domain.knowledge import knowledge_question_without_document_names

TITLES = ["BMW Owner Manual.pdf", "Thermostat guide"]


def test_a_quoted_file_name_drops_out() -> None:
    asked = '"BMW Owner Manual.pdf" noises after switching off the engine'
    assert (
        knowledge_question_without_document_names(asked, TITLES)
        == "noises after switching off the engine"
    )


def test_the_title_without_its_extension_drops_out_in_any_case_or_possessive() -> None:
    asked = "bmw owner's manual parked vehicle noises"
    assert knowledge_question_without_document_names(asked, TITLES) == "parked vehicle noises"


def test_scattered_title_words_stay() -> None:
    asked = "2026 BMW X3 M50 owner manual noises after parking"
    assert knowledge_question_without_document_names(asked, TITLES) == asked


def test_a_question_that_is_only_a_document_name_stays_whole() -> None:
    assert knowledge_question_without_document_names("Thermostat guide", TITLES) == (
        "Thermostat guide"
    )


def test_no_titles_leave_the_question_as_asked() -> None:
    asked = "What are the valve-like sounds after I park?"
    assert knowledge_question_without_document_names(asked, []) == asked
