"""Immutable skill package-store contract, bound to every store adapter (skills.md)."""

import os
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.skills.stores import (
    FilesystemSkillPackageStore,
    InMemorySkillPackageStore,
    skill_archive_key,
)
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.domain.errors import NotFoundError, SkillValidationError
from agent_core.domain.skills import SkillPackage, SkillPackageMember
from agent_core.skills.package import SkillPackageValidator

type SkillStore = InMemorySkillPackageStore | FilesystemSkillPackageStore

STORES: list[tuple[str, Callable[[Path], SkillStore]]] = [
    ("memory", lambda _root: InMemorySkillPackageStore()),
    ("filesystem", lambda root: FilesystemSkillPackageStore(root)),
]


@pytest.fixture(params=[factory for _name, factory in STORES], ids=[name for name, _ in STORES])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> SkillStore:
    factory: Callable[[Path], SkillStore] = request.param
    return factory(tmp_path / "skills")


def _archive() -> bytes:
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
    return SkillPackageValidator(ConservativeTokenEstimator()).validate(package).archive


async def test_skill_package_store_is_immutable_and_reads_members(store: SkillStore) -> None:
    archive = _archive()
    stored = await store.put("tenant-a", UUID(int=1), 1, archive)
    assert stored.created
    assert stored.key == f"skills/tenant-a/{UUID(int=1)}/1.tar.zst"
    assert await store.archive_bytes(stored.key) == archive
    assert await store.open_member(stored.key, "references/note.txt") == b"reference"
    repeated = await store.put("tenant-a", UUID(int=1), 1, archive)
    assert repeated.key == stored.key and not repeated.created
    with pytest.raises(ValueError, match="immutable"):
        await store.put("tenant-a", UUID(int=1), 1, b"different")
    assert await store.archive_bytes(stored.key) == archive


async def test_revisions_and_tenants_are_distinct_keys(store: SkillStore) -> None:
    archive = _archive()
    first = await store.put("tenant-a", UUID(int=1), 1, archive)
    second = await store.put("tenant-a", UUID(int=1), 2, archive)
    other = await store.put("tenant-b", UUID(int=1), 1, archive)

    assert len({first.key, second.key, other.key}) == 3
    assert first.created and second.created and other.created


async def test_a_missing_archive_is_not_found_and_delete_is_idempotent(store: SkillStore) -> None:
    stored = await store.put("tenant-a", UUID(int=1), 1, _archive())

    await store.delete(stored.key)
    await store.delete(stored.key)

    with pytest.raises(NotFoundError):
        await store.archive_bytes(stored.key)
    with pytest.raises(NotFoundError):
        await store.open_member(stored.key, "SKILL.md")


@pytest.mark.parametrize(
    "path",
    ["../SKILL.md", "/SKILL.md", "references/../SKILL.md", "references\\note.txt", "missing.md"],
)
async def test_open_member_refuses_escaping_and_absent_paths(store: SkillStore, path: str) -> None:
    stored = await store.put("tenant-a", UUID(int=1), 1, _archive())

    with pytest.raises(SkillValidationError) as refused:
        await store.open_member(stored.key, path)

    assert refused.value.rule in {"package.path", "package.member"}


@pytest.mark.parametrize(
    ("tenant_id", "revision"),
    [("", 1), ("tenant/a", 1), ("tenant\\a", 1), (".", 1), ("..", 1), ("tenant-a", 0)],
)
async def test_archive_keys_are_derived_only_from_safe_platform_values(
    store: SkillStore, tenant_id: str, revision: int
) -> None:
    with pytest.raises(ValueError):
        skill_archive_key(tenant_id, UUID(int=1), revision)
    with pytest.raises(ValueError):
        await store.put(tenant_id, UUID(int=1), revision, _archive())


@pytest.mark.parametrize(
    "key", ["../outside.tar.zst", "/etc/passwd", "skills/../../outside.tar.zst"]
)
async def test_filesystem_store_refuses_keys_that_escape_its_root(tmp_path: Path, key: str) -> None:
    store = FilesystemSkillPackageStore(tmp_path / "skills")
    (tmp_path / "outside.tar.zst").write_bytes(b"outside")

    for operation in (store.archive_bytes(key), store.delete(key)):
        with pytest.raises(ValueError, match="escapes its store"):
            await operation
    assert (tmp_path / "outside.tar.zst").read_bytes() == b"outside"


async def test_filesystem_store_refuses_a_symlink_out_of_its_root(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    (root / "skills").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "1.tar.zst").write_bytes(b"outside")
    (root / "skills" / "tenant-a").symlink_to(outside, target_is_directory=True)
    store = FilesystemSkillPackageStore(root)

    with pytest.raises(ValueError, match="escapes its store"):
        await store.archive_bytes("skills/tenant-a/1.tar.zst")


async def test_filesystem_store_leaves_no_staging_files(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    store = FilesystemSkillPackageStore(root)
    archive = _archive()

    stored = await store.put("tenant-a", UUID(int=1), 1, archive)
    await store.put("tenant-a", UUID(int=1), 1, archive)
    with pytest.raises(ValueError, match="immutable"):
        await store.put("tenant-a", UUID(int=1), 1, b"different")

    directory = (root / stored.key).parent
    assert sorted(os.listdir(directory)) == ["1.tar.zst"]
    assert await FilesystemSkillPackageStore(root).archive_bytes(stored.key) == archive
