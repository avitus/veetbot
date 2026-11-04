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


def test_a_curly_apostrophe_possessive_is_the_same_word() -> None:
    asked = "BMW Owner\u2019s Manual parked vehicle noises"
    assert knowledge_question_without_document_names(asked, TITLES) == "parked vehicle noises"


def test_an_uppercase_possessive_is_the_same_word() -> None:
    asked = "BMW OWNER\u2019S MANUAL parked vehicle noises"
    assert knowledge_question_without_document_names(asked, TITLES) == "parked vehicle noises"


def test_an_apostrophe_inside_a_name_is_not_a_possessive() -> None:
    titles = ["O\u2019Shea Guide.pdf"]
    asked = "o\u2019shea guide installation steps"
    assert knowledge_question_without_document_names(asked, titles) == "installation steps"


def test_a_plural_is_not_a_title_word() -> None:
    asked = "How do glasses break?"
    assert knowledge_question_without_document_names(asked, ["Glass"]) == asked


def test_a_title_word_keeps_its_accented_letters() -> None:
    asked = "Caf\u00e9's opening hours"
    assert knowledge_question_without_document_names(asked, ["Caf\u00e9.pdf"]) == "opening hours"


def test_a_name_is_matched_as_written_so_punctuation_counts() -> None:
    asked = "C# memory management"
    assert knowledge_question_without_document_names(asked, ["C++.pdf"]) == asked
    assert knowledge_question_without_document_names(asked, ["C.pdf"]) == asked
    assert knowledge_question_without_document_names("C++.pdf memory model", ["C++.pdf"]) == (
        "memory model"
    )
    assert knowledge_question_without_document_names("C++ memory model", ["C++.pdf"]) == (
        "memory model"
    )


def test_sentence_punctuation_after_a_name_is_kept() -> None:
    asked = "Tell me about C. Thanks"
    assert knowledge_question_without_document_names(asked, ["C.pdf"]) == "Tell me about . Thanks"


def test_case_folds_beyond_ascii() -> None:
    asked = "STRASSE speed limits"
    assert knowledge_question_without_document_names(asked, ["Stra\u00dfe.pdf"]) == "speed limits"


def test_a_decomposed_title_matches_a_composed_question() -> None:
    asked = "Caf\u00e9 opening hours"
    assert knowledge_question_without_document_names(asked, ["Cafe\u0301.pdf"]) == "opening hours"
    assert knowledge_question_without_document_names("Cafe\u0301 opening hours", ["Caf\u00e9"]) == (
        "opening hours"
    )
