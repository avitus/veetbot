"""artifact.export@2.0.0 turns text the model wrote into a file for the owner (ADR-0122)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.execution.local_workspace import LocalWorkspaceHandle
from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.artifacts import StoredArtifactRef
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import (
    ActionKind,
    ExecutionTarget,
    PolicyDecisionType,
    ProposedAction,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.tools import ToolExecutionContext, ToolFailureKind
from agent_core.policy.engine import DeterministicPolicyEngine
from agent_core.policy.loader import DEFAULT_RULESET
from agent_core.tools.artifact_export import ArtifactExportTool, LegacyArtifactExportTool
from agent_core.tools.validation import validate_and_normalize
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal, run, tool_context
from tests.unit.test_config import base_environment

ARTIFACT_ID = UUID(int=4242)


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(
        self,
        stream: AsyncIterator[bytes],
        filename: str,
        media_type: str,
        trust: TrustLevel,
    ) -> StoredArtifactRef:
        data = b"".join([chunk async for chunk in stream])
        self.calls.append(
            {"data": data, "filename": filename, "media_type": media_type, "trust": trust}
        )
        return StoredArtifactRef(
            artifact_id=ARTIFACT_ID,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            media_type=media_type,
        )


class _UnavailableCollaborator:
    def __getattr__(self, name: str) -> object:
        raise RuntimeError(f"collaborator {name!r} unavailable")


def _context(writer: object, workspace: object | None = None) -> ToolExecutionContext:
    return replace(
        tool_context(),
        workspace=_UnavailableCollaborator() if workspace is None else workspace,
        artifacts=writer,
    )


def _arguments(**overrides: Any) -> dict[str, Any]:
    normalized, _rendered, _digest = validate_and_normalize(
        {"filename": "Jev-Market-Update.txt", "media_type": "text/plain", **overrides},
        ArtifactExportTool.spec.input_schema,
    )
    return normalized


def _only_text(content: Sequence[object]) -> str:
    assert len(content) == 1
    part = content[0]
    assert isinstance(part, TextPart)
    return part.text


async def test_content_mode_stores_the_exact_text_without_touching_a_workspace() -> None:
    writer = _RecordingWriter()
    text = "Jev market update\n\n• Strong developer reception\n"

    result = await ArtifactExportTool().execute(_arguments(content=text), _context(writer))

    assert result.ok is True, result.failure
    assert writer.calls == [
        {
            "data": text.encode("utf-8"),
            "filename": "Jev-Market-Update.txt",
            "media_type": "text/plain",
            "trust": TrustLevel.EXTERNAL_UNTRUSTED,
        }
    ]
    assert result.structured == {
        "artifact_id": str(ARTIFACT_ID),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "size_bytes": len(text.encode("utf-8")),
        "media_type": "text/plain",
    }


async def test_content_mode_tells_the_model_the_file_is_attached() -> None:
    writer = _RecordingWriter()
    text = "a,b\n1,2\n"

    result = await ArtifactExportTool().execute(
        _arguments(content=text, filename="table.csv", media_type="text/csv"),
        _context(writer),
    )

    reported = json.loads(_only_text(result.content))
    assert reported == {
        "artifact_id": str(ARTIFACT_ID),
        "attached_to_reply": True,
        "size_bytes": len(text.encode("utf-8")),
    }


async def test_path_mode_tells_the_model_the_file_is_attached(tmp_path: Path) -> None:
    workspace = LocalWorkspaceHandle(tmp_path / "workspace")
    await workspace.write("output/report.txt", b"generated in the sandbox\n")
    writer = _RecordingWriter()

    result = await ArtifactExportTool().execute(
        _arguments(path="output/report.txt", filename="report.txt"),
        _context(writer, workspace),
    )

    assert result.ok is True, result.failure
    assert writer.calls[0]["data"] == b"generated in the sandbox\n"
    assert json.loads(_only_text(result.content)) == {
        "artifact_id": str(ARTIFACT_ID),
        "attached_to_reply": True,
        "size_bytes": len(b"generated in the sandbox\n"),
    }


async def test_the_pinned_legacy_version_also_reports_the_attachment(tmp_path: Path) -> None:
    workspace = LocalWorkspaceHandle(tmp_path / "workspace")
    await workspace.write("report.txt", b"legacy\n")
    writer = _RecordingWriter()

    result = await LegacyArtifactExportTool().execute(
        {"path": "report.txt", "filename": "report.txt", "media_type": "text/plain"},
        _context(writer, workspace),
    )

    assert result.ok is True, result.failure
    assert json.loads(_only_text(result.content))["attached_to_reply"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"path": "output/report.txt", "content": "both"},
        {},
        {"content": "<p>page</p>", "media_type": "text/html"},
        {"content": "bytes", "media_type": "application/octet-stream"},
        {"content": "nested", "filename": "notes/report.txt"},
        {"content": "escape", "filename": "..\\report.txt"},
        {"content": "dot", "filename": ".."},
        {"content": "dot", "filename": "."},
        {"content": "nul", "filename": "report\x00.txt"},
        {"content": "é" * 524_289},
    ],
    ids=[
        "path-and-content",
        "neither",
        "html",
        "binary-type",
        "nested-name",
        "backslash-name",
        "dot-dot-name",
        "dot-name",
        "nul-name",
        "over-one-mebibyte-encoded",
    ],
)
async def test_invalid_content_requests_fail_before_anything_is_stored(
    overrides: dict[str, Any],
) -> None:
    writer = _RecordingWriter()
    arguments = {"path": "", "filename": "report.txt", "media_type": "text/plain", **overrides}

    result = await ArtifactExportTool().execute(arguments, _context(writer))

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind is ToolFailureKind.INVALID_ARGUMENTS
    assert result.failure.reason_code == "tool.arguments_invalid"
    assert writer.calls == []


async def test_content_mode_reports_an_unavailable_writer_as_internal() -> None:
    result = await ArtifactExportTool().execute(
        _arguments(content="text"), _context(_UnavailableCollaborator())
    )

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.reason_code == "tool.internal_error"


def test_version_two_adds_content_and_defaults_the_path() -> None:
    spec = ArtifactExportTool.spec
    legacy = LegacyArtifactExportTool.spec

    assert (spec.name, spec.version) == ("artifact.export", "2.0.0")
    assert (legacy.name, legacy.version) == ("artifact.export", "1.0.0")
    assert spec.input_schema["properties"]["path"]["default"] == ""
    assert spec.input_schema["properties"]["content"] == {
        "type": "string",
        "maxLength": 1_048_576,
    }
    assert spec.input_schema["required"] == ["filename", "media_type"]
    assert legacy.input_schema["required"] == ["path", "filename", "media_type"]
    assert "content" not in legacy.input_schema["properties"]
    # The classification is unchanged, so the policy profile and its version are too.
    for candidate in (spec, legacy):
        assert candidate.side_effect is SideEffectClass.WORKSPACE_READ
        assert candidate.required_scopes == {"artifact.write"}
    assert spec.output_schema == legacy.output_schema


async def test_the_registry_keeps_both_versions_and_serves_two_by_default(tmp_path: Path) -> None:
    settings = replace(
        load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        artifact_root=tmp_path / "artifacts",
    )
    async with build(settings=settings) as composition:
        registry = composition.tool_pipeline._registry
        current = registry.get("artifact.export")
        pinned = registry.get("artifact.export", "1.0.0")

    assert current.spec.version == "2.0.0"
    assert pinned.spec == LegacyArtifactExportTool.spec


def _export_action(arguments: dict[str, Any]) -> ProposedAction:
    spec = ArtifactExportTool.spec
    normalized, _rendered, digest = validate_and_normalize(arguments, spec.input_schema)
    return ProposedAction(
        kind=ActionKind.TOOL_CALL,
        action_id=UUID(int=45),
        tenant_id="tenant-a",
        session_id=SESSION_ID,
        run_id=RUN_ID,
        step_number=1,
        name=spec.name,
        version=spec.version,
        summary="Export a file for the owner.",
        side_effect=spec.side_effect,
        risk=spec.risk,
        idempotency=spec.idempotency,
        required_scopes=set(spec.required_scopes),
        arguments=normalized,
        normalized_arguments_hash=digest,
        # Nothing the model wrote came verbatim from the owner, and the turn read
        # the web: the trust overlay applies wherever it can.
        argument_trust=dict.fromkeys(normalized, TrustLevel.EXTERNAL_UNTRUSTED),
        origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
        target=ExecutionTarget(kind="in_process", isolated=False, network_enabled=False),
        evaluated_at=NOW,
    )


async def test_the_shipped_policy_allows_text_exports_and_still_confines_paths() -> None:
    """ADR-0122 changes no policy file: the defaulted path is what the rule admits."""
    engine = DeterministicPolicyEngine(DEFAULT_RULESET)

    written = await engine.evaluate(
        _export_action({"content": "notes", "filename": "notes.md", "media_type": "text/markdown"}),
        principal(),
        run(),
    )
    exported = await engine.evaluate(
        _export_action(
            {"path": "output/chart.png", "filename": "chart.png", "media_type": "image/png"}
        ),
        principal(),
        run(),
    )
    escaped = await engine.evaluate(
        _export_action({"path": "../secrets", "filename": "x.txt", "media_type": "text/plain"}),
        principal(),
        run(),
    )

    assert written.decision is PolicyDecisionType.ALLOW
    assert exported.decision is PolicyDecisionType.ALLOW
    assert escaped.decision is PolicyDecisionType.DENY
