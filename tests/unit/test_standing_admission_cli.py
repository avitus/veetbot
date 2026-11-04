"""`agent memory standing-admission` writes the owner's standing files from the live digests."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_core.cli.main import app
from agent_core.memory.semantic_deployment import (
    IndexProfiles,
    SemanticCompositionInputs,
    bind_index_manifest,
)
from tests.contract.support import principal
from tests.contract.test_local_semantic_preparation import CONFIG, LOCAL
from tests.integration.m2_support import memory_settings
from tests.unit.test_bounded_recall_admission import Clock

pytestmark = pytest.mark.static

INSTRUCTION = (
    "Synthetic owner direction: keep semantic recall on; it has been turned on and off "
    "for months without progress."
)


def _overlay(tmp_path: Path, *, local: bool, reader: bool = True) -> tuple[Any, dict[str, Any]]:
    overlay = tmp_path / "config" / "memory"
    overlay.mkdir(parents=True)
    if local:
        configuration: dict[str, Any] = {
            "embedding": CONFIG.model_dump(mode="json"),
            "local": LOCAL.model_dump(mode="json"),
            "model_directory": str(tmp_path / "model"),
        }
        if reader:
            configuration["reader_sha256"] = "a" * 64
    else:
        from tests.contract.semantic_indexing_cases import INDEX_CONFIG

        remote = INDEX_CONFIG.model_copy(
            update={
                "space": INDEX_CONFIG.space.model_copy(update={"provider": "openai"}),
                "residency_provider": "openai",
            }
        )
        configuration = {"credential_ref": "openai", "embedding": remote.model_dump(mode="json")}
    document = {"schema_version": 1, "configurations": [configuration]}
    (overlay / "semantic-index.yaml").write_text(json.dumps(document))
    return replace(memory_settings(), config_dir=overlay.parent), document


def _inputs(*, semantic_enabled: bool = True) -> SemanticCompositionInputs:
    return SemanticCompositionInputs(
        configuration={
            "policy_version": "policy@1",
            "principal": {
                "tenant": principal().tenant_id,
                "owner": principal().principal_id,
                "scopes": [],
                "roles": ["user"],
            },
            "memory": {"retrieval": {"semantic_enabled": semantic_enabled}},
            "storage": "postgres",
        },
        formation={"policy": "formation"},
        reader={"model": "reader"},
    )


def _fake_build(
    monkeypatch: pytest.MonkeyPatch, inputs: SemanticCompositionInputs
) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    @asynccontextmanager
    async def build(**kwargs: Any) -> Any:
        seen.append(kwargs)
        kwargs["recall_capture"](inputs)
        pytest.fail("Capture must stop before exposing a runtime")
        yield

    monkeypatch.setattr("agent_core.bootstrap.build", build)
    return seen


def test_standing_admission_writes_binding_files_from_the_live_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, document = _overlay(tmp_path, local=True)
    monkeypatch.setattr("agent_core.bootstrap.load_settings", lambda: settings)
    inputs = _inputs()
    seen = _fake_build(monkeypatch, inputs)
    words = tmp_path / "owner.txt"
    words.write_text(INSTRUCTION + "\n")
    out = tmp_path / "standing"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "memory",
            "standing-admission",
            "--instruction-file",
            str(words),
            "--reference",
            "owner-chat-message",
            "--rollback-build-ref",
            "b" * 40,
            "--out",
            str(out),
            "--supersedes",
            "synthetic-trial",
            "--decided-at",
            "2026-11-03T17:00:00+00:00",
        ],
    )
    assert result.exit_code == 0, result.output
    printed = json.loads(result.stdout)
    assert INSTRUCTION not in result.output, "The owner's words are never echoed"
    assert seen[0]["model_metadata_only"] and seen[0]["storage"] == "postgres"
    profile = IndexProfiles.model_validate(document).configurations[0]
    dependencies = inputs.dependencies(profile)
    assert (
        printed["embedding_configuration_sha256"] == dependencies["embedding_configuration_sha256"]
    )
    assert printed["embedding_space_key"] == profile.embedding.space.key
    assert printed["tenant_id"] == principal().tenant_id
    manifest, evidence = Path(printed["manifest"]), Path(printed["evidence"])
    assert manifest.parent == out and evidence.parent == out
    facts = json.loads(evidence.read_text())
    assert facts["supersedes"] == "synthetic-trial"
    assert facts["decided_at"].startswith("2026-11-03T17:00:00")
    assert facts["observed_dependencies"] == dependencies
    clock = Clock()
    clock.value = datetime.now(UTC)  # the CLI stamps activation with the real clock
    admitted = bind_index_manifest(
        root=tmp_path,
        manifest=manifest,
        evidence=evidence,
        release_id="release-aaaaaaa",
        dependencies=dependencies,
        principal=principal(),
        clock=clock,
    )
    assert admitted()
    # A second run never overwrites the installed pair.
    again = runner.invoke(
        app,
        [
            "memory",
            "standing-admission",
            "--instruction-file",
            str(words),
            "--reference",
            "owner-chat-message",
            "--rollback-build-ref",
            "b" * 40,
            "--out",
            str(out),
        ],
    )
    assert again.exit_code == 1 and "already exist" in again.output


@pytest.mark.parametrize("reason", ["remote", "query_disabled", "no_reader", "reconsolidation"])
def test_standing_admission_refuses_a_remote_or_query_disabled_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """Every condition composition would refuse is refused before a file is written."""

    settings, _ = _overlay(tmp_path, local=reason != "remote", reader=reason != "no_reader")
    if reason == "reconsolidation":
        settings = replace(settings, memory_reconsolidation_enabled=True)
    monkeypatch.setattr("agent_core.bootstrap.load_settings", lambda: settings)
    _fake_build(monkeypatch, _inputs(semantic_enabled=reason != "query_disabled"))
    words = tmp_path / "owner.txt"
    words.write_text(INSTRUCTION)
    result = CliRunner().invoke(
        app,
        [
            "memory",
            "standing-admission",
            "--instruction-file",
            str(words),
            "--reference",
            "owner-chat-message",
            "--rollback-build-ref",
            "b" * 40,
            "--out",
            str(tmp_path / "standing"),
        ],
    )
    assert result.exit_code == 1, result.output
    expected = {
        "remote": "local encoder",
        "query_disabled": "semantic_enabled",
        "no_reader": "reviewed reader",
        "reconsolidation": "reconsolidation",
    }[reason]
    assert expected in result.output
    assert not (tmp_path / "standing").exists()
