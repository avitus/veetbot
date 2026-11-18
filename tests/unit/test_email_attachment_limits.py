"""Bounded replies carry ordinary large document sets without changing file bytes."""

import base64
import hashlib
from email import policy
from email.parser import BytesParser
from typing import Any

import pytest

from agent_core.domain.email_drafting import DraftAttachment, draft_transport_fits
from gmail_mcp import client as gmail_client
from gmail_mcp.client import GmailClient
from gmail_mcp.errors import GmailError
from tests.gates.test_email_m18 import _generated_gmail_discovery


async def test_platform_and_private_send_schema_accept_the_same_file_limit() -> None:
    discovery = await _generated_gmail_discovery("send")
    tool = next(tool for tool in discovery.tools if tool.name == "send_attached_message")
    properties = tool.input_schema["properties"]["attachments"]["items"]["properties"]
    assert properties["size_bytes"]["maximum"] == 25_000_000
    assert DraftAttachment.model_json_schema()["properties"]["size_bytes"]["maximum"] == 25_000_000


@pytest.mark.parametrize("sizes", [(14_000_000, 6_000_000), (25_000_000,)])
def test_large_document_set_fits_preparation_and_gmail_transport(sizes: tuple[int, ...]) -> None:
    assert draft_transport_fits("The requested documents are attached.", sizes)
    files = [bytes([index]) * size for index, size in enumerate(sizes)]
    raw = GmailClient._raw_message(
        to="recipient@example.test",
        subject="Requested documents",
        body="The requested documents are attached.",
        cc=None,
        bcc=None,
        attachments=[
            {
                "filename": f"document-{index}.bin",
                "media_type": "application/octet-stream",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "data_base64": base64.b64encode(content).decode(),
            }
            for index, content in enumerate(files)
        ],
    )
    mime = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    assert len(mime) <= 35 * 1024 * 1024
    parsed = BytesParser(policy=policy.default).parsebytes(mime)
    assert [part.get_payload(decode=True) for part in parsed.iter_attachments()] == files


@pytest.mark.parametrize("sizes", [(25_000_001,), (12_500_000, 12_500_001)])
def test_gmail_rejects_attachments_above_25_mb_before_dispatch(sizes: tuple[int, ...]) -> None:
    with pytest.raises(GmailError, match=r"gmail\.arguments_invalid"):
        GmailClient._raw_message(
            to="recipient@example.test",
            subject="Too large",
            body="Attached.",
            cc=None,
            bcc=None,
            attachments=[
                {
                    "filename": "large.bin",
                    "media_type": "application/octet-stream",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "data_base64": base64.b64encode(content).decode(),
                }
                for content in (b"x" * size for size in sizes)
            ],
        )


@pytest.mark.parametrize("encoding", ["mime", "base64"])
def test_gmail_enforces_actual_message_size_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    arguments: dict[str, Any] = {
        "to": "recipient@example.test",
        "subject": "Bounded message",
        "body": "A short reply.",
        "cc": None,
        "bcc": None,
    }
    raw = GmailClient._raw_message(**arguments)
    mime = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    name, size = (
        ("MESSAGE_MIME_MAXIMUM_BYTES", len(mime))
        if encoding == "mime"
        else ("MESSAGE_BASE64_MAXIMUM_BYTES", len(raw))
    )
    monkeypatch.setattr(gmail_client, name, size)
    assert GmailClient._raw_message(**arguments) == raw
    monkeypatch.setattr(gmail_client, name, size - 1)
    with pytest.raises(GmailError, match=r"gmail\.attachment_too_large"):
        GmailClient._raw_message(**arguments)
