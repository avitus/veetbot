"""Selection excludes refused neighbors without granting provider authority."""

from uuid import UUID, uuid5

import pytest

from agent_core.domain.memory import Sensitivity
from agent_core.domain.people_sources import source_id
from agent_core.domain.reconsolidation import group_digest
from agent_core.memory.reconsolidation_policy import local_egress_policy
from agent_core.memory.reconsolidation_selection import fit_selection
from tests.contract.support import NOW, principal
from tests.unit.test_reconsolidation_inputs import resolved_model, selected
from tests.unit.test_reconsolidation_merge import source


@pytest.mark.parametrize("anchor_index", [0, 1, 2])
def test_memory_privacy_keeps_the_anchor_or_explicit_refusal(anchor_index: int) -> None:
    value = selected()
    refused = source(503, "User has a private passport record")
    refused = refused.model_copy(
        update={"record": refused.record.model_copy(update={"source_event_ids": [2]})}
    )
    event_id = source_id(principal(), refused.record.source_session_id, 2)
    private_excerpt = value.excerpts[0].model_copy(
        update={
            "id": uuid5(event_id, "reconsolidation-excerpt@1:0"),
            "event_id": event_id,
            "event_sequence": 2,
            "text": "User has a private passport record",
        }
    )
    value = value.model_copy(update={"excerpts": (*value.excerpts, private_excerpt)})
    sources = (*value.sources, refused)
    versions = tuple(s.version for s in sources)
    value = value.model_copy(
        update={
            "sources": sources,
            "group": value.group.model_copy(
                update={
                    "sources": versions,
                    "input_digest": group_digest(versions),
                }
            ),
        }
    )
    before = value.model_dump_json()
    result = fit_selection(
        principal(),
        value,
        versions[anchor_index],
        model=resolved_model(),
        egress_policy=local_egress_policy("fake"),
        now=NOW,
    )
    assert result == (versions if anchor_index == 2 else versions[:2])
    assert value.model_dump_json() == before


@pytest.mark.parametrize("residency", [None, "other-provider", "fake"])
def test_unknown_policy_or_shared_private_evidence_stays_explicit(residency: str | None) -> None:
    value = selected()
    value = value.model_copy(
        update={
            "excerpts": (value.excerpts[0].model_copy(update={"text": "My passport is private."}),),
        }
    )
    assert (
        fit_selection(
            principal(),
            value,
            value.group.sources[0],
            model=resolved_model(),
            egress_policy=local_egress_policy(residency),
            now=NOW,
        )
        == value.group.sources
    )


def test_unrecognized_anchor_keeps_the_original_refusal() -> None:
    value = selected()
    unknown = value.group.sources[0].model_copy(update={"belief_id": UUID(int=999)})
    assert (
        fit_selection(
            principal(),
            value,
            unknown,
            model=resolved_model(),
            egress_policy=local_egress_policy("fake"),
            now=NOW,
        )
        == value.group.sources
    )


@pytest.mark.parametrize("floor", [Sensitivity.SENSITIVE, Sensitivity.RESTRICTED])
@pytest.mark.parametrize("anchor_index", [0, 1, 2])
def test_refused_floor_cannot_be_dropped_from_shared_innocuous_original(
    floor: Sensitivity,
    anchor_index: int,
) -> None:
    value = selected()
    refused = source(503, "User prefers tea")
    refused = refused.model_copy(
        update={"record": refused.record.model_copy(update={"sensitivity": floor})}
    )
    sources = (*value.sources, refused)
    versions = tuple(item.version for item in sources)
    value = value.model_copy(
        update={
            "sources": sources,
            "group": value.group.model_copy(
                update={"sources": versions, "input_digest": group_digest(versions)}
            ),
        }
    )
    before = value.model_dump_json()
    assert (
        fit_selection(
            principal(),
            value,
            versions[anchor_index],
            model=resolved_model(),
            egress_policy=local_egress_policy("fake"),
            now=NOW,
        )
        == versions
    ), "A refused shared event must retain the explicit refusal, even with innocuous text"
    assert value.model_dump_json() == before
