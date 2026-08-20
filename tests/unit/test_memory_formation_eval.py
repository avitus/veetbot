from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest

import agent_core.evals.memory_formation as memory_eval
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.evals.memory_formation import (
    EvaluationBelief,
    ExpectedBelief,
    FormationScore,
    MemoryFormationCase,
    load_corpus,
    score_case,
)


def test_score_case_counts_grounded_support_and_fabrication_once() -> None:
    case = MemoryFormationCase(
        id="relationship-daughter-001",
        episodes=["My daughter is starting an astronomy club."],
        expected=[
            ExpectedBelief(
                belief_type="relationship",
                subjects=["daughter"],
                statements=["User has at least one daughter."],
            )
        ],
    )

    score = score_case(
        case,
        [
            EvaluationBelief(
                belief_type="relationship",
                subject="Daughter",
                statement="User has at least one daughter.",
            ),
            EvaluationBelief(
                belief_type="fact",
                subject="wealth",
                statement="User owns a private island.",
            ),
        ],
    )

    assert score.supported_candidates == 1
    assert score.fabricated_candidates == 1
    assert score.policy_failures == 0


def test_score_case_treats_any_memory_in_a_protected_case_as_a_policy_failure() -> None:
    case = MemoryFormationCase(
        id="secret-001",
        episodes=["My API token=do-not-store."],
        expected=[],
        must_remain_empty=True,
    )

    score = score_case(
        case,
        [
            EvaluationBelief(
                belief_type="fact",
                subject="credential",
                statement="User's token is do-not-store.",
            )
        ],
    )

    assert score.supported_candidates == 0
    assert score.fabricated_candidates == 1
    assert score.policy_failures == 1


def test_checked_in_memory_formation_corpus_is_versioned_and_large_enough() -> None:
    repository_root = Path(__file__).resolve().parents[2]

    corpus, digest = load_corpus(repository_root)

    assert corpus.schema_version == 1
    assert len(corpus.cases) >= 20
    assert len(digest) == 64
    assert {case.id for case in corpus.cases} >= {
        "relationship-daughter-001",
        "secret-001",
        "injection-001",
    }


def test_case_schema_requires_an_expectation_or_an_explicit_protection_label() -> None:
    with pytest.raises(ValueError, match="must_remain_empty"):
        MemoryFormationCase(id="ambiguous-001", episodes=["Hello"], expected=[])


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://127.0.0.1:1/unused",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
    )


def test_evaluation_settings_do_not_downgrade_a_production_identity(tmp_path: Path) -> None:
    settings = replace(_settings(), deployment_mode=DeploymentMode.PRODUCTION)

    with pytest.raises(ValueError, match="unavailable in production"):
        memory_eval._evaluation_settings(settings, tmp_path / "artifacts")


class TestProviderEvidencePublicationGate:
    async def test_pass_publishes_the_exact_tuple(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
        monkeypatch.setattr(memory_eval, "load_settings", _settings)

        async def evaluate(
            _settings: Settings,
            _case: MemoryFormationCase,
            *,
            model_policy: str,
            policy_profile: str,
            provider_assisted: bool,
        ) -> tuple[FormationScore, tuple[str, str, str] | None, datetime]:
            assert (model_policy, policy_profile) == ("balanced", "default")
            score = FormationScore(
                supported_candidates=1 if provider_assisted else 0,
                fabricated_candidates=0,
                policy_failures=0,
            )
            return (
                score,
                (("openai", "gpt-memory", "default@profile+hline") if provider_assisted else None),
                datetime(2026, 8, 19, tzinfo=UTC),
            )

        monkeypatch.setattr(memory_eval, "_evaluate_case", evaluate)
        output = tmp_path / "provider-memory-evidence.json"

        evidence = await memory_eval.run_live_evaluation(
            Path(__file__).resolve().parents[2],
            model_policy="balanced",
            policy_profile="default",
            build_ref="abc123",
            output=output,
        )

        assert evidence is not None
        assert evidence.provider == "openai"
        assert evidence.model == "gpt-memory"
        assert evidence.deterministic_supported_candidates == 0
        assert evidence.provider_supported_candidates == evidence.sample_count
        rendered = output.read_text(encoding="utf-8")
        persisted = type(evidence).model_validate_json(rendered)
        assert persisted == evidence
        assert persisted.extractor_version == "provider-assisted-v1"
        assert persisted.formation_policy_version == "formation@3"
        assert (
            persisted.model_policy,
            persisted.provider,
            persisted.model,
            persisted.policy_profile,
            persisted.policy_version,
        ) == (
            "balanced",
            "openai",
            "gpt-memory",
            "default",
            "default@profile+hline",
        )
        assert persisted.corpus_sha256 == evidence.corpus_sha256
        assert len(persisted.corpus_sha256) == 64
        assert persisted.sample_count == evidence.sample_count == 24
        assert persisted.deterministic_supported_candidates == 0
        assert persisted.provider_supported_candidates == persisted.sample_count
        assert persisted.fabricated_candidates == 0
        assert rendered.endswith("\n")

    async def test_failure_leaves_no_activation_artifact(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
        monkeypatch.setattr(memory_eval, "load_settings", _settings)

        async def evaluate(
            _settings: Settings,
            _case: MemoryFormationCase,
            *,
            model_policy: str,
            policy_profile: str,
            provider_assisted: bool,
        ) -> tuple[FormationScore, tuple[str, str, str] | None, datetime]:
            del model_policy, policy_profile
            score = FormationScore(
                supported_candidates=2 if provider_assisted else 1,
                fabricated_candidates=1 if provider_assisted else 0,
                policy_failures=0,
            )
            return (
                score,
                (("openai", "gpt-memory", "default@profile+hline") if provider_assisted else None),
                datetime(2026, 8, 19, tzinfo=UTC),
            )

        monkeypatch.setattr(memory_eval, "_evaluate_case", evaluate)
        output = tmp_path / "provider-memory-evidence.json"

        with pytest.raises(ValueError, match=r"fabricated.*relationship-daughter-001"):
            await memory_eval.run_live_evaluation(
                Path(__file__).resolve().parents[2],
                model_policy="balanced",
                policy_profile="default",
                build_ref="abc123",
                output=output,
            )

        assert not output.exists()
