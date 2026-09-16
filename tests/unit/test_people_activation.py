"""Changed People extraction activates only on its own passing evidence tuple."""

from pathlib import Path

import pytest

from agent_core.config import MemoryFormationPolicyPin


def test_activation_digest_includes_import_budget_and_retrieval_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    from agent_core.memory.people_evidence import implementation_digest

    original = Path.read_bytes
    before = implementation_digest()

    def changed(path: Path) -> bytes:
        content = original(path)
        if path.name == "people_imports.py" and path.parent.name == "runtime":
            return content + b"\n# changed budget enforcement\n"
        return content

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert implementation_digest() != before


def test_people_policy_pin_is_distinct_from_frozen_formation_policies() -> None:
    assert "formation@11" in {value.value for value in MemoryFormationPolicyPin}, (
        "People extraction needs an independently evidence-bound policy pin"
    )


async def test_people_evaluation_uses_new_policy_without_activating_legacy_evidence() -> None:
    from dataclasses import replace

    from agent_core.bootstrap import build
    from tests.integration.m2_support import memory_settings

    async with build(
        settings=replace(memory_settings(), people_enabled=True),
        storage="memory",
        memory_people_evaluation_mode=True,
    ) as app:
        assert app.memory._policy_version == "formation@11"
        assert app.memory.extractor_name.startswith("people-assisted-v1")
        async with app.uow_factory() as uow:
            events = await uow.process_events.list("memory.provider_extraction.selection")
        assert events[0].payload["outcome"] == "evaluation"
        assert events[0].payload["reason"] == "explicit_people_evaluation_mode"


def test_people_evidence_cannot_reuse_legacy_distillation_artifact() -> None:
    from pathlib import Path

    import agent_core.config as config

    loader = getattr(config, "load_people_formation_evidence", None)
    assert loader is not None, "People activation must have a distinct evidence validator"
    artifact = Path(
        "src/agent_core/memory/release_evidence/openai-balanced-gpt-5.6-sol-default-formation9.json"
    )
    with pytest.raises(config.ConfigurationError):
        loader(artifact)


async def test_pinned_people_does_not_activate_matching_legacy_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    import json
    from dataclasses import replace
    from pathlib import Path

    import agent_core.config as config
    from agent_core.bootstrap import build
    from tests.gates.test_memory_m21 import _runtime_policy_version
    from tests.integration.m2_support import memory_settings

    root = Path(str(tmp_path))
    artifact = json.loads(
        Path(
            "src/agent_core/memory/release_evidence/openai-balanced-gpt-5.6-sol-default-formation9.json"
        ).read_text()
    )
    artifact.update(
        provider="fake",
        model="scripted",
        model_policy="fake-balanced",
        policy_version=_runtime_policy_version(),
    )
    (root / "legacy.json").write_text(json.dumps(artifact))
    monkeypatch.setattr(config, "PROVIDER_EXTRACTION_RELEASE_EVIDENCE_ROOT", root)
    settings = replace(
        memory_settings(),
        people_enabled=True,
        memory_provider_extraction_mode=config.MemoryProviderExtractionMode.AUTO,
        memory_formation_policy_pin=config.MemoryFormationPolicyPin.PEOPLE,
        artifact_root=root / "artifacts",
    )
    async with build(settings=settings, storage="memory") as app, app.uow_factory() as uow:
        selections = await uow.process_events.list("memory.provider_extraction.selection")
        assert selections[0].payload["outcome"] == "deterministic_fallback"
        assert selections[0].payload["reason"] == "pinned_policy_unevidenced"


def test_people_activation_rejects_unmeasured_reasoning_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.domain.messages import ResolvedModel
    from agent_core.domain.people_evidence import PeopleFormationEvidence
    from agent_core.memory import people_evidence

    monkeypatch.setattr(people_evidence, "schema_digest", lambda: "1" * 64)
    monkeypatch.setattr(people_evidence, "implementation_digest", lambda: "2" * 64)
    monkeypatch.setattr(people_evidence, "reviewed_corpora", lambda: True, raising=False)
    evidence = PeopleFormationEvidence.model_construct(
        provider="fake",
        model="scripted",
        model_policy="fake-policy",
        policy_profile="default",
        policy_version="policy@1",
        schema_sha256="1" * 64,
        implementation_sha256="2" * 64,
        corpus_sha256="3" * 64,
        holdout_sha256="4" * 64,
        ordinary_corpus_sha256="5" * 64,
        ordinary_holdout_sha256="6" * 64,
        reasoning_configuration="custom",
    )
    model = ResolvedModel.model_construct(
        provider="fake", model="scripted", policy_name="fake-policy"
    )
    arguments = {
        "corpus_sha256": "3" * 64,
        "holdout_sha256": "4" * 64,
        "ordinary_corpus_sha256": "5" * 64,
        "ordinary_holdout_sha256": "6" * 64,
    }
    assert not people_evidence.people_evidence_matches(
        evidence, model, "default", "policy@1", **arguments
    )
    assert people_evidence.people_evidence_matches(
        evidence.model_copy(update={"reasoning_configuration": "provider-default"}),
        model,
        "default",
        "policy@1",
        **arguments,
    )
    monkeypatch.setattr(people_evidence, "reviewed_corpora", lambda: False, raising=False)
    assert not people_evidence.people_evidence_matches(
        evidence.model_copy(update={"reasoning_configuration": "provider-default"}),
        model,
        "default",
        "policy@1",
        **arguments,
    )


@pytest.mark.parametrize("review_status", ["unreviewed", "reviewed", None])
def test_activation_requires_both_shipped_corpora_to_be_reviewed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, review_status: str | None
) -> None:
    import json

    import agent_core.config as config
    from agent_core.memory import people_evidence

    monkeypatch.setattr(config, "REPOSITORY_ROOT", tmp_path)
    # Deployment must not depend on the process's working directory.
    monkeypatch.chdir(tmp_path.parent)
    for relative in (people_evidence.PEOPLE_CORPUS_PATH, people_evidence.PEOPLE_HOLDOUT_PATH):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"review_status": "reviewed"}))
    (tmp_path / people_evidence.PEOPLE_HOLDOUT_PATH).write_text(
        json.dumps({"review_status": review_status})
    )
    assert people_evidence.reviewed_corpora() is (review_status == "reviewed")


async def test_malformed_email_people_evidence_fails_as_configuration_error(tmp_path: Path) -> None:
    from dataclasses import replace

    from agent_core.bootstrap import build
    from agent_core.config import ConfigurationError
    from tests.integration.m2_support import memory_settings

    artifact = tmp_path / "bad-email-evidence.json"
    artifact.write_text("{invalid")
    with pytest.raises(ConfigurationError, match=r"email.*evidence"):
        async with build(
            settings=replace(
                memory_settings(), people_enabled=True, email_semantic_evidence=artifact
            ),
            storage="memory",
        ):
            pass


def test_email_activation_rejects_stale_nested_corpora(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hashlib

    from agent_core.config import (
        MEMORY_DISTILLATION_CORPUS_PATH,
        MEMORY_DISTILLATION_HOLDOUT_PATH,
        ConfigurationError,
    )
    from agent_core.domain.email_people_evidence import EmailPeopleEvidence
    from agent_core.domain.people_evidence import PeopleFormationEvidence
    from agent_core.memory import email_people_evidence as loader
    from agent_core.memory.people_evidence import PEOPLE_CORPUS_PATH, PEOPLE_HOLDOUT_PATH

    corpus = hashlib.sha256(PEOPLE_CORPUS_PATH.read_bytes()).hexdigest()
    holdout = hashlib.sha256(PEOPLE_HOLDOUT_PATH.read_bytes()).hexdigest()
    ordinary = hashlib.sha256(MEMORY_DISTILLATION_CORPUS_PATH.read_bytes()).hexdigest()
    ordinary_holdout = hashlib.sha256(MEMORY_DISTILLATION_HOLDOUT_PATH.read_bytes()).hexdigest()
    nested = PeopleFormationEvidence.model_construct(
        model_policy="test",
        policy_profile="test",
        policy_version="test",
        reasoning_configuration="provider-default",
        corpus_sha256=corpus,
        holdout_sha256=holdout,
        ordinary_corpus_sha256=ordinary,
        ordinary_holdout_sha256=ordinary_holdout,
    )
    evidence = EmailPeopleEvidence.model_construct(
        build_ref="b" * 40,
        corpus_sha256=hashlib.sha256(
            Path("evals/capability/email-people.v1.json").read_bytes()
        ).hexdigest(),
        holdout_sha256=hashlib.sha256(
            Path("evals/capability/email-people.v1-holdout.json").read_bytes()
        ).hexdigest(),
        people=nested,
    )
    monkeypatch.setattr(loader, "matches_email_people", lambda *args, **kwargs: True)
    monkeypatch.setattr(EmailPeopleEvidence, "model_validate_json", lambda _: evidence)
    artifact = tmp_path / "fixture.json"
    artifact.write_text("{}")
    assert (
        loader.load_email_people_evidence(
            artifact,
            provider="test",
            model="test",
            build_ref="b" * 40,
            model_policy="test",
            policy_profile="test",
            policy_version="test",
        )
        is evidence
    )
    for field in [
        "corpus_sha256",
        "holdout_sha256",
        "ordinary_corpus_sha256",
        "ordinary_holdout_sha256",
    ]:
        evidence = evidence.model_copy(
            update={
                "people": nested.model_copy(
                    update={
                        "corpus_sha256": corpus,
                        "holdout_sha256": holdout,
                        "ordinary_corpus_sha256": ordinary,
                        "ordinary_holdout_sha256": ordinary_holdout,
                        field: "0" * 64,
                    }
                )
            }
        )
        with pytest.raises(ConfigurationError):
            loader.load_email_people_evidence(
                artifact,
                provider="test",
                model="test",
                build_ref="b" * 40,
                model_policy="test",
                policy_profile="test",
                policy_version="test",
            )
