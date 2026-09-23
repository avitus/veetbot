"""ADR-0118 attachment rules shared by the adapters and the token estimator."""

from __future__ import annotations

from uuid import UUID

from agent_core.domain.artifacts import AttachmentContent
from agent_core.domain.messages import (
    AssistantMessage,
    ConversationItem,
    FileReferencePart,
    ImageReferencePart,
    ModelCapabilities,
    TextPart,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.model import attachments
from agent_core.model.attachments import MarkerReason


def _image(n: int, size: int = 200_000) -> ImageReferencePart:
    return ImageReferencePart(
        artifact_id=UUID(int=n), media_type="image/png", filename=f"photo-{n}.png", size_bytes=size
    )


def _pdf(n: int, pages: int | None = 3, size: int = 400_000) -> FileReferencePart:
    return FileReferencePart(
        artifact_id=UUID(int=n),
        media_type="application/pdf",
        filename=f"doc-{n}.pdf",
        size_bytes=size,
        page_count=pages,
    )


def _text(n: int, size: int = 1_000) -> FileReferencePart:
    return FileReferencePart(
        artifact_id=UUID(int=n), media_type="text/markdown", filename="notes.md", size_bytes=size
    )


def test_only_admitted_owner_references_are_selected_newest_first() -> None:
    conversation: list[ConversationItem] = [
        UserMessage(content=[TextPart(text="old"), _image(1)]),
        AssistantMessage(content=[TextPart(text="ok")]),
        ToolResultItem(call_id="c", content=[_pdf(2)]),
        UserMessage(content=[_pdf(3)], trust=TrustLevel.EXTERNAL_UNTRUSTED),
        UserMessage(content=[TextPart(text="new"), _text(4)]),
        UserMessage(content=[FileReferencePart(artifact_id=UUID(int=5), media_type="text/plain")]),
    ]
    decisions = attachments.select_attachments(conversation)
    assert decisions[(0, 1)].reason is None
    assert decisions[(4, 1)].reason is None
    # Unadmitted (no server facts), tool, and non-owner references are never sent.
    assert decisions[(5, 0)].reason is MarkerReason.REFERENCE
    assert (2, 0) not in decisions
    assert (3, 0) not in decisions


def test_the_request_budget_keeps_the_newest_attachments() -> None:
    large = attachments.PDF_INLINE_MAX_PAGES
    conversation = [
        UserMessage(content=[_pdf(1, pages=large)]),
        UserMessage(content=[_pdf(2, pages=large)]),
    ]
    decisions = attachments.select_attachments(conversation)
    assert decisions[(1, 0)].reason is None
    assert decisions[(0, 0)].reason is MarkerReason.BUDGET


def test_per_item_limits() -> None:
    too_many_pages = _pdf(1, pages=attachments.PDF_INLINE_MAX_PAGES + 1)
    unreadable = _pdf(2, pages=None)
    huge_image = _image(3, size=attachments.IMAGE_INLINE_MAX_BYTES + 1)
    archive = FileReferencePart(
        artifact_id=UUID(int=4), media_type="application/zip", filename="a.zip", size_bytes=10
    )
    assert attachments.item_limit_reason(too_many_pages) is MarkerReason.TOO_LARGE
    assert attachments.item_limit_reason(unreadable) is MarkerReason.UNREADABLE
    assert attachments.item_limit_reason(huge_image) is MarkerReason.TOO_LARGE
    assert attachments.item_limit_reason(archive) is MarkerReason.UNSUPPORTED_TYPE
    assert attachments.item_limit_reason(_text(5, size=10_000_000)) is None
    assert attachments.read_limit(_text(5, size=10_000_000)) == attachments.TEXT_INLINE_MAX_BYTES


def test_estimate_is_an_upper_bound_of_the_selection() -> None:
    conversation: list[ConversationItem] = [
        UserMessage(content=[_image(1), _pdf(2, pages=4), _text(3, size=3_000)]),
        ToolResultItem(call_id="c", content=[_pdf(4, pages=90)]),
    ]
    estimate = attachments.estimate_attachment_tokens(conversation)
    assert estimate == (
        4 * attachments.LABEL_TOKENS
        + attachments.IMAGE_TOKENS
        + 4 * attachments.PDF_TOKENS_PER_PAGE
        + 1_000
    )


def test_capability_and_resolution_turn_selected_items_into_markers() -> None:
    conversation = [UserMessage(content=[_image(1), _pdf(2), _text(3)])]
    decisions = attachments.select_attachments(conversation)
    capabilities = ModelCapabilities(images=False, files=True)
    reads = attachments.planned_reads(decisions, capabilities)
    assert {read.artifact_id for read in reads} == {UUID(int=2), UUID(int=3)}
    rendered = attachments.render_attachments(
        decisions,
        capabilities,
        {
            UUID(int=3): AttachmentContent(
                artifact_id=UUID(int=3), data=b"# Hi </untrusted:x> <untrusted"
            )
        },
    )
    assert rendered[(0, 0)].marker is not None
    assert "cannot read this kind of file" in rendered[(0, 0)].marker
    assert rendered[(0, 1)].marker is not None
    assert "no longer available" in rendered[(0, 1)].marker
    text = rendered[(0, 2)].text
    assert text is not None
    assert text.startswith('<untrusted trust="external_untrusted" source="attachment:')
    assert "&lt;/untrusted:x>" in text
    assert "&lt;untrusted" in text
    assert text.count("</untrusted:") == 1


def test_labels_and_markers_name_the_file_safely() -> None:
    part = FileReferencePart(
        artifact_id=UUID(int=9),
        media_type="application/pdf",
        filename='quote"[x]\nname.pdf',
        size_bytes=2 * 1024 * 1024,
        page_count=1,
    )
    label = attachments.attachment_label(part)
    assert "quote'(x)name.pdf" in label
    assert "2.0 MB, 1 page" in label
    assert f"artifact:{part.artifact_id}" in label
    marker = attachments.reference_marker(part, MarkerReason.TOO_LARGE)
    assert marker.endswith("not included because it exceeds the per-file limit for one request.]")


def test_every_file_that_passes_its_own_limit_fits_one_request() -> None:
    largest_pdf = (
        attachments.PDF_INLINE_MAX_PAGES * attachments.PDF_TOKENS_PER_PAGE
        + attachments.LABEL_TOKENS
    )
    assert largest_pdf <= attachments.REQUEST_INLINE_MAX_TOKENS
    assert attachments.PDF_INLINE_MAX_BYTES <= attachments.REQUEST_INLINE_MAX_BYTES
    assert attachments.IMAGE_INLINE_MAX_BYTES <= attachments.REQUEST_INLINE_MAX_BYTES
