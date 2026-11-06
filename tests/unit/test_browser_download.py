"""A downloaded file is bounded text, saved in this chat and read as a capture (ADR-0294)."""

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from playwright.async_api import Download
from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.browser.hosted_sessions import (
    MAXIMUM_SESSION_RESPONSE_BYTES,
    BrowserSessionObservation,
)
from agent_core.adapters.browser.playwright import (
    DOWNLOAD_ACT_BUDGET_SECONDS,
    PythonPlaywrightRuntime,
    _close_download_window,
    _DownloadWindow,
)
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.browser_control_plane.api import _snapshot_response
from agent_core.domain.artifacts import ArtifactOrigin, StoredArtifactRef
from agent_core.domain.browser import BrowserObservation, BrowserSnapshot
from agent_core.domain.browser_download import (
    MAX_BROWSER_DOWNLOAD_BYTES,
    BrowserDownload,
)
from agent_core.domain.browser_projection import browser_context_projection
from agent_core.domain.captures import (
    CAPTURE_METADATA_KEY,
    CAPTURE_READER_TOOL_NAME,
    capture_text,
    reader_call,
)
from agent_core.domain.errors import ConflictError
from agent_core.domain.messages import FileReferencePart, TextPart
from agent_core.domain.policies import TrustLevel
from agent_core.domain.tools import ToolExecutionContext
from agent_core.tools.artifact_read_capture import ArtifactReadCaptureTool
from agent_core.tools.browser_act import BrowserActTool, PinnedBrowserActTool
from agent_core.tools.browser_results import OUTPUT_SCHEMA
from agent_core.tools.tool_call import TOOL_CALL_TOOL_NAME
from agent_core.tools.validation import validate_output
from tests.contract.support import RUN_ID, SESSION_ID, TENANT, principal, run, tool_context
from tests.unit.test_browser_tools import FakeBrowserProvider
from tests.unit.test_history_cache_window import _factory

CSV = "Card,Description,Amount\n1,STREAMING CO,-15.49\n"
ARGUMENTS = {"kind": "click", "expected_revision": "revision-1", "ref": "element-1"}


class _Writer:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create(
        self,
        stream: AsyncIterator[bytes],
        filename: str,
        media_type: str,
        trust: TrustLevel,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> StoredArtifactRef:
        data = b"".join([chunk async for chunk in stream])
        self.created.append(
            {
                "data": data,
                "filename": filename,
                "media_type": media_type,
                "trust": trust,
                "metadata": metadata,
            }
        )
        return StoredArtifactRef(
            artifact_id=uuid4(), sha256="0" * 64, size_bytes=len(data), media_type=media_type
        )


class _DownloadingProvider(FakeBrowserProvider):
    def __init__(self, download: BrowserDownload) -> None:
        super().__init__()
        self.download = download

    async def act(self, action: Any, *, constraint: Any = None) -> BrowserObservation:
        del action, constraint
        return self._observation("https://example.org/activity").model_copy(
            update={"download": self.download}
        )


def _context(writer: object, *, reader: bool = True) -> ToolExecutionContext:
    tools = {"browser.act"} | ({CAPTURE_READER_TOOL_NAME, TOOL_CALL_TOOL_NAME} if reader else set())
    return replace(tool_context(), artifacts=writer, available_tools=frozenset(tools))


def test_a_utf8_file_keeps_its_text_and_a_platform_clean_name() -> None:
    download = BrowserDownload.from_bytes("../../odd name;.CSV", ("﻿" + CSV).encode())
    assert download.status == "saved"
    assert download.filename == "odd_name_.CSV"
    assert download.media_type == "text/csv"
    assert download.text() == CSV


def test_a_windows_encoded_export_becomes_utf8() -> None:
    download = BrowserDownload.from_bytes("a.csv", "CAFÉ,-1\n".encode("cp1252"))
    assert download.text() == "CAFÉ,-1\n"


@pytest.mark.parametrize(
    ("name", "media_type"),
    [
        ("activity.tsv", "text/tab-separated-values"),
        ("activity.qfx", "text/plain"),
        ("", "text/plain"),
    ],
)
def test_the_media_type_comes_from_a_short_list(name: str, media_type: str) -> None:
    assert BrowserDownload.from_bytes(name, b"a\n").media_type == media_type


@pytest.mark.parametrize(
    "data", [b"%PDF-1.7\x00binary", b"\x81\x8d\x8f\x90", "a,b\n".encode("utf-16")]
)
def test_a_file_that_is_not_text_is_refused(data: bytes) -> None:
    download = BrowserDownload.from_bytes("x.csv", data)
    assert (download.status, download.reason) == ("refused", "unsupported_type")
    assert download.data_base64 is None


@pytest.mark.parametrize("data", [b"%PDF-1.4\n1 0 obj\n", b"\n\n%PDF-1.7\nascii only\n"])
def test_a_pdf_of_plain_characters_is_still_refused(data: bytes) -> None:
    download = BrowserDownload.from_bytes("report.pdf", data)
    assert (download.status, download.reason) == ("refused", "unsupported_type")


def test_the_worker_refuses_a_pdf_the_service_called_text() -> None:
    with pytest.raises(ValidationError):
        BrowserDownload.model_validate(
            {
                "status": "saved",
                "filename": "report.txt",
                "media_type": "text/plain",
                "data_base64": "JVBERi0xLjQK",
            }
        )


def test_an_empty_file_is_refused_because_there_is_nothing_to_read() -> None:
    assert BrowserDownload.from_bytes("a.csv", b"") == BrowserDownload.refused("failed")


def test_a_file_over_the_limit_is_refused() -> None:
    download = BrowserDownload.from_bytes("x.csv", b"a" * (MAX_BROWSER_DOWNLOAD_BYTES + 1))
    assert (download.status, download.reason) == ("refused", "too_large")


def test_a_refusal_carries_no_bytes_and_a_save_carries_no_reason() -> None:
    with pytest.raises(ValidationError):
        BrowserDownload(status="refused", reason="failed", data_base64="YQ==")
    with pytest.raises(ValidationError):
        BrowserDownload(status="saved", reason="failed", filename="a", media_type="text/plain")


@pytest.mark.parametrize(
    "fields",
    [
        {"filename": "../secret.csv", "media_type": "text/csv", "data_base64": "YQ=="},
        {"filename": "a.csv", "media_type": "text/html", "data_base64": "YQ=="},
        {"filename": "a.csv", "media_type": "text/csv", "data_base64": "not base64!"},
        {"filename": "a.csv", "media_type": "text/csv", "data_base64": ""},
        {"filename": "a.csv", "media_type": "text/csv", "data_base64": "/w=="},
        {"filename": "a.csv", "media_type": "text/csv", "data_base64": "YQA="},
    ],
)
def test_the_worker_rechecks_a_saved_file_from_the_service(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        BrowserDownload.model_validate({"status": "saved", **fields})


def test_bytes_never_appear_in_a_repr() -> None:
    assert "STREAMING" not in repr(BrowserDownload.from_bytes("a.csv", CSV.encode()))


def test_the_largest_file_fits_the_hosted_response_after_a_full_observation() -> None:
    download = BrowserDownload.from_bytes("a.csv", b"a" * MAX_BROWSER_DOWNLOAD_BYTES)
    assert download.status == "saved"
    assert len(download.model_dump_json()) + 720 * 1024 <= MAXIMUM_SESSION_RESPONSE_BYTES


def test_the_file_crosses_the_hosted_boundary_with_the_observation() -> None:
    observation = BrowserObservation(
        url="https://example.org/a",
        revision="r",
        download=BrowserDownload.from_bytes("a.csv", CSV.encode()),
    )
    wire = _snapshot_response(BrowserSnapshot(observation=observation))
    page = BrowserSessionObservation.model_validate(wire)
    assert page.snapshot().observation.download == observation.download


class _UnfinishedDownload:
    """A transfer that ends only when cancelled; delete waits for it, as Playwright's does."""

    suggested_filename = "a.csv"

    def __init__(self, url: str = "https://site.test/file") -> None:
        self.url = url
        self.cancelled = asyncio.Event()
        self.deleted = False

    async def path(self) -> Path:
        await self.cancelled.wait()
        raise PlaywrightError("download canceled")

    async def cancel(self) -> None:
        self.cancelled.set()

    async def delete(self) -> None:
        await self.cancelled.wait()
        self.deleted = True


async def test_an_interrupted_read_stops_the_transfer_instead_of_waiting_for_it() -> None:
    download = _UnfinishedDownload()
    reading = asyncio.create_task(
        PythonPlaywrightRuntime()._read_download(cast(Download, download))
    )
    await asyncio.sleep(0)
    reading.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(reading, 2)

    assert download.cancelled.is_set() and download.deleted


@pytest.mark.parametrize(
    "url",
    [
        "https://elsewhere.test/file.csv",
        "blob:https://elsewhere.test/1b2c",
        "http://site.test/file.csv",
        "data:text/csv,a%2Cb",
        "blob:null/1b2c",
        "about:blank",
    ],
)
async def test_a_file_from_outside_the_login_is_refused_and_leaves_the_slot_free(url: str) -> None:
    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://site.test",)
    window = _DownloadWindow(approved=True)
    runtime._download_window = window
    outside = _UnfinishedDownload(url)

    await runtime._handle_download(cast(Download, outside))

    assert outside.cancelled.is_set() and outside.deleted
    assert window.reading is None and not window.started.is_set()
    assert window.refusal == "origin_not_allowed"

    inside = _UnfinishedDownload("blob:https://site.test/1b2c")
    await runtime._handle_download(cast(Download, inside))
    assert window.reading is not None and not inside.cancelled.is_set()
    await _close_download_window(window)


async def test_a_slow_click_leaves_no_time_to_wait_for_a_file_that_never_starts() -> None:
    """The click ran, so the file wait must not spend the tool's time on an unknown outcome."""
    window = _DownloadWindow(approved=True, deadline=asyncio.get_running_loop().time())

    finished = await asyncio.wait_for(
        PythonPlaywrightRuntime()._finish_download(window, names_a_file=True), 2
    )

    assert finished is None


async def test_a_file_still_arriving_when_the_act_runs_out_of_time_is_timed_out() -> None:
    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://site.test",)
    window = _DownloadWindow(approved=True, deadline=asyncio.get_running_loop().time() + 0.2)
    runtime._download_window = window
    download = _UnfinishedDownload()

    await runtime._handle_download(cast(Download, download))
    finished = await asyncio.wait_for(runtime._finish_download(window, names_a_file=True), 2)

    assert finished == BrowserDownload.refused("timed_out")
    assert download.cancelled.is_set() and download.deleted


async def test_a_file_that_starts_once_the_act_is_out_of_time_is_not_admitted() -> None:
    """A key press can run for 30 s, past the 20 s the act keeps for a file."""
    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://site.test",)
    window = _DownloadWindow(approved=True, deadline=asyncio.get_running_loop().time())
    runtime._download_window = window
    download = _UnfinishedDownload()

    await runtime._handle_download(cast(Download, download))

    assert window.reading is None
    assert download.cancelled.is_set() and download.deleted
    finished = await asyncio.wait_for(runtime._finish_download(window, names_a_file=True), 2)
    assert finished == BrowserDownload.refused("timed_out")


async def test_a_file_during_an_approved_click_is_admitted_whatever_started_it() -> None:
    """The window is temporal: ADR-0294's Limits record why it cannot name a file's cause."""

    runtime = PythonPlaywrightRuntime()
    runtime._allowed_origins = ("https://site.test",)
    window = _DownloadWindow(approved=True)
    runtime._download_window = window
    download = _UnfinishedDownload()

    await runtime._handle_download(cast(Download, download))

    assert window.reading is not None and not download.cancelled.is_set()
    # The act ends before the read takes its first step, the narrowest early end.
    await _close_download_window(window)
    assert download.cancelled.is_set() and download.deleted


async def test_a_cancelled_file_leaves_nothing_to_count_against_the_next() -> None:
    download = _UnfinishedDownload()

    await PythonPlaywrightRuntime()._handle_download(cast(Download, download))

    assert download.cancelled.is_set() and download.deleted


async def test_an_approved_download_is_saved_as_a_capture_the_reader_opens() -> None:
    writer = _Writer()
    provider = _DownloadingProvider(BrowserDownload.from_bytes("Activity.CSV", CSV.encode()))

    result = await BrowserActTool(provider).execute(ARGUMENTS, _context(writer))

    assert result.ok and result.structured is not None
    validate_output(result.structured, BrowserActTool.spec.output_schema)
    saved = result.structured["saved_file"]
    created = writer.created[0]
    assert created["trust"] is TrustLevel.EXTERNAL_UNTRUSTED
    assert created["filename"] == "Activity.CSV"
    assert created["media_type"] == "text/csv"
    assert created["metadata"] == {
        CAPTURE_METADATA_KEY: {"tool": "browser.act", "required_scopes": [], "literal": True}
    }
    assert capture_text(created["data"]).strip() == CSV.strip()
    artifact = UUID(saved["artifact"])
    assert saved == {
        "status": "saved",
        "artifact": str(artifact),
        "filename": "Activity.CSV",
        "media_type": "text/csv",
        "size_bytes": len(CSV),
        "sections": 1,
        "read_with": f"tool.call {reader_call(artifact)}",
    }
    assert isinstance(result.content[0], TextPart)
    assert "read_with" in result.content[0].text
    assert CSV not in result.content[0].text


async def test_the_model_learns_why_a_file_was_refused() -> None:
    writer = _Writer()
    provider = _DownloadingProvider(BrowserDownload.refused("needs_approval"))

    result = await BrowserActTool(provider).execute(ARGUMENTS, _context(writer))

    assert result.ok and result.structured is not None
    validate_output(result.structured, BrowserActTool.spec.output_schema)
    assert result.structured["saved_file"] == {"status": "refused", "reason": "needs_approval"}
    assert writer.created == []


class _FailingWriter:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def create(self, *args: object, **kwargs: object) -> StoredArtifactRef:
        del args, kwargs
        raise self.error


@pytest.mark.parametrize(
    "error", [ConflictError("artifact row refused"), RuntimeError("database went away")]
)
async def test_a_file_that_cannot_be_stored_never_costs_the_click_its_result(
    error: Exception,
) -> None:
    provider = _DownloadingProvider(BrowserDownload.from_bytes("a.csv", CSV.encode()))

    result = await BrowserActTool(provider).execute(ARGUMENTS, _context(_FailingWriter(error)))

    assert result.ok and result.structured is not None
    validate_output(result.structured, BrowserActTool.spec.output_schema)
    assert result.structured["saved_file"] == {"status": "refused", "reason": "failed"}


class _HangingWriter:
    async def create(self, *args: object, **kwargs: object) -> StoredArtifactRef:
        del args, kwargs
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_a_store_that_hangs_gives_up_while_the_tool_can_still_report_the_click() -> None:
    provider = _DownloadingProvider(BrowserDownload.from_bytes("a.csv", CSV.encode()))
    context = replace(_context(_HangingWriter()), timeout_seconds=1.2)

    result = await asyncio.wait_for(BrowserActTool(provider).execute(ARGUMENTS, context), 1.0)

    assert result.ok and result.structured is not None
    assert result.structured["saved_file"] == {"status": "refused", "reason": "failed"}


async def test_storing_a_file_gives_up_by_the_call_deadline_not_a_clock_of_its_own() -> None:
    """The call's clock starts before the tool does, so the tool cannot restart it."""
    provider = _DownloadingProvider(BrowserDownload.from_bytes("a.csv", CSV.encode()))
    context = replace(
        _context(_HangingWriter()),
        timeout_seconds=30.0,
        monotonic_deadline=asyncio.get_running_loop().time() + 1.2,
    )

    result = await asyncio.wait_for(BrowserActTool(provider).execute(ARGUMENTS, context), 1.0)

    assert result.ok and result.structured is not None
    assert result.structured["saved_file"] == {"status": "refused", "reason": "failed"}


def test_the_file_wait_leaves_the_tool_time_for_its_slowest_later_steps() -> None:
    """After the wait come the observation, a postcondition of up to 5 s, and the store."""
    assert BrowserActTool.spec.timeout_seconds >= DOWNLOAD_ACT_BUDGET_SECONDS + 5 + 5


async def test_cancelling_the_run_while_a_file_is_stored_still_cancels_it() -> None:
    provider = _DownloadingProvider(BrowserDownload.from_bytes("a.csv", CSV.encode()))

    with pytest.raises(asyncio.CancelledError):
        await BrowserActTool(provider).execute(
            ARGUMENTS, _context(_FailingWriter(asyncio.CancelledError()))
        )


async def test_a_chat_that_cannot_read_a_capture_saves_nothing() -> None:
    writer = _Writer()
    provider = _DownloadingProvider(BrowserDownload.from_bytes("a.csv", CSV.encode()))

    result = await BrowserActTool(provider).execute(ARGUMENTS, _context(writer, reader=False))

    assert result.ok and result.structured is not None
    assert result.structured["saved_file"] == {"status": "refused", "reason": "reader_unavailable"}
    assert writer.created == []


async def test_a_postcondition_read_does_not_lose_the_file() -> None:
    writer = _Writer()
    provider = _DownloadingProvider(BrowserDownload.from_bytes("a.csv", CSV.encode()))

    result = await BrowserActTool(provider).execute(
        {
            **ARGUMENTS,
            "postcondition": {"evidence": {"kind": "text", "text": "x"}, "timeout_ms": 100},
        },
        _context(writer),
    )

    assert result.structured is not None
    assert result.structured["saved_file"]["status"] == "saved"


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        '[{"kind": "text", "text": "only this"}, {"date": "01/02", "amount": -15.49}]',
        "42",
        CSV,
    ],
)
async def test_the_saved_file_opens_in_the_capture_reader(tmp_path: Path, text: str) -> None:
    """Writer, row and reader together: a file reads back as its own text, whatever it holds."""

    clock, factory = await _factory()
    async with factory() as uow:
        await uow.runs.create(run())
    store = FilesystemArtifactStore(tmp_path)
    context = _context(
        ArtifactWriterFactory(factory, store, clock, SequenceIdFactory()).for_run(
            tenant_id=TENANT,
            principal_id=principal().principal_id,
            session_id=SESSION_ID,
            run_id=RUN_ID,
            origin=ArtifactOrigin.TOOL_OUTPUT,
        )
    )
    provider = _DownloadingProvider(BrowserDownload.from_bytes("Activity.CSV", text.encode()))

    result = await BrowserActTool(provider).execute(ARGUMENTS, context)
    assert result.structured is not None
    saved = result.structured["saved_file"]
    read = await ArtifactReadCaptureTool(factory, store, clock).execute(
        {"artifact": saved["artifact"], "section": 1}, context
    )
    found = await ArtifactReadCaptureTool(factory, store, clock).execute(
        {"artifact": saved["artifact"], "find": text[:2]}, context
    )

    assert read.ok and read.structured is not None
    assert read.structured["text"].strip() == text.strip()
    assert saved["sections"] == read.structured["sections"] == 1
    assert read.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert found.ok and found.structured is not None and found.structured["total"] >= 1


def test_the_context_projection_keeps_the_saved_file_note() -> None:
    saved = {"status": "saved", "read_with": "tool.call {}"}
    projected = browser_context_projection(
        {
            "provider": "fake",
            "url": "https://example.org/a",
            "title": None,
            "revision": "r",
            "text": "page " * 4000,
            "elements": [],
            "saved_file": saved,
        },
        budget=4096,
        reference=FileReferencePart(
            artifact_id=UUID(int=1), media_type="application/json", filename="o.json"
        ),
    )
    assert projected is not None
    assert isinstance(projected[0], TextPart)
    assert '"saved_file":{"status":"saved"' in projected[0].text


def test_a_budget_too_small_for_the_page_still_keeps_the_saved_file_note() -> None:
    """The note is the only route to the file, so the omission fallback keeps it."""

    saved = {"status": "saved", "read_with": "tool.call {}"}
    projected = browser_context_projection(
        {
            "provider": "fake",
            "url": "https://example.org/a",
            "title": None,
            "revision": "r" * 128,
            "text": "",
            "elements": [],
            "saved_file": saved,
        },
        budget=512,
        reference=FileReferencePart(
            artifact_id=UUID(int=1), media_type="application/json", filename="o.json"
        ),
    )
    assert projected is not None
    assert isinstance(projected[0], TextPart)
    assert '"observation_omitted":true' in projected[0].text
    assert '"saved_file":{"status":"saved"' in projected[0].text


@pytest.mark.parametrize("budget", [800, 900])
def test_a_tight_budget_keeps_a_timed_out_exports_guidance(budget: int) -> None:
    """ADR-0300 decision 3: the guidance outranks the page, which a later read restores."""

    timed_out = {"status": "refused", "reason": "timed_out"}
    projected = browser_context_projection(
        {
            "provider": "fake",
            "url": "https://example.org/a",
            "title": None,
            "revision": "r" * 128,
            "text": "",
            "elements": [],
            "saved_file": timed_out,
        },
        budget=budget,
        reference=FileReferencePart(
            artifact_id=UUID(int=1), media_type="application/json", filename="o.json"
        ),
    )
    assert projected is not None
    assert isinstance(projected[0], TextPart)
    assert '"reason":"timed_out"' in projected[0].text
    assert "Do not repeat the same export blindly." in projected[0].text


def test_without_room_for_the_guidance_the_page_stays() -> None:
    """The guidance is dropped before the page, never the saved-file note."""

    projected = browser_context_projection(
        {
            "provider": "fake",
            "url": "https://example.org/a",
            "title": None,
            "revision": "r" * 128,
            "text": "",
            "elements": [],
            "saved_file": {"status": "refused", "reason": "timed_out"},
        },
        budget=700,
        reference=FileReferencePart(
            artifact_id=UUID(int=1), media_type="application/json", filename="o.json"
        ),
    )
    assert projected is not None
    assert isinstance(projected[0], TextPart)
    assert '"observation_omitted"' not in projected[0].text
    assert '"reason":"timed_out"' in projected[0].text
    # Nothing the page could keep without the guidance was cut to make room for it.
    assert '"url":"https://example.org/a"' in projected[0].text


def test_act_gains_the_report_and_a_pinned_chat_keeps_its_exact_spec() -> None:
    current, pinned = BrowserActTool.spec, PinnedBrowserActTool.spec
    assert current.version == "1.8.0"
    assert current.output_schema is not None
    assert "saved_file" in current.output_schema["properties"]
    assert pinned.version == "1.7.0"
    assert pinned.output_schema == OUTPUT_SCHEMA
    assert pinned.model_copy(update={"version": current.version}) == current.model_copy(
        update={"output_schema": OUTPUT_SCHEMA}
    )
    assert pinned.description == current.description
    assert pinned.input_schema == current.input_schema


async def test_a_pinned_chat_never_reports_a_file_its_schema_cannot_carry() -> None:
    writer = _Writer()
    provider = _DownloadingProvider(BrowserDownload.from_bytes("a.csv", CSV.encode()))

    result = await PinnedBrowserActTool(provider).execute(ARGUMENTS, _context(writer))

    assert result.ok and result.structured is not None
    validate_output(result.structured, PinnedBrowserActTool.spec.output_schema)
    assert "saved_file" not in result.structured
    assert writer.created == []


async def test_the_final_export_button_never_reports_silent_success_without_a_file() -> None:
    window = _DownloadWindow(
        approved=True, export_origin="https://site.test", deadline=asyncio.get_running_loop().time()
    )
    result = await PythonPlaywrightRuntime()._finish_download(window, names_a_file=True)
    assert result == BrowserDownload.refused("timed_out")
