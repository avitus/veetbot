"""Fabricated matrices exercise source accounting; they are not release evidence."""

import hashlib
from typing import Any

import pytest

from agent_core.domain.general_recall_release import RecallReleaseBundle, RecallScenario
from agent_core.memory.general_recall_evidence import score_release
from tests.unit.test_general_recall_release import digest, fixture


def recovered_bundle(**source_changes: Any) -> RecallReleaseBundle:
    raw = fixture().model_dump(mode="json")
    raw["version"] = "general-recall-release@2"
    for scenario in raw["scenarios"]:
        scenario["evidence_sources"] = {
            scenario["id"]: [{"source_id": "h1", "text_sha256": digest(scenario["id"])}]
        }
    for row in raw["observations"]:
        row["rendered_noise_items"] = 2
        row["global_rank_certifiable"] = True
        if row["channel"] == "hybrid":
            row.update(formed=[], candidates=[], ranked=[], rendered=[])
            row["recovered_sources"] = [
                {
                    "source_id": "h1",
                    "text_sha256": digest(row["scenario_id"]),
                    "session_id": "00000000-0000-0000-0000-000000000020",
                    "event_sequence": 7,
                    "belief_id": "00000000-0000-0000-0000-000000000030",
                    "anchor_rank": 1,
                    "rendered": True,
                    **source_changes,
                }
            ]
    raw["identity"]["scenario_manifest_sha256"] = hashlib.sha256(
        b"".join(
            RecallScenario.model_validate(s).model_dump_json().encode() + b"\n"
            for s in raw["scenarios"]
        )
    ).hexdigest()
    from agent_core.domain.general_recall_release import RecallReleaseIdentity

    identity = digest(RecallReleaseIdentity.model_validate(raw["identity"]).model_dump_json())
    for row in [*raw["observations"], *raw["gates"]]:
        row["identity_sha256"] = identity
    return RecallReleaseBundle.model_validate(raw)


def test_recovered_sources_support_answers_without_inventing_formed_atoms() -> None:
    report = score_release(recovered_bundle())
    cell = next(c for c in report["cells"] if c["channel"] == "hybrid")
    assert cell["formed_recall"] == 0
    assert cell["atomic_ranked_recall_at_20"] == 0
    assert cell["ranked_recall_at_20"] == cell["rendered_recall"] == 1
    assert cell["source_rendered_recall"] == 1
    assert cell["answer_success"] == 1
    assert cell["noise_items"] == 2 * cell["cases"]


@pytest.mark.parametrize(
    "change",
    [
        {"source_id": "not-the-source"},
        {"text_sha256": "f" * 64},
        {"anchor_rank": 21},
        {"anchor_rank": None},
        {"rendered": False},
    ],
)
def test_wrong_source_or_missing_rank_or_render_cannot_pass(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        score_release(recovered_bundle(**change))


def test_source_observations_cannot_silently_change_the_version_one_scorer() -> None:
    with pytest.raises(ValueError, match="version one"):
        score_release(recovered_bundle().model_copy(update={"version": "general-recall-release@1"}))


def test_uncertifiable_combined_order_cannot_supply_a_release_receipt() -> None:
    bundle = recovered_bundle()
    ambiguous = bundle.observations[0].model_copy(update={"global_rank_certifiable": False})
    with pytest.raises(ValueError, match="global ranking"):
        score_release(
            bundle.model_copy(update={"observations": (ambiguous, *bundle.observations[1:])})
        )
