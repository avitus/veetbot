"""Trusted composition of optional source indexing; never query egress authority."""

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_core.config import ConfigurationError, DeploymentMode, Settings, load_config_document
from agent_core.domain.agents import Principal
from agent_core.domain.credentials import CredentialRef
from agent_core.domain.general_recall_release import RecallReleaseIdentity
from agent_core.domain.semantic_work import EmbeddingConfig
from agent_core.memory.general_recall_evidence import (
    _read_bounded,
    _unique_object,
    bind_semantic_evidence,
)
from agent_core.memory.reconsolidation_policy import POLICY_VERSION
from agent_core.memory.semantic_indexing import SemanticIndexingPass
from agent_core.ports.credentials import CredentialResolver
from agent_core.ports.determinism import Clock
from agent_core.ports.embeddings import EmbeddingProvider
from agent_core.ports.persistence import UnitOfWorkFactory

DOCUMENT = "memory/semantic-index.yaml"


class IndexConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    credential_ref: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,127}$")
    embedding: EmbeddingConfig


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
) -> Callable[[], bool]:
    """Both independently reviewed manifest and measured evidence must remain bound."""
    required = {
        "configuration_sha256",
        "embedding_configuration_sha256",
        "formation_configuration_sha256",
        "reader_configuration_sha256",
    }
    raw = _read_bounded(manifest)
    expected = RecallReleaseIdentity.model_validate(
        json.loads(raw, object_pairs_hook=_unique_object)
    )
    revision = release_id.rsplit("-", 1)[-1]
    if (
        set(dependencies) != required
        or re.fullmatch(r"[a-f0-9]{7,40}", revision) is None
        or not expected.build_ref.startswith(revision)
        or any(getattr(expected, key) != value for key, value in dependencies.items())
    ):
        raise ValueError("Semantic index active dependency mismatch")
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
) -> SemanticIndexingPass | None:
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
        if (
            profile.embedding.space.provider != "openai"
            or profile.embedding.residency_provider != "openai"
            or profile.embedding.egress_revision != POLICY_VERSION
        ):
            raise ValueError("Semantic index provider or egress policy is unavailable")
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
        )
    except (OSError, ValueError, ConfigurationError):
        raise ConfigurationError(
            "Semantic index configuration or release evidence is invalid"
        ) from None
    try:
        secret = await credentials.resolve(CredentialRef(profile.credential_ref))
        if not secret.reveal():
            raise PermissionError
    except PermissionError:
        raise ConfigurationError("Semantic index credential is unavailable") from None
    # No provider object or credential resolution before independent admission.
    return SemanticIndexingPass(
        factory,
        clock,
        principal,
        provider_factory(secret.reveal()),
        profile.embedding,
        admitted=admitted,
    )
