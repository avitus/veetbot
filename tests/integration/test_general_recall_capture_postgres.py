"""Read-only schema snapshots distinguish drift from ordinary stored content."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine
from agent_core.adapters.persistence.revision import EXPECTED_REVISION
from agent_core.bootstrap import build
from agent_core.domain.general_recall_release import PathKind
from agent_core.evals.general_recall_capture import capture_schema
from agent_core.memory.semantic_deployment import configuration_digest
from tests.integration.disposable_database import clone_recall_database
from tests.integration.m2_support import database_settings


@pytest.mark.parametrize("model_policy", ["astra", "fable"])
async def test_confirmation_captures_distinct_chat_and_formation_models(
    tmp_path: Path, model_policy: str
) -> None:
    from decimal import Decimal

    from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
    from agent_core.config import PACKAGE_ROOT
    from agent_core.evals.general_memory_recall_driver import OWNER
    from agent_core.evals.general_recall_composition import system_clock
    from agent_core.evals.general_recall_confirmation import capture_runtime_configurations
    from agent_core.evals.general_recall_experiment import Experiment
    from agent_core.model.registry import ProviderRegistry, StaticModelRouter
    from tests.contract.test_local_semantic_preparation import LOCAL

    router = StaticModelRouter(
        ProviderRegistry.load(PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS), system_clock()
    )
    resolved = await router.resolve(model_policy, tenant_id=OWNER.tenant_id)
    async with clone_recall_database() as database_url:
        capture = capture_runtime_configurations(
            Experiment(
                corpus=tmp_path / "unopened.jsonl",
                output=tmp_path / "unused",
                storage="postgres",
                disposable_database=True,
                production_reader=True,
                model_policy=model_policy,
            ),
            settings=replace(database_settings(), database_url=database_url),
            principal=OWNER,
            resolved=resolved,
            local=LOCAL,
            maximum_cost_usd=Decimal("100"),
        )
        if model_policy == "fable":
            with pytest.raises(ValueError, match="share the budgeted reader provider"):
                await capture
            return
        manifest = await capture
    assert len(manifest.variants) == 12
    for variant in manifest.variants:
        assert variant.configuration["reader"]["model"] == "gpt-6-astra"
        if variant.pipeline == "formation":
            assert variant.configuration["composition"]["formation"]["default"] == {
                "model_policy": "balanced",
                "reasoning_effort": None,
            }


@pytest.mark.parametrize("path", ["automatic", "deliberate"])
async def test_confirmatory_reader_preserves_real_postgres_pin(
    tmp_path: Path, path: PathKind
) -> None:
    from tests.unit.test_general_recall_execution import (
        test_confirmatory_reader_keeps_production_pin_and_reasoning as exercise,
    )

    async with clone_recall_database() as database_url:
        await exercise(
            tmp_path,
            path,
            settings=replace(database_settings(), database_url=database_url),
            storage="postgres",
        )


async def test_actual_schema_fingerprint_reaches_admission_configuration() -> None:
    async with clone_recall_database() as database_url:
        expected = configuration_digest(await capture_schema(database_url))
        settings = replace(database_settings(), database_url=database_url)
        async with build(settings=settings, storage="postgres", model_metadata_only=True) as app:
            assert app.recall_composition.configuration.get("schema_sha256") == expected


async def test_schema_capture_records_actual_indexes_and_never_reads_rows() -> None:
    async with clone_recall_database() as database_url:
        original = await capture_schema(database_url)
        assert original["revision"] == EXPECTED_REVISION
        assert original["extensions"]
        assert any("memory_vectors" in row["name"] for row in original["relations"])
        engine = create_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("CREATE TABLE recall_capture_probe (id bigint, body text)")
                )
                await connection.execute(
                    text("INSERT INTO recall_capture_probe VALUES (1, 'SENTINEL_PRIVATE_CONTENT')")
                )
            with_table = await capture_schema(database_url)
            assert with_table != original
            assert "SENTINEL_PRIVATE_CONTENT" not in json.dumps(with_table)
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE recall_capture_probe SET body = 'CHANGED_PRIVATE_CONTENT'")
                )
            assert await capture_schema(database_url) == with_table
            async with engine.begin() as connection:
                await connection.execute(
                    text("CREATE INDEX recall_capture_probe_index ON recall_capture_probe (id)")
                )
            with_index = await capture_schema(database_url)
            assert with_index != with_table
            assert any(
                "recall_capture_probe_index" in row["definition"] for row in with_index["indexes"]
            )
            async with engine.begin() as connection:
                await connection.execute(
                    text("ALTER TABLE recall_capture_probe ENABLE ROW LEVEL SECURITY")
                )
                await connection.execute(
                    text(
                        "CREATE POLICY capture_probe_policy ON recall_capture_probe USING (id > 0)"
                    )
                )
            assert await capture_schema(database_url) != with_index
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE alembic_version SET version_num = 'unexpected_revision'")
                )
            with pytest.raises(ValueError, match="revision"):
                await capture_schema(database_url)
        finally:
            await engine.dispose()
