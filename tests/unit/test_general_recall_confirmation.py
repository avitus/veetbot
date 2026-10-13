"""Fabricated captures test binding mechanics, never supply release evidence."""

from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest

from agent_core.domain.general_recall_release import Channel, PathKind, Pipeline
from agent_core.evals.general_recall_confirmation import (
    ConfirmationPrincipal,
    ConfirmationVariant,
    ConfirmedExecution,
    RecallExecutionManifest,
)
from tests.contract.support import NOW
from tests.unit.test_general_recall_release import fixture as release_fixture


def capture(configuration: dict[str, Any] | None = None) -> RecallExecutionManifest:
    pipelines: tuple[Pipeline, ...] = ("seeded", "formation")
    paths: tuple[PathKind, ...] = ("automatic", "deliberate")
    channels: tuple[Channel, ...] = ("sparse", "dense", "hybrid")
    principal = ConfirmationPrincipal(tenant="evaluation", owner="selected", scopes=(), roles=())
    if configuration is None:
        configuration = {
            "version": "recall-fixture-inputs@1",
            "execution": "runtime",
            "production_reader": True,
            "storage": "postgres",
            "composition": {"configuration": {"principal": principal.model_dump(mode="json")}},
        }
    else:
        principal = ConfirmationPrincipal.model_validate(
            configuration["composition"]["configuration"]["principal"]
        )
    return RecallExecutionManifest(
        version="recall-execution-manifest@1",
        captured_at=NOW,
        principal=principal,
        maximum_cost_usd=Decimal("100"),
        variants=tuple(
            ConfirmationVariant(
                pipeline=pipeline,
                path=path,
                channel=channel,
                configuration={
                    **deepcopy(configuration),
                    "pipeline": pipeline,
                    "path": path,
                    "channel": channel,
                },
            )
            for pipeline in pipelines
            for path in paths
            for channel in channels
        ),
    )


def binding(manifest: RecallExecutionManifest) -> ConfirmedExecution:
    identity = release_fixture().identity.model_copy(
        update={"evaluated_inputs_sha256": "e" * 64, "execution_manifest_sha256": manifest.digest}
    )
    return ConfirmedExecution(identity, manifest)


def test_confirmation_binds_independent_authority_and_complete_matrix() -> None:
    confirmation = binding(capture())
    for variant in confirmation.manifest.variants:
        confirmation.validate(variant.configuration)
        confirmation.validate_observation(
            {
                **variant.model_dump(exclude={"configuration"}),
                **confirmation.observation_fields(),
                "configuration_sha256": variant.digest,
            }
        )
    assert confirmation.manifest.principal.principal().principal_id == "selected"


def test_confirmation_rejects_the_unpinned_diagnostic_reader() -> None:
    from agent_core.evals.general_recall_confirmation import RecallExecutionManifest

    manifest = capture()
    for variant in manifest.variants:
        variant.configuration["production_reader"] = False
    with pytest.raises(ValueError, match="production reader"):
        RecallExecutionManifest.model_validate(manifest.model_dump())


@pytest.mark.parametrize("change", ["principal", "configuration", "identity", "unbound", "matrix"])
def test_confirmation_refuses_changed_or_unbound_inputs(change: str) -> None:
    manifest = capture()
    confirmation = binding(manifest)
    variant = manifest.variants[0]
    row = {
        **variant.model_dump(exclude={"configuration"}),
        **confirmation.observation_fields(),
        "configuration_sha256": variant.digest,
    }
    if change == "principal":
        variant.configuration["composition"]["configuration"]["principal"]["owner"] = "foreign"
    elif change == "configuration":
        variant.configuration["noise"] = 3000
    elif change == "identity":
        row["identity_sha256"] = "f" * 64
    elif change == "unbound":
        row.pop("execution_manifest_sha256")
    else:
        row["pipeline"] = "unrecognized"
    with pytest.raises(ValueError):
        confirmation.validate_observation(row)


def test_confirmation_rejects_runtime_drift_before_work() -> None:
    confirmation = binding(capture())
    actual = deepcopy(confirmation.manifest.variants[0].configuration)
    actual["model"] = "changed"
    with pytest.raises(ValueError, match="runtime configuration changed"):
        confirmation.validate(actual)


@pytest.mark.parametrize("change", ["duplicate", "authority", "storage", "direct"])
def test_capture_requires_every_runtime_variant_with_one_authority(change: str) -> None:
    raw = capture().model_dump(mode="json")
    if change == "duplicate":
        raw["variants"][1] = raw["variants"][0]
    elif change == "authority":
        raw["principal"]["owner"] = "other"
    else:
        raw["variants"][0]["configuration"]["storage" if change == "storage" else "execution"] = (
            "memory" if change == "storage" else "direct"
        )
    with pytest.raises(ValueError):
        RecallExecutionManifest.model_validate(raw)
