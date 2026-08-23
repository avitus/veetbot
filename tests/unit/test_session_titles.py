"""Conversation-title normalization and legacy event compatibility."""

from agent_core.adapters.persistence.repositories import PostgresSessionRepository
from agent_core.domain.sessions import conversation_title, project_scope


def test_conversation_title_collapses_whitespace_and_caps_length() -> None:
    title = conversation_title("  Restore\n\tthis   conversation " + "x" * 100)

    assert title is not None
    assert title.startswith("Restore this conversation ")
    assert len(title) == 64
    assert conversation_title(" \n\t ") is None


def test_project_scope_normalizes_padded_metadata() -> None:
    assert project_scope({"project_scope": "  project-a \n"}) == "project-a"
    assert project_scope({"project_scope": " \n\t "}) == "general"


def test_legacy_title_reads_the_first_nonempty_text_part() -> None:
    title = PostgresSessionRepository._title_from_event_payload(
        {
            "content": [
                {"kind": "image", "artifact_id": "ignored"},
                {"kind": "text", "text": "   "},
                {"kind": "text", "text": "  Existing\nconversation  "},
            ]
        }
    )

    assert title == "Existing conversation"
    assert (
        PostgresSessionRepository._title_from_event_payload(
            {"content": "  Terminal   conversation  "}
        )
        == "Terminal conversation"
    )
