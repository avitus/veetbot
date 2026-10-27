"""ADR-0261: artifact.read_capture opens only this chat's untrusted captures."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.captures import CAPTURE_METADATA_KEY, CaptureSections, capture_text
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tool_output import content_bytes
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolInvocation,
    ToolInvocationStatus,
    ToolResult,
)
from agent_core.tools.artifact_read_capture import ArtifactReadCaptureTool
from agent_core.tools.messages import TOOL_MESSAGES
from agent_core.tools.registry import validate_registration
from tests.contract.support import (
    NOW,
    PRINCIPAL_ID,
    RUN_ID,
    SESSION_ID,
    TENANT,
    principal,
    run,
    tool_context,
)
from tests.unit.test_history_cache_window import _factory

FILING = "".join(
    f"Item {n}. {'Results of Operations' if n % 40 == 0 else 'Risk factor'} {n}\n"
    for n in range(12_000)
)
WEB_MARK = {"tool": "web.fetch", "required_scopes": []}


async def _bytes(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


class _Stack:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    async def start(self) -> _Stack:
        self.clock, self.factory = await _factory()
        async with self.factory() as uow:
            await uow.runs.create(run())
        self.store = FilesystemArtifactStore(self.tmp_path)
        self.writers = ArtifactWriterFactory(
            self.factory, self.store, self.clock, SequenceIdFactory()
        )
        self.tool = ArtifactReadCaptureTool(
            self.factory, self.store, self.clock, step_maximum_reads=2, run_maximum_reads=3
        )
        return self

    async def capture(
        self,
        text: str = FILING,
        *,
        mark: dict[str, Any] | None = WEB_MARK,
        trust: TrustLevel = TrustLevel.EXTERNAL_UNTRUSTED,
        session_id: UUID = SESSION_ID,
        principal_id: str = PRINCIPAL_ID,
        origin: ArtifactOrigin = ArtifactOrigin.TOOL_OUTPUT,
        filename: str = "web-fetch-output.json",
        raw: bytes | None = None,
    ) -> UUID:
        writer = self.writers.for_run(
            tenant_id=TENANT,
            principal_id=principal_id,
            session_id=session_id,
            run_id=RUN_ID,
            origin=origin,
        )
        ref = await writer.create(
            _bytes(raw if raw is not None else content_bytes([TextPart(text=text)])),
            filename,
            "application/json",
            trust,
            metadata=None if mark is None else {CAPTURE_METADATA_KEY: mark},
        )
        return ref.artifact_id

    async def read(
        self, arguments: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> ToolResult:
        return await self.tool.execute(arguments, context or tool_context())

    async def record_read(self, number: int, *, step: int, status: ToolInvocationStatus) -> None:
        async with self.factory() as uow:
            await uow.invocations.create(
                ToolInvocation(
                    id=UUID(int=9_000 + number),
                    run_id=RUN_ID,
                    session_id=SESSION_ID,
                    step_number=step,
                    call_id=f"read-{number}",
                    tool_name="artifact.read_capture",
                    tool_version="1.0.0",
                    side_effect=SideEffectClass.NONE,
                    risk=RiskLevel.LOW,
                    idempotency_class=IdempotencyClass.READ_ONLY,
                    status=status,
                    raw_arguments="{}",
                    idempotency_key=f"read-{number}",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )


@pytest.fixture
async def stack(tmp_path: Path) -> _Stack:
    return await _Stack(tmp_path).start()


def _failure_code(result: ToolResult) -> str:
    assert not result.ok
    assert result.failure is not None
    return result.failure.reason_code


def test_the_reader_is_a_registered_untrusted_read_with_no_scopes() -> None:
    spec = ArtifactReadCaptureTool.spec
    validate_registration(spec)
    assert spec.name == "artifact.read_capture"
    assert spec.side_effect is SideEffectClass.NONE
    assert spec.risk is RiskLevel.LOW
    assert spec.idempotency is IdempotencyClass.READ_ONLY
    assert spec.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert spec.required_scopes == set()
    assert spec.target_kind == "in_process"
    assert spec.maximum_output_bytes == 16_384
    assert spec.allow_parallel is False
    for code in (
        "tool.capture.not_found",
        "tool.capture.section_out_of_range",
        "tool.capture.step_limit",
        "tool.capture.run_limit",
    ):
        assert code in TOOL_MESSAGES


async def test_a_section_is_the_capture_text_under_its_header(stack: _Stack) -> None:
    artifact = await stack.capture()
    sections = CaptureSections(FILING, artifact_id=artifact, tool="web.fetch")
    for argument in (f"artifact:{artifact}", str(artifact)):
        result = await stack.read({"artifact": argument, "section": 3})
        assert result.ok
        assert result.output_trust is TrustLevel.EXTERNAL_UNTRUSTED
        assert result.content == [TextPart(text=sections.section_text(3))]
        assert len(content_bytes(result.content)) <= 16_384
        assert result.structured == {
            "artifact": str(artifact),
            "tool": "web.fetch",
            "sections": sections.count,
            "section": 3,
            "text": sections.body(3),
        }
    first = await stack.read({"artifact": str(artifact)})
    assert first.content == [TextPart(text=sections.section_text(1))]


async def test_find_returns_sections_and_snippets(stack: _Stack) -> None:
    artifact = await stack.capture()
    sections = CaptureSections(FILING, artifact_id=artifact, tool="web.fetch")
    result = await stack.read({"artifact": str(artifact), "find": "results of OPERATIONS"})
    assert result.ok
    assert result.content == [
        TextPart(text=sections.find_text("results of OPERATIONS", from_section=1))
    ]
    assert result.structured is not None
    assert result.structured["total"] == 300
    assert len(result.structured["matches"]) == 20
    later = await stack.read(
        {"artifact": str(artifact), "find": "results of operations", "section": 5}
    )
    assert later.structured is not None
    assert later.structured["matches"][0]["section"] >= 5


async def test_a_section_past_the_end_is_refused(stack: _Stack) -> None:
    artifact = await stack.capture("short\n")
    result = await stack.read({"artifact": str(artifact), "section": 2})
    assert _failure_code(result) == "tool.capture.section_out_of_range"


async def test_a_partial_capture_reads_as_text(stack: _Stack) -> None:
    raw = content_bytes([TextPart(text="雪" * 9_000)])[:20_000]
    artifact = await stack.capture(raw=raw, filename="web-fetch-output.partial.json")
    result = await stack.read({"artifact": str(artifact)})
    assert result.ok
    assert result.structured is not None
    assert capture_text(raw).startswith(result.structured["text"])


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "other_session",
        "other_principal",
        "unmarked",
        "trusted",
        "export",
        "upload_shaped",
        "chart",
        "expired",
        "revoked_scope",
    ],
)
async def test_everything_but_this_chats_untrusted_capture_is_not_found(
    stack: _Stack, case: str
) -> None:
    context = tool_context()
    if case == "unknown":
        artifact = UUID(int=404)
    elif case == "other_session":
        artifact = await stack.capture(session_id=UUID(int=777))
    elif case == "other_principal":
        artifact = await stack.capture(principal_id="someone-else")
    elif case == "unmarked":
        artifact = await stack.capture(mark=None)
    elif case == "trusted":
        artifact = await stack.capture(trust=TrustLevel.INTERNAL_TOOL)
    elif case == "export":
        artifact = await stack.capture(origin=ArtifactOrigin.SANDBOX_EXPORT)
    elif case == "upload_shaped":
        artifact = await stack.capture(origin=ArtifactOrigin.MODEL_OUTPUT)
    elif case == "chart":
        artifact = await stack.capture(mark=None, filename="chart.png")
    elif case == "expired":
        artifact = await stack.capture()
        stack.clock.advance(timedelta(days=31))
    else:
        artifact = await stack.capture(
            mark={"tool": "mcp.gmail_read.get_thread", "required_scopes": ["mcp.gmail_read.use"]}
        )
    result = await stack.read({"artifact": str(artifact)}, context)
    assert _failure_code(result) == "tool.capture.not_found"


async def test_a_capture_opens_while_its_tools_scopes_are_held(stack: _Stack) -> None:
    artifact = await stack.capture(
        mark={"tool": "mcp.gmail_read.get_thread", "required_scopes": ["mcp.gmail_read.use"]}
    )
    holder = principal().model_copy(update={"scopes": {"mcp.gmail_read.use"}})
    context = dataclasses.replace(tool_context(), principal=holder)
    result = await stack.read({"artifact": str(artifact)}, context)
    assert result.ok


async def test_two_reads_a_step_and_the_run_limit_count_only_succeeded_reads(
    stack: _Stack,
) -> None:
    artifact = await stack.capture()
    arguments = {"artifact": str(artifact), "section": 1}
    step_two = dataclasses.replace(tool_context(), step_number=2)
    # A refused or failed read does not count.
    await stack.record_read(1, step=2, status=ToolInvocationStatus.FAILED)
    await stack.record_read(2, step=2, status=ToolInvocationStatus.SUCCEEDED)
    assert (await stack.read(arguments, step_two)).ok
    await stack.record_read(3, step=2, status=ToolInvocationStatus.SUCCEEDED)
    assert _failure_code(await stack.read(arguments, step_two)) == "tool.capture.step_limit"
    # A new step may read again until the run's limit; a resumed run counts the
    # reads its invocations already recorded.
    step_three = dataclasses.replace(tool_context(), step_number=3)
    await stack.record_read(4, step=3, status=ToolInvocationStatus.SUCCEEDED)
    result = await stack.read(arguments, step_three)
    assert _failure_code(result) == "tool.capture.run_limit"
