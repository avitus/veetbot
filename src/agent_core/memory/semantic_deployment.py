"""Release-bound source indexing and optional offline run-query preparation."""

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.config import ConfigurationError, DeploymentMode, Settings, load_config_document
from agent_core.domain.agents import Principal
from agent_core.domain.credentials import CredentialRef
from agent_core.domain.general_recall_release import RecallReleaseIdentity
from agent_core.domain.local_embeddings import LocalEmbeddingConfig, local_execution
from agent_core.domain.semantic_work import EmbeddingConfig
from agent_core.memory.bounded_recall_admission import BoundedAdmission, bind_bounded_recall
from agent_core.memory.general_recall_evidence import (
    _read_bounded,
    _unique_object,
    bind_semantic_evidence,
)
from agent_core.memory.local_query_authority import LocalQueryAuthority
from agent_core.memory.recall_release_binding import (
    RecallDeploymentBinding,
    capture_inputs,
    input_stamps,
)
from agent_core.memory.reconsolidation_policy import POLICY_VERSION
from agent_core.memory.semantic_indexing import SemanticIndexingPass
from agent_core.memory.semantic_runtime import RunSemanticQueries
from agent_core.memory.source_recovery import BoundedSourceRecovery
from agent_core.ports.credentials import CredentialResolver
from agent_core.ports.determinism import Clock
from agent_core.ports.embeddings import EmbeddingProvider, LocalEmbeddingProvider
from agent_core.ports.persistence import UnitOfWorkFactory

DOCUMENT = "memory/semantic-index.yaml"


class IndexConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    credential_ref: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,127}$")
    embedding: EmbeddingConfig
    local: LocalEmbeddingConfig | None = None
    model_directory: Path | None = None
    reader_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def execution_tuple(self) -> Self:
        if self.local is None:
            if (
                self.credential_ref is None
                or self.model_directory is not None
                or self.reader_sha256 is not None
                or self.embedding.historical_indexing
            ):
                raise ValueError(
                    "Remote embedding configuration needs only its provider credential"
                )
        elif (
            self.credential_ref is not None
            or self.model_directory is None
            or not self.model_directory.is_absolute()
            or not local_execution(self.embedding, self.local)
        ):
            raise ValueError("Invalid local embedding configuration")
        return self


@dataclass(frozen=True)
class SemanticServices:
    index: SemanticIndexingPass
    queries: RunSemanticQueries | None = None
    local_provider: LocalEmbeddingProvider | None = None
    source_recovery: BoundedSourceRecovery | None = None
    admitted: Callable[[], bool] | None = None

    async def close(self) -> None:
        if self.local_provider is not None:
            await self.local_provider.close()


@dataclass(frozen=True)
class SemanticCompositionInputs:
    """Effective values resolved by bootstrap, before semantic evidence admission."""

    configuration: dict[str, Any]
    formation: dict[str, Any]
    reader: dict[str, Any]

    def dependencies(self, profile: IndexConfiguration) -> dict[str, str]:
        return active_index_dependencies(
            profile,
            configuration=self.configuration,
            formation=self.formation,
            reader=self.reader,
        )


class IndexProfiles(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1]
    configurations: tuple[IndexConfiguration, ...] = Field(max_length=1)


def configuration_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def active_index_dependencies(
    profile: IndexConfiguration,
    *,
    configuration: Mapping[str, Any],
    formation: Mapping[str, Any],
    reader: Mapping[str, Any],
) -> dict[str, str]:
    """Hash active composition values, not claims read from the quality bundle."""
    return {
        "configuration_sha256": configuration_digest(
            {
                "version": "semantic-index-composition@1",
                "configuration": configuration,
                "indexing": profile.model_dump(mode="json"),
            }
        ),
        "embedding_configuration_sha256": profile.embedding.key,
        "formation_configuration_sha256": configuration_digest(formation),
        "reader_configuration_sha256": configuration_digest(reader),
    }


def bind_index_manifest(
    root: Path,
    manifest: Path,
    evidence: Path,
    release_id: str,
    dependencies: Mapping[str, str],
    *,
    principal: Principal | None = None,
    clock: Clock | None = None,
) -> Callable[[], bool]:
    """Both independently reviewed manifest and measured evidence must remain bound."""
    required = {
        "configuration_sha256",
        "embedding_configuration_sha256",
        "formation_configuration_sha256",
        "reader_configuration_sha256",
    }
    raw = _read_bounded(manifest)
    document = json.loads(raw, object_pairs_hook=_unique_object)
    if isinstance(document, dict) and document.get("version") in (
        "recall-bounded-admission@1",
        "recall-bounded-admission@2",
        "recall-bounded-admission@3",
    ):
        if principal is None or clock is None:
            raise ValueError("Bounded recall requires actual owner and clock")
        return bind_bounded_recall(
            root, manifest, evidence, release_id, dependencies, principal=principal, clock=clock
        )
    binding = (
        RecallDeploymentBinding.model_validate(document)
        if isinstance(document, dict) and "version" in document
        else None
    )
    expected = binding.identity if binding else RecallReleaseIdentity.model_validate(document)
    revision = release_id.rsplit("-", 1)[-1]
    deployed_ref = binding.deployment_build_ref if binding else expected.build_ref
    if (
        set(dependencies) != required
        or re.fullmatch(r"[a-f0-9]{7,40}", revision) is None
        or not deployed_ref.startswith(revision)
        or any(getattr(expected, key) != value for key, value in dependencies.items())
    ):
        raise ValueError("Semantic index active dependency mismatch")
    stamps = None
    if binding is not None or expected.evaluated_inputs_sha256 is not None:
        stamps = input_stamps(root)
        inputs = capture_inputs(root, expected.model_dump(mode="json"))
        if (
            expected.evaluated_inputs_sha256 != inputs.digest
            or (
                binding is not None
                and (
                    binding.inputs != inputs
                    or binding.evidence_sha256
                    != hashlib.sha256(_read_bounded(evidence)).hexdigest()
                )
            )
            or input_stamps(root) != stamps
        ):
            raise ValueError("Semantic index evaluated input mismatch")
    evidence_admitted = bind_semantic_evidence(root, evidence, expected)
    fingerprint = hashlib.sha256(raw).digest()
    revoked = False

    def admitted() -> bool:
        nonlocal revoked
        if not revoked:
            try:
                revoked = (
                    hashlib.sha256(_read_bounded(manifest)).digest() != fingerprint
                    or not evidence_admitted()
                    or (stamps is not None and input_stamps(root) != stamps)
                )
            except (OSError, ValueError):
                revoked = True
        return not revoked

    if not admitted():
        raise ValueError("Semantic index evidence changed during binding")
    return admitted


async def compose_indexing(
    *,
    root: Path,
    settings: Settings,
    principal: Principal,
    factory: UnitOfWorkFactory,
    clock: Clock,
    credentials: CredentialResolver,
    configuration: Mapping[str, Any],
    formation: Mapping[str, Any],
    reader: Mapping[str, Any],
    provider_factory: Callable[[str], EmbeddingProvider],
    local_provider_factory: Callable[[Path, LocalEmbeddingConfig], LocalEmbeddingProvider]
    | None = None,
) -> SemanticServices | None:
    if not settings.memory_semantic_index_enabled:
        return None
    if (
        settings.deployment_mode == DeploymentMode.PRODUCTION
        and configuration.get("storage") != "postgres"
    ):
        raise ConfigurationError("Semantic index production processing requires PostgreSQL")
    if (
        settings.memory_semantic_index_manifest is None
        or settings.memory_semantic_index_evidence is None
        or settings.release_id is None
    ):
        raise ConfigurationError("Semantic index needs a release, manifest and passing evidence")
    try:
        document = load_config_document(settings, DOCUMENT)
        # These are policy-semantic values; environment interpolation is not supported.
        if "${" in json.dumps(document):
            raise ValueError("Semantic index configuration cannot interpolate")
        profiles = IndexProfiles.model_validate(document)
        if len(profiles.configurations) != 1:
            raise ValueError("Semantic index needs one explicit configuration")
        profile = profiles.configurations[0]
        if profile.local is None and (
            profile.embedding.space.provider != "openai"
            or profile.embedding.residency_provider != "openai"
            or profile.embedding.egress_revision != POLICY_VERSION
        ):
            raise ValueError("Semantic index provider or egress policy is unavailable")
        query_enabled = (
            configuration.get("memory", {}).get("retrieval", {}).get("semantic_enabled") is True
        )
        source_enabled = (
            configuration.get("memory", {}).get("retrieval", {}).get("source_recovery_enabled")
            is True
        )
        if source_enabled and (not query_enabled or profile.local is None):
            raise ValueError("Source recovery requires local runtime reader authority")
        if profile.local is not None and (
            local_provider_factory is None or (query_enabled and profile.reader_sha256 is None)
        ):
            raise ValueError("Local semantic recall needs its adapter and reviewed reader")
        dependencies = active_index_dependencies(
            profile,
            configuration=configuration,
            formation=formation,
            reader=reader,
        )
        admitted = bind_index_manifest(
            root,
            settings.memory_semantic_index_manifest,
            settings.memory_semantic_index_evidence,
            settings.release_id,
            dependencies,
            principal=principal,
            clock=clock,
        )
        if isinstance(admitted, BoundedAdmission) and (
            profile.local is None or not query_enabled or settings.memory_reconsolidation_enabled
        ):
            raise ValueError(
                "Bounded recall requires local interactive recall without unattended application"
            )
    except (OSError, ValueError, ConfigurationError):
        raise ConfigurationError(
            "Semantic index configuration or release evidence is invalid"
        ) from None
    local_provider = None
    queries = None
    provider: EmbeddingProvider
    if profile.local is not None:
        assert local_provider_factory is not None and profile.model_directory is not None
        try:
            local_provider = local_provider_factory(profile.model_directory, profile.local)
        except Exception:
            raise ConfigurationError("Semantic index local model is unavailable") from None
        provider = local_provider
        if query_enabled:
            assert profile.reader_sha256 is not None
            queries = RunSemanticQueries(
                factory,
                clock,
                principal,
                provider,
                profile.embedding,
                admitted=admitted,
                local=profile.local,
                authority=LocalQueryAuthority(
                    factory,
                    principal,
                    policy_version=str(configuration["policy_version"]),
                    retrieval_policy=configuration_digest(configuration),
                    reader_sha256=profile.reader_sha256,
                    interactive_only=isinstance(admitted, BoundedAdmission),
                ),
            )
    else:
        try:
            assert profile.credential_ref is not None
            secret = await credentials.resolve(CredentialRef(profile.credential_ref))
            if not secret.reveal():
                raise PermissionError
        except PermissionError:
            raise ConfigurationError("Semantic index credential is unavailable") from None
        provider = provider_factory(secret.reveal())
    # No provider object, model loading or credential resolution before admission.
    return SemanticServices(
        admitted=admitted,
        index=SemanticIndexingPass(
            factory,
            clock,
            principal,
            provider,
            profile.embedding,
            admitted=admitted,
            local=profile.local,
        ),
        queries=queries,
        local_provider=local_provider,
        source_recovery=BoundedSourceRecovery(principal, admitted=admitted)
        if source_enabled
        else None,
    )
