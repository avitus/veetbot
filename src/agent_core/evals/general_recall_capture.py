"""Independent deployment inputs for a preregistered recall evaluation."""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy.engine import make_url

from agent_core.config import load_config_document
from agent_core.evals.general_recall_database import validate_evaluation_database
from agent_core.memory.semantic_deployment import (
    DOCUMENT,
    IndexProfiles,
    SemanticCompositionInputs,
)

if TYPE_CHECKING:
    from agent_core.config import Settings


async def capture_deployment(
    settings: Settings, scratch: Settings, *, authorized: bool, model_policy: str = "balanced"
) -> dict[str, Any]:
    """Resolve the target graph while directing bootstrap writes to a scratch DB.

    Only database and artifact destinations are replaced. Authority, profiles,
    model catalogs and policy selection come from the target settings. Capture
    cannot authorize activation or expose an executable composition.
    """
    from agent_core.adapters.models.local_embedding_assets import read_local_assets
    from agent_core.evals.general_recall_composition import build

    validate_evaluation_database(scratch, authorized=authorized)
    target_url, scratch_url = make_url(settings.database_url), make_url(scratch.database_url)
    # Require different database names even across hosts: DNS aliases, credentials
    # and connection options cannot establish isolation.
    if target_url.database == scratch_url.database:
        raise ValueError("Deployment capture requires a separate scratch database")
    document = load_config_document(settings, DOCUMENT)
    if "${" in json.dumps(document):
        raise ValueError("Deployment capture cannot interpolate index configuration")
    profiles = IndexProfiles.model_validate(document)
    if len(profiles.configurations) != 1:
        raise ValueError("Deployment capture requires one explicit index configuration")
    profile = profiles.configurations[0]
    if profile.local is None or profile.model_directory is None:
        raise ValueError("Confirmatory recall requires verified local model assets")
    try:
        model, tokenizer = read_local_assets(profile.model_directory, profile.local)
    except (OSError, ValueError, RuntimeError) as error:
        raise ValueError("Deployment capture could not verify local model assets") from error
    assets = {
        "model": {"sha256": profile.local.model_sha256, "bytes": len(model)},
        "tokenizer": {"sha256": profile.local.tokenizer_sha256, "bytes": len(tokenizer)},
    }
    del model, tokenizer

    class CapturedError(Exception):
        pass

    result: dict[str, Any] = {}

    def capture(inputs: SemanticCompositionInputs) -> None:
        result.update(
            version="recall-deployment-inputs@1",
            composition={
                "configuration": inputs.configuration,
                "formation": inputs.formation,
                "reader": inputs.reader,
            },
            indexing=profile.model_dump(mode="json"),
            assets=assets,
            dependencies=inputs.dependencies(profile),
        )
        raise CapturedError

    with tempfile.TemporaryDirectory(prefix="m33-deployment-capture-") as temporary:
        effective = replace(
            settings, database_url=scratch.database_url, artifact_root=Path(temporary)
        )
        try:
            async with build(
                settings=effective,
                storage="postgres",
                model_metadata_only=True,
                recall_capture=capture,
                model_policy=model_policy,
            ):
                raise ValueError("Deployment capture exposed a runtime")
        except CapturedError:
            # Detach the snapshot from mutable graph dictionaries.
            return json.loads(json.dumps(result))  # type: ignore[no-any-return]
    raise ValueError("Deployment capture did not produce actual inputs")


async def capture_schema(database_url: str) -> dict[str, Any]:
    """Read actual schema definitions through the same production capture adapter."""
    from agent_core.adapters.persistence.database import create_engine
    from agent_core.adapters.persistence.recall_schema import capture_recall_schema

    if make_url(database_url).drivername != "postgresql+asyncpg":
        raise ValueError("Recall schema capture requires PostgreSQL")
    engine = create_engine(database_url)
    try:
        return await capture_recall_schema(engine)
    finally:
        await engine.dispose()
