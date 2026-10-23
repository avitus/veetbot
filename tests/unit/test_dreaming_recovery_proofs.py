"""Identifier proofs never replace missing evidence with an assumed empty footprint."""

import hashlib
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid5

import pytest

from agent_core.domain.dreaming import ForwardDreamingBoundary
from agent_core.domain.dreaming_recovery import (
    LegacyAttributionManifest,
    LegacyAttributionProof,
    RecoveryEvidence,
    RecoveryHead,
    validate_recovery,
)
from agent_core.domain.errors import ConflictError
from agent_core.domain.people_sources import source_id
from agent_core.domain.reconsolidation_attribution import AttributionFootprint
from tests.contract.support import NOW, SESSION_ID, principal


def fixture() -> tuple[LegacyAttributionManifest, ForwardDreamingBoundary, RecoveryEvidence]:
    person, belief = UUID(int=11), UUID(int=12)
    source = source_id(principal(), SESSION_ID, 10)
    proofs = (
        LegacyAttributionProof(record_id=person, revision=1, kind="person"),
        LegacyAttributionProof(
            record_id=uuid5(belief, f"{person}:subject"),
            revision=1,
            kind="memory_link",
            belief_id=belief,
            person_id=person,
            role="subject",
        ),
        LegacyAttributionProof(
            record_id=source, revision=1, kind="source", session_id=SESSION_ID, event_sequence=10
        ),
        LegacyAttributionProof(
            record_id=uuid5(source, "mention:0:5"),
            revision=1,
            kind="mention",
            source_id=source,
            start=0,
            end=5,
        ),
    )
    heads = {p.record_id: RecoveryHead(p.kind, p.revision, True, None) for p in proofs}
    digest = hashlib.sha256(
        "\n".join(f"{k}:{h.revision}:{h.kind}" for k, h in sorted(heads.items())).encode()
    ).hexdigest()
    manifest = LegacyAttributionManifest(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        legacy_digest=digest,
        proofs=proofs,
    )
    boundary = ForwardDreamingBoundary(
        activated_at=NOW,
        event_id=10,
        creation_sequence=12,
        change_sequence=12,
        legacy_digest=digest,
    )
    evidence = RecoveryEvidence(
        heads, frozenset({belief}), {source: frozenset({NOW})}, {SESSION_ID: NOW}
    )
    return manifest, boundary, evidence


def test_exact_metadata_proofs_recover_every_footprint() -> None:
    manifest, boundary, evidence = fixture()
    result = validate_recovery(principal(), manifest, boundary, evidence)
    assert len(result) == 4
    assert result[manifest.proofs[0].record_id] == AttributionFootprint()
    assert result[manifest.proofs[1].record_id].belief_id == UUID(int=12)
    assert result[manifest.proofs[2].record_id].event_sequence == 10
    assert result[manifest.proofs[3].record_id].source_id == manifest.proofs[2].record_id


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "duplicate",
        "extra",
        "foreign",
        "digest",
        "live",
        "payload",
        "revision",
        "kind",
        "belief",
        "person",
        "role",
        "source_event",
        "source_session",
        "receipt",
        "receipt_time",
        "tombstone",
        "mention_source",
        "span",
        "empty_keys",
    ],
)
def test_unproved_recovery_fails_closed(failure: str) -> None:
    manifest, boundary, evidence = fixture()
    proofs = list(manifest.proofs)
    heads = dict(evidence.heads)
    if failure == "missing":
        proofs.pop()
    elif failure == "duplicate":
        proofs.append(proofs[0])
    elif failure == "extra":
        proofs.append(proofs[0].model_copy(update={"record_id": UUID(int=999)}))
    elif failure == "foreign":
        manifest = manifest.model_copy(update={"principal_id": "other"})
    elif failure == "digest":
        manifest = manifest.model_copy(update={"legacy_digest": "0" * 64})
    elif failure in {"live", "payload"}:
        head = heads[proofs[0].record_id]
        heads[proofs[0].record_id] = (
            replace(head, erased=False) if failure == "live" else replace(head, has_payload=True)
        )
    elif failure == "revision":
        proofs[1] = proofs[1].model_copy(update={"revision": 2})
    elif failure == "kind":
        proofs[1] = proofs[1].model_copy(update={"kind": "person"})
    elif failure == "belief":
        evidence = replace(evidence, beliefs=frozenset())
    elif failure == "person":
        proofs[1] = proofs[1].model_copy(update={"person_id": UUID(int=999)})
    elif failure == "role":
        proofs[1] = proofs[1].model_copy(update={"role": "object"})
    elif failure == "source_event":
        proofs[2] = proofs[2].model_copy(update={"event_sequence": 11})
    elif failure == "source_session":
        proofs[2] = proofs[2].model_copy(update={"session_id": UUID(int=888)})
    elif failure == "receipt":
        evidence = replace(evidence, source_cleanup={})
    elif failure == "receipt_time":
        evidence = replace(evidence, deleted_sessions={SESSION_ID: NOW + timedelta(seconds=1)})
    elif failure == "tombstone":
        evidence = replace(evidence, deleted_sessions={})
    elif failure == "mention_source":
        proofs[3] = proofs[3].model_copy(update={"source_id": UUID(int=888)})
    elif failure == "span":
        proofs[3] = proofs[3].model_copy(update={"end": 6})
    elif failure == "empty_keys":
        proofs[0] = proofs[0].model_copy(update={"belief_id": UUID(int=12)})
    with pytest.raises(ConflictError):
        validate_recovery(
            principal(),
            manifest.model_copy(update={"proofs": tuple(proofs)}),
            boundary,
            replace(evidence, heads=heads),
        )


def test_later_attribution_revision_refuses_even_with_a_matching_cutover() -> None:
    manifest, boundary, evidence = fixture()
    proof = manifest.proofs[1].model_copy(update={"revision": 2})
    evidence.heads[proof.record_id] = replace(evidence.heads[proof.record_id], revision=2)
    digest = hashlib.sha256(
        "\n".join(f"{k}:{h.revision}:{h.kind}" for k, h in sorted(evidence.heads.items())).encode()
    ).hexdigest()
    manifest = manifest.model_copy(
        update={
            "legacy_digest": digest,
            "proofs": (manifest.proofs[0], proof, *manifest.proofs[2:]),
        }
    )
    with pytest.raises(ConflictError, match="Later revisions"):
        validate_recovery(
            principal(), manifest, boundary.model_copy(update={"legacy_digest": digest}), evidence
        )


def test_retry_is_bound_to_installed_metadata_and_rejects_new_uncertainty() -> None:
    manifest, boundary, evidence = fixture()
    recovered = validate_recovery(principal(), manifest, boundary, evidence)
    boundary = boundary.model_copy(
        update={"recovered_at": NOW, "recovery_digest": manifest.digest()}
    )
    evidence = replace(
        evidence, heads={k: replace(h, attribution=recovered[k]) for k, h in evidence.heads.items()}
    )
    assert validate_recovery(principal(), manifest, boundary, evidence) == recovered
    key = manifest.proofs[1].record_id
    evidence.heads[key] = replace(evidence.heads[key], attribution=AttributionFootprint())
    with pytest.raises(ConflictError, match="Installed"):
        validate_recovery(principal(), manifest, boundary, evidence)
    evidence.heads[key] = replace(evidence.heads[key], attribution=None)
    with pytest.raises(ConflictError, match="no longer complete"):
        validate_recovery(principal(), manifest, boundary, evidence)
