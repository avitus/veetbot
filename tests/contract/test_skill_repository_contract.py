"""Versioned skill repository contract."""

import pytest

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.skills.memory import InMemorySkillRepository
from agent_core.adapters.skills.stores import InMemorySkillPackageStore
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.domain.errors import ConflictError
from agent_core.domain.skills import SkillPackage, SkillPackageMember, SkillRef, SkillSource
from agent_core.skills.package import SkillPackageValidator
from tests.contract.support import NOW


def _package(version: str, body: str) -> SkillPackage:
    return SkillPackage(
        directory_name="demo",
        members=(
            SkillPackageMember(
                path="SKILL.md",
                data=(
                    f"---\nname: demo\nversion: {version}\ndescription: Demo\n"
                    f"required_tools: []\n---\n{body}"
                ).encode(),
            ),
        ),
    )


async def test_skill_repository_versions_and_preserves_pinned_archives() -> None:
    repository = InMemorySkillRepository(
        InMemorySkillPackageStore(),
        SkillPackageValidator(ConservativeTokenEstimator()),
        FixedClock(NOW),
        SequenceIdFactory(),
    )
    first = await repository.install(
        "tenant-a", _package("1.0.0", "first"), SkillSource.OPERATOR, None, None
    )
    second = await repository.install(
        "tenant-a", _package("1.1.0", "second"), SkillSource.OPERATOR, 1, None
    )
    assert second.revision == 2
    assert (await repository.resolve("tenant-a", SkillRef.parse("demo@1"))).body == "first"
    await repository.archive("tenant-a", "demo", 2)
    assert (await repository.resolve("tenant-a", SkillRef.parse("demo"))).revision == 1
    assert (
        await repository.resolve("tenant-a", SkillRef.parse("demo@2"))
    ).status.value == "archived"
    assert first.content_sha256 != second.content_sha256


async def test_skill_repository_refuses_persistent_mcp_prompts() -> None:
    repository = InMemorySkillRepository(
        InMemorySkillPackageStore(),
        SkillPackageValidator(ConservativeTokenEstimator()),
        FixedClock(NOW),
        SequenceIdFactory(),
    )
    with pytest.raises(ConflictError, match="read-only"):
        await repository.install(
            "tenant-a", _package("1.0.0", "remote"), SkillSource.MCP, None, None
        )
