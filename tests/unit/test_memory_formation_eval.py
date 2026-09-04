from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import ValidationError

import agent_core.evals.memory_formation as memory_eval
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.evals.memory_distillation import SeedBelief
from agent_core.evals.memory_formation import (
    EvaluationBelief,
    ExpectedBelief,
    FormationArmResult,
    FormationScore,
    MemoryFormationCase,
    MemoryFormationCorpus,
    ProviderExtractionDiagnostics,
    load_corpus,
    score_case,
)
from agent_core.memory.provider_extraction import (
    PROVIDER_FORMATION_POLICY_VERSION,
    REPAIRED_PROVIDER_FORMATION_POLICY_VERSION,
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
    assert len(corpus.cases) == 25
    assert len(digest) == 64
    assert {case.id for case in corpus.cases} >= {
        "attribute-saxophone-experience-and-pain-001",
        "relationship-daughter-001",
        "secret-001",
        "injection-001",
    }
    wife = next(case for case in corpus.cases if case.id == "relationship-wife-001")
    score = score_case(
        wife,
        [
            EvaluationBelief(
                belief_type="relationship",
                subject="wife",
                statement="User has a wife.",
            ),
            EvaluationBelief(
                belief_type="user_model_attr",
                subject="hobby",
                statement="User goes hiking.",
            ),
        ],
    )
    assert len(wife.expected) == 2
    assert score.supported_candidates == 2
    assert score.fabricated_candidates == 0


def test_provider_evaluation_guidance_tracks_current_corpus_cost() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    corpus, _digest = load_corpus(repository_root)
    expected_calls = len(corpus.cases)
    expected_ceiling = expected_calls * 0.05

    for relative_path in ("README.md", "evals/capability/README.md"):
        guidance = (repository_root / relative_path).read_text(encoding="utf-8")
        assert re.search(
            rf"{expected_calls}(?: bounded)? provider calls[^\n]*"
            rf"USD\s+{re.escape(f'{expected_ceiling:.2f}')}",
            guidance,
        )

    design = (repository_root / "docs/plan/memory-formation-and-consolidation.md").read_text(
        encoding="utf-8"
    )
    assert f"current {expected_calls}-case corpus" in design
    assert re.search(
        rf"reviewed\s+passing\s+`{re.escape(REPAIRED_PROVIDER_FORMATION_POLICY_VERSION)}`\s+evidence\s+"
        rf"from\s+the\s+checked-in\s+{expected_calls}-case\s+corpus",
        design,
    )


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
    @staticmethod
    def _arm(
        score: FormationScore,
        *,
        identity: tuple[str, str, str] | None = None,
        beliefs: list[EvaluationBelief] | None = None,
        candidate_count: int = 0,
        grounded_candidate_count: int = 0,
    ) -> FormationArmResult:
        return FormationArmResult(
            score=score,
            identity=identity,
            evaluated_at=datetime(2026, 8, 19, tzinfo=UTC),
            beliefs=[] if beliefs is None else beliefs,
            candidates_proposed=len(beliefs or []),
            committed=len(beliefs or []),
            reinforced=0,
            superseded=0,
            rejected=0,
            extraction=(
                None
                if identity is None
                else ProviderExtractionDiagnostics(
                    outcome="completed",
                    error_class=None,
                    candidate_count=candidate_count,
                    grounded_candidate_count=grounded_candidate_count,
                )
            ),
        )

    async def test_pass_publishes_the_exact_tuple(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
        monkeypatch.setattr(memory_eval, "load_settings", _settings)
        monkeypatch.setattr(memory_eval, "require_committed_tree", lambda _root, _ref: None)

        async def evaluate(
            _settings: Settings,
            _case: MemoryFormationCase,
            *,
            model_policy: str,
            policy_profile: str,
            provider_assisted: bool,
            formation_policy_version: str = PROVIDER_FORMATION_POLICY_VERSION,
            seeds: list[SeedBelief] | None = None,
        ) -> FormationArmResult:
            assert (model_policy, policy_profile) == ("balanced", "default")
            supported = len(_case.expected) if provider_assisted else 0
            score = FormationScore(
                supported_candidates=supported,
                fabricated_candidates=0,
                policy_failures=0,
            )
            return self._arm(
                score,
                identity=(
                    ("openai", "gpt-memory", "default@profile+hline") if provider_assisted else None
                ),
                candidate_count=supported,
                grounded_candidate_count=supported,
            )

        monkeypatch.setattr(memory_eval, "_evaluate_case", evaluate)
        output = tmp_path / "provider-memory-evidence.json"

        result = await memory_eval.run_live_evaluation(
            Path(__file__).resolve().parents[2],
            model_policy="balanced",
            policy_profile="default",
            build_ref="a" * 40,
            output=output,
        )

        assert result is not None
        assert result.passed
        assert result.failure_summary is None
        assert result.evidence is not None
        evidence = result.evidence
        assert evidence.provider == "openai"
        assert evidence.model == "gpt-memory"
        assert evidence.deterministic_supported_candidates == 0
        assert evidence.provider_supported_candidates == sum(
            len(case.expected) for case in load_corpus(Path(__file__).resolve().parents[2])[0].cases
        )
        rendered = output.read_text(encoding="utf-8")
        persisted = type(evidence).model_validate_json(rendered)
        assert persisted == evidence
        assert persisted.extractor_version == "provider-assisted-v2"
        assert persisted.formation_policy_version == PROVIDER_FORMATION_POLICY_VERSION
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
        assert persisted.sample_count == evidence.sample_count == 25
        assert persisted.deterministic_supported_candidates == 0
        assert persisted.provider_supported_candidates == evidence.provider_supported_candidates
        assert persisted.deterministic_fabricated_candidates == 0
        assert persisted.provider_fabricated_candidates == 0
        assert persisted.provider_supported_case_count == persisted.positive_case_count
        assert persisted.minimum_supported_case_count == 17
        assert rendered.endswith("\n")

    async def test_failure_returns_diagnostics_and_leaves_no_activation_artifact(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
        monkeypatch.setattr(memory_eval, "load_settings", _settings)
        monkeypatch.setattr(memory_eval, "require_committed_tree", lambda _root, _ref: None)

        async def evaluate(
            _settings: Settings,
            _case: MemoryFormationCase,
            *,
            model_policy: str,
            policy_profile: str,
            provider_assisted: bool,
            formation_policy_version: str = PROVIDER_FORMATION_POLICY_VERSION,
            seeds: list[SeedBelief] | None = None,
        ) -> FormationArmResult:
            del model_policy, policy_profile
            supported = not _case.must_remain_empty
            fabricated = _case.id == "relationship-daughter-001"
            beliefs = []
            if supported:
                beliefs.append(
                    EvaluationBelief(
                        belief_type=_case.expected[0].belief_type,
                        subject=_case.expected[0].subjects[0],
                        statement=_case.expected[0].statements[0],
                    )
                )
            if fabricated:
                beliefs.append(
                    EvaluationBelief(
                        belief_type="fact",
                        subject="astronomy activity",
                        statement="User's daughter is starting an astronomy club.",
                    )
                )
            score = FormationScore(
                supported_candidates=int(supported and provider_assisted),
                fabricated_candidates=int(fabricated),
                policy_failures=0,
            )
            return self._arm(
                score,
                identity=(
                    ("openai", "gpt-memory", "default@profile+hline") if provider_assisted else None
                ),
                beliefs=beliefs,
                candidate_count=len(beliefs),
                grounded_candidate_count=len(beliefs),
            )

        monkeypatch.setattr(memory_eval, "_evaluate_case", evaluate)
        output = tmp_path / "provider-memory-evidence.json"

        result = await memory_eval.run_live_evaluation(
            Path(__file__).resolve().parents[2],
            model_policy="balanced",
            policy_profile="default",
            build_ref="a" * 40,
            output=output,
        )

        assert result is not None
        assert not result.passed
        assert result.failure_summary is not None
        assert "fabricated" in result.failure_summary
        daughter = next(
            case for case in result.cases if case.case_id == "relationship-daughter-001"
        )
        assert daughter.deterministic.score.fabricated_candidates == 1
        assert daughter.provider.score.fabricated_candidates == 1
        assert daughter.provider.extraction is not None
        assert daughter.provider.extraction.candidate_count == 2
        assert daughter.provider.extraction.grounded_candidate_count == 2
        assert daughter.shared_beliefs == daughter.provider.beliefs
        assert daughter.provider_added_beliefs == []
        assert not output.exists()

    async def test_lift_without_minimum_positive_coverage_does_not_publish(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
        monkeypatch.setattr(memory_eval, "load_settings", _settings)
        monkeypatch.setattr(memory_eval, "require_committed_tree", lambda _root, _ref: None)

        async def evaluate(
            _settings: Settings,
            _case: MemoryFormationCase,
            *,
            model_policy: str,
            policy_profile: str,
            provider_assisted: bool,
            formation_policy_version: str = PROVIDER_FORMATION_POLICY_VERSION,
            seeds: list[SeedBelief] | None = None,
        ) -> FormationArmResult:
            del model_policy, policy_profile
            supported = provider_assisted and _case.id == "relationship-daughter-001"
            return self._arm(
                FormationScore(
                    supported_candidates=int(supported),
                    fabricated_candidates=0,
                    policy_failures=0,
                ),
                identity=(
                    ("openai", "gpt-memory", "default@profile+hline") if provider_assisted else None
                ),
                candidate_count=int(supported),
                grounded_candidate_count=int(supported),
            )

        monkeypatch.setattr(memory_eval, "_evaluate_case", evaluate)
        output = tmp_path / "provider-memory-evidence.json"

        result = await memory_eval.run_live_evaluation(
            Path(__file__).resolve().parents[2],
            model_policy="balanced",
            policy_profile="default",
            build_ref="a" * 40,
            output=output,
        )

        assert result is not None
        assert not result.passed
        assert result.failure_summary is not None
        assert "positive coverage 1/21" in result.failure_summary
        assert not output.exists()


def test_corpus_seed_pools_are_declared_large_and_cover_most_positive_cases() -> None:
    """The bundled formation@10 evidence must come from a populated store.

    A case may name only a declared pool, a pool holds at least twenty-five
    beliefs, and the checked-in corpus seeds at least eighty percent of its
    positive cases, since an empty store is exactly the blind spot that hid
    formation@8's starvation.
    """

    corpus, _digest = load_corpus(Path(__file__).resolve().parents[2])
    positives = [case for case in corpus.cases if case.expected]
    seeded = [case for case in positives if case.prior_beliefs_pool is not None]
    assert corpus.seed_pools
    assert all(len(pool) >= 25 for pool in corpus.seed_pools.values())
    assert len(seeded) >= 0.8 * len(positives)

    base = corpus.model_dump(mode="json")
    undeclared = {
        **base,
        "cases": [{**base["cases"][0], "prior_beliefs_pool": "missing"}, *base["cases"][1:]],
    }
    with pytest.raises(ValidationError, match="undeclared seed pool"):
        MemoryFormationCorpus.model_validate(undeclared)
    small = {**base, "seed_pools": {name: pool[:3] for name, pool in base["seed_pools"].items()}}
    with pytest.raises(ValidationError, match="at least 25"):
        MemoryFormationCorpus.model_validate(small)
    mostly_unseeded = {
        **base,
        "cases": [
            {**case, "prior_beliefs_pool": None} if index % 2 else case
            for index, case in enumerate(base["cases"])
        ],
    }
    with pytest.raises(ValidationError, match="eighty percent"):
        MemoryFormationCorpus.model_validate(mostly_unseeded)


class TestRepairedPolicyPublication:
    async def test_pass_publishes_formation10_with_its_seeded_case_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
        monkeypatch.setattr(memory_eval, "load_settings", _settings)
        monkeypatch.setattr(memory_eval, "require_committed_tree", lambda _root, _ref: None)
        repository_root = Path(__file__).resolve().parents[2]
        corpus, _digest = load_corpus(repository_root)
        seeded_ids = {case.id for case in corpus.cases if case.prior_beliefs_pool is not None}
        seen: dict[str, int] = {}

        async def evaluate(
            _settings: Settings,
            case: MemoryFormationCase,
            *,
            model_policy: str,
            policy_profile: str,
            provider_assisted: bool,
            formation_policy_version: str,
            seeds: list[SeedBelief],
        ) -> FormationArmResult:
            assert formation_policy_version == REPAIRED_PROVIDER_FORMATION_POLICY_VERSION
            # The exact configured pool, not merely something non-empty.
            assert seeds == corpus.seeds_for(case)
            assert bool(seeds) == (case.id in seeded_ids)
            if provider_assisted:
                seen[case.id] = len(seeds)
            supported = len(case.expected) if provider_assisted else 0
            return TestProviderEvidencePublicationGate._arm(
                FormationScore(
                    supported_candidates=supported,
                    fabricated_candidates=0,
                    policy_failures=0,
                ),
                identity=(
                    ("openai", "gpt-memory", "default@profile+hline") if provider_assisted else None
                ),
                candidate_count=supported,
                grounded_candidate_count=supported,
            )

        monkeypatch.setattr(memory_eval, "_evaluate_case", evaluate)
        output = tmp_path / "formation10.json"

        result = await memory_eval.run_live_evaluation(
            repository_root,
            model_policy="balanced",
            policy_profile="default",
            build_ref="a" * 40,
            output=output,
            formation_policy_version=REPAIRED_PROVIDER_FORMATION_POLICY_VERSION,
        )

        assert result is not None and result.passed and result.evidence is not None
        evidence = result.evidence
        assert evidence.extractor_version == "provider-assisted-v3"
        assert evidence.formation_policy_version == "formation@10"
        assert evidence.seeded_case_count == len(seeded_ids)
        assert evidence.seeded_case_count == sum(1 for count in seen.values() if count)
        persisted = type(evidence).model_validate_json(output.read_text(encoding="utf-8"))
        assert persisted == evidence


class TestCommittedTreeBinding:
    """The stubs above must never be the only thing between evidence and a real commit."""

    async def test_a_reference_that_is_not_the_checked_out_head_is_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
        monkeypatch.setattr(memory_eval, "load_settings", _settings)

        with pytest.raises(ValueError, match="checked-out HEAD"):
            await memory_eval.run_live_evaluation(
                Path(__file__).resolve().parents[2],
                model_policy="balanced",
                policy_profile="default",
                build_ref="a" * 40,
                output=tmp_path / "never-written.json",
                formation_policy_version=REPAIRED_PROVIDER_FORMATION_POLICY_VERSION,
            )
        assert not (tmp_path / "never-written.json").exists()

    async def test_a_label_is_not_a_build_reference(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("RUN_LIVE_MODEL_TESTS", "1")
        monkeypatch.setattr(memory_eval, "load_settings", _settings)

        with pytest.raises(ValueError, match="forty-character commit sha"):
            await memory_eval.run_live_evaluation(
                Path(__file__).resolve().parents[2],
                model_policy="balanced",
                policy_profile="default",
                build_ref="abc123",
                output=tmp_path / "never-written.json",
            )
