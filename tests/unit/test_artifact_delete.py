"""ADR-0277: artifact.delete removes only files Chat generated in this chat."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.bootstrap import DEFAULT_AGENT_INSTRUCTIONS
from agent_core.context.rendering import UNINDEXED_DEFERRED_TOOLS
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind, ToolResult
from agent_core.tools.artifact_delete import ArtifactDeleteTool
from agent_core.tools.messages import TOOL_MESSAGES
from agent_core.tools.registry import validate_registration
from tests.contract.support import PRINCIPAL_ID, RUN_ID, SESSION_ID, TENANT, principal, run
from tests.contract.support import tool_context as contract_tool_context
from tests.unit.test_history_cache_window import _factory


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
        self.tool = ArtifactDeleteTool(self.factory, self.clock)
        return self

    async def file(
        self,
        name: str = "stencil.png",
        *,
        origin: ArtifactOrigin = ArtifactOrigin.MODEL_OUTPUT,
        session_id: UUID = SESSION_ID,
        principal_id: str = PRINCIPAL_ID,
    ) -> UUID:
        writer = self.writers.for_run(
            tenant_id=TENANT,
            principal_id=principal_id,
            session_id=session_id,
            run_id=RUN_ID,
            origin=origin,
        )
        ref = await writer.create(
            _bytes(f"bytes of {name}".encode()),
            name,
            "image/png",
            TrustLevel.INTERNAL_TOOL,
        )
        return ref.artifact_id

    async def delete(
        self, ids: list[UUID | str], context: ToolExecutionContext | None = None
    ) -> ToolResult:
        arguments: dict[str, Any] = {"artifact_ids": [str(value) for value in ids]}
        return await self.tool.execute(arguments, context or contract_tool_context())

    async def live(self, artifact_id: UUID) -> bool:
        async with self.factory() as uow:
            artifact = await uow.artifacts.get(artifact_id, principal())
        return artifact.expires_at is None or artifact.expires_at > self.clock.now()


@pytest.fixture
async def stack(tmp_path: Path) -> _Stack:
    return await _Stack(tmp_path).start()


def test_the_spec_is_an_internal_serial_delete_that_needs_no_approval() -> None:
    spec = ArtifactDeleteTool.spec
    validate_registration(spec)
    assert (spec.name, spec.version) == ("artifact.delete", "1.0.0")
    # NONE keeps the policy profile untouched and asks for no approval card;
    # the tool itself refuses a turn that read external content.
    assert spec.side_effect is SideEffectClass.NONE
    assert spec.risk is RiskLevel.MEDIUM
    assert spec.idempotency is IdempotencyClass.IDEMPOTENT
    assert spec.required_scopes == {"artifact.write"}
    assert spec.allow_parallel is False
    assert spec.output_trust is TrustLevel.INTERNAL_TOOL
    assert "artifact.untrusted_turn" in TOOL_MESSAGES
    assert "artifact.not_deletable" in TOOL_MESSAGES


async def test_generated_files_in_this_chat_are_deleted(stack: _Stack) -> None:
    drawn = await stack.file("stencil.png")
    exported = await stack.file("stencil.pdf", origin=ArtifactOrigin.SANDBOX_EXPORT)
    kept = await stack.file("final.png")

    result = await stack.delete([drawn, f"artifact:{exported}"])

    assert result.ok, result.failure
    assert result.structured == {
        "deleted": [str(drawn), str(exported)],
        "already_deleted": [],
        "not_deletable": [],
    }
    text = result.content[0].text  # type: ignore[union-attr]
    assert "stencil.png" in text and "stencil.pdf" in text
    assert not await stack.live(drawn)
    assert not await stack.live(exported)
    assert await stack.live(kept)
    # The existing sweeper removes the bytes and rows of what was expired.
    assert await stack.writers.sweep_expired() == 2
    assert await stack.live(kept)


@pytest.mark.parametrize(
    "origin",
    [ArtifactOrigin.UPLOAD, ArtifactOrigin.TOOL_OUTPUT, ArtifactOrigin.KNOWLEDGE_SOURCE],
)
async def test_files_chat_did_not_generate_are_left_alone(
    stack: _Stack, origin: ArtifactOrigin
) -> None:
    generated = await stack.file("stencil.png")
    other = await stack.file("photo.jpg", origin=origin)

    result = await stack.delete([generated, other])

    assert result.ok, result.failure
    assert result.structured == {
        "deleted": [str(generated)],
        "already_deleted": [],
        "not_deletable": [str(other)],
    }
    assert not await stack.live(generated)
    assert await stack.live(other)


async def test_a_call_with_nothing_deletable_fails(stack: _Stack) -> None:
    upload = await stack.file("photo.jpg", origin=ArtifactOrigin.UPLOAD)
    elsewhere = await stack.file("other-chat.png", session_id=uuid4())
    foreign = await stack.file("foreign.png", principal_id="someone-else")

    for target in (upload, elsewhere, foreign, uuid4()):
        result = await stack.delete([target])
        assert not result.ok
        assert result.failure is not None
        assert result.failure.kind is ToolFailureKind.NOT_FOUND
        assert result.failure.reason_code == "artifact.not_deletable"
    assert await stack.live(upload)
    assert await stack.live(elsewhere)


async def test_a_turn_that_read_external_content_cannot_delete(stack: _Stack) -> None:
    generated = await stack.file("stencil.png")
    untrusted = dataclasses.replace(
        contract_tool_context(), origin_trust=TrustLevel.EXTERNAL_UNTRUSTED
    )

    result = await stack.delete([generated], untrusted)

    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind is ToolFailureKind.PERMISSION
    assert result.failure.reason_code == "artifact.untrusted_turn"
    assert await stack.live(generated)


async def test_a_repeated_delete_reports_what_is_already_gone(stack: _Stack) -> None:
    first = await stack.file("stencil.png")
    second = await stack.file("stencil-v2.png")
    assert (await stack.delete([first])).ok

    result = await stack.delete([first, second])

    assert result.ok, result.failure
    assert result.structured == {
        "deleted": [str(second)],
        "already_deleted": [str(first)],
        "not_deletable": [],
    }


def test_chat_learns_of_the_tool_from_its_instructions_not_the_index() -> None:
    # ADR-0277: the index has no headroom to spare (ADR-0267), so the tool is an
    # unindexed deferral and the default instructions name it.
    assert "artifact.delete" in UNINDEXED_DEFERRED_TOOLS
    assert "call artifact.delete through tool.call" in DEFAULT_AGENT_INSTRUCTIONS


def test_the_schema_bounds_the_list() -> None:
    schema = ArtifactDeleteTool.spec.input_schema
    ids = schema["properties"]["artifact_ids"]
    assert schema["required"] == ["artifact_ids"]
    assert schema["additionalProperties"] is False
    assert (ids["minItems"], ids["maxItems"], ids["uniqueItems"]) == (1, 50, True)
