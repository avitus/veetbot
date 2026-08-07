"""Immutable skill package-store contract."""

from uuid import UUID

import pytest

from agent_core.adapters.skills.stores import InMemorySkillPackageStore
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.domain.skills import SkillPackage, SkillPackageMember
from agent_core.skills.package import SkillPackageValidator


async def test_skill_package_store_is_immutable_and_reads_members() -> None:
    package = SkillPackage(
        directory_name="demo",
        members=(
            SkillPackageMember(
                path="SKILL.md",
                data=(
                    b"---\nname: demo\nversion: 1.0.0\ndescription: Demo\n"
                    b"required_tools: []\n---\nDo the thing."
                ),
            ),
            SkillPackageMember(path="references/note.txt", data=b"reference"),
        ),
    )
    validated = SkillPackageValidator(ConservativeTokenEstimator()).validate(package)
    store = InMemorySkillPackageStore()
    stored = await store.put("tenant-a", UUID(int=1), 1, validated.archive)
    assert stored.created
    assert await store.archive_bytes(stored.key) == validated.archive
    assert await store.open_member(stored.key, "references/note.txt") == b"reference"
    repeated = await store.put("tenant-a", UUID(int=1), 1, validated.archive)
    assert repeated.key == stored.key and not repeated.created
    with pytest.raises(ValueError, match="immutable"):
        await store.put("tenant-a", UUID(int=1), 1, b"different")
