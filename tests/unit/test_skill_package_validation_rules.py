"""Each skill-package rejection names the rule it broke (skills.md, the validator).

The M8 property gate proves validation is total; these pin which named rule each
documented rejection raises, so a refusal can say what to fix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.domain.errors import SkillValidationError
from agent_core.domain.skills import SkillPackage, SkillPackageMember
from agent_core.skills.package import (
    MAX_PACKAGE_BYTES,
    SkillPackageValidator,
    package_from_directory,
    read_archive_member,
    read_archive_members,
)

VALID_MANIFEST: dict[str, Any] = {
    "name": "demo",
    "version": "1.0.0",
    "description": "Demo procedure.",
    "required_tools": ["workspace.read_text"],
}


def _skill_md(manifest: dict[str, Any] | None = None, body: str = "Do the thing.") -> bytes:
    metadata = yaml.safe_dump(VALID_MANIFEST if manifest is None else manifest, sort_keys=False)
    return f"---\n{metadata}---\n{body}".encode()


def _package(
    *members: SkillPackageMember,
    directory: str = "demo",
    skill_md: bytes | None = None,
) -> SkillPackage:
    root = SkillPackageMember(path="SKILL.md", data=skill_md or _skill_md())
    return SkillPackage(directory_name=directory, members=(root, *members))


def _with(**changes: Any) -> bytes:
    return _skill_md({**VALID_MANIFEST, **changes})


def _validate(package: SkillPackage) -> Any:
    return SkillPackageValidator(ConservativeTokenEstimator()).validate(package)


def test_a_valid_package_is_accepted_with_its_measured_fields() -> None:
    validated = _validate(_package(SkillPackageMember(path="references/a.txt", data=b"a")))

    assert validated.manifest.name == "demo"
    assert validated.body == "Do the thing."
    assert validated.file_count == 2
    assert validated.package_bytes == len(validated.archive)
    assert read_archive_member(validated.archive, "references/a.txt") == b"a"


@pytest.mark.parametrize(
    ("package", "rule"),
    [
        (_package(directory="Demo"), "name.grammar"),
        (_package(directory="-demo"), "name.grammar"),
        (_package(directory="demo-"), "name.grammar"),
        (_package(directory="a" * 65), "name.grammar"),
        (SkillPackage(directory_name="demo", members=()), "package.file_count"),
        (
            _package(*(SkillPackageMember(path=f"m{index}.txt", data=b"m") for index in range(64))),
            "package.file_count",
        ),
        (
            _package(SkillPackageMember(path="big.bin", data=b"x" * MAX_PACKAGE_BYTES)),
            "package.bytes",
        ),
        (_package(SkillPackageMember(path="../escape.txt", data=b"x")), "package.path"),
        (_package(SkillPackageMember(path="/etc/passwd", data=b"x")), "package.path"),
        (_package(SkillPackageMember(path="scripts\\run.sh", data=b"x")), "package.path"),
        (_package(SkillPackageMember(path="scripts/../../x", data=b"x")), "package.path"),
        (_package(SkillPackageMember(path="", data=b"x")), "package.path"),
        (_package(SkillPackageMember(path="./SKILL.md", data=b"x")), "package.duplicate"),
        (
            _package(
                SkillPackageMember(path="notes.txt", data=b"a"),
                SkillPackageMember(path="notes.txt", data=b"b"),
            ),
            "package.duplicate",
        ),
        (_package(SkillPackageMember(path="link", kind="symlink")), "package.symlink"),
        (
            SkillPackage(
                directory_name="demo",
                members=(SkillPackageMember(path="skill.md", data=_skill_md()),),
            ),
            "package.required",
        ),
        (
            SkillPackage(
                directory_name="demo",
                members=(SkillPackageMember(path="docs/SKILL.md", data=_skill_md()),),
            ),
            "package.required",
        ),
        (_package(skill_md=b"\xff\xfe---"), "manifest.encoding"),
        (_package(skill_md=b"name: demo\n---\nbody"), "manifest.front_matter"),
        (_package(skill_md=b"---\nname: demo\nbody"), "manifest.front_matter"),
        (_package(skill_md=b"---\nname: [demo\n---\nbody"), "manifest.yaml"),
        (_package(skill_md=b"---\n- demo\n---\nbody"), "manifest.fields"),
        (_package(skill_md=_with(extra="field")), "manifest.fields"),
        (
            _package(skill_md=_skill_md({"name": "demo", "version": "1.0.0"})),
            "manifest.fields",
        ),
        (_package(skill_md=_skill_md(body="   \n")), "body.length"),
        (_package(skill_md=_with(name="other")), "name.directory"),
        (_package(skill_md=_with(version="1.0")), "version.semver"),
        (_package(skill_md=_with(version="01.0.0")), "version.semver"),
        (_package(skill_md=_with(version="1.0.0-01")), "version.semver"),
        (_package(skill_md=_with(description="")), "description.length"),
        (_package(skill_md=_with(description="two\nlines")), "description.length"),
        (_package(skill_md=_with(description="d" * 501)), "description.length"),
        (
            _package(skill_md=_with(required_tools=[f"a.t{index}" for index in range(11)])),
            "required_tools.count",
        ),
        (
            _package(skill_md=_with(required_tools=["a.b", "a.b"])),
            "required_tools.count",
        ),
        (_package(skill_md=_with(required_tools=["workspace"])), "required_tools.name"),
        (_package(skill_md=_with(required_tools=["Workspace.Read"])), "required_tools.name"),
        (_package(skill_md=_skill_md(body="word " * 20_000)), "body.tokens"),
        (_package(skill_md=_with(description="Procedure " * 45)), "metadata.tokens"),
    ],
)
def test_each_rejection_names_its_rule(package: SkillPackage, rule: str) -> None:
    with pytest.raises(SkillValidationError) as refused:
        _validate(package)

    assert refused.value.rule == rule
    assert rule in str(refused.value)


@pytest.mark.parametrize("version", ["1.0.0", "0.1.0-alpha.1", "2.3.4+build.7", "1.0.0-rc.0"])
def test_semver_forms_are_accepted(version: str) -> None:
    assert _validate(_package(skill_md=_with(version=version))).manifest.version == version


def test_member_order_does_not_change_the_canonical_archive() -> None:
    first = _validate(
        _package(
            SkillPackageMember(path="b.txt", data=b"b"),
            SkillPackageMember(path="a.txt", data=b"a"),
        )
    )
    second = _validate(
        SkillPackage(
            directory_name="demo",
            members=(
                SkillPackageMember(path="a.txt", data=b"a"),
                SkillPackageMember(path="SKILL.md", data=_skill_md()),
                SkillPackageMember(path="b.txt", data=b"b"),
            ),
        )
    )

    assert first.content_sha256 == second.content_sha256
    assert [member.path for member in read_archive_members(first.archive)] == [
        "SKILL.md",
        "a.txt",
        "b.txt",
    ]


@pytest.mark.parametrize(
    ("archive", "rule"),
    [(b"not zstd", "package.corrupt"), (b"", "package.corrupt")],
)
def test_corrupt_archives_are_refused_by_name(archive: bytes, rule: str) -> None:
    with pytest.raises(SkillValidationError) as member:
        read_archive_member(archive, "SKILL.md")
    with pytest.raises(SkillValidationError) as members:
        read_archive_members(archive)

    assert member.value.rule == members.value.rule == rule


def test_a_directory_symlink_is_carried_as_a_member_and_refused(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    root.mkdir()
    (root / "SKILL.md").write_bytes(_skill_md())
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")

    package = package_from_directory(root)

    link = next(member for member in package.members if member.path == "link.txt")
    assert (link.kind, link.data) == ("symlink", b"")
    with pytest.raises(SkillValidationError) as refused:
        _validate(package)
    assert refused.value.rule == "package.symlink"


def test_a_package_root_must_be_a_real_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "SKILL.md").write_bytes(_skill_md())
    (tmp_path / "demo").symlink_to(real, target_is_directory=True)
    (tmp_path / "file").write_text("x", encoding="utf-8")

    for root in (tmp_path / "demo", tmp_path / "file", tmp_path / "missing"):
        with pytest.raises(SkillValidationError) as refused:
            package_from_directory(root)
        assert refused.value.rule == "package.directory"


def test_directory_reads_stop_at_the_package_byte_bound(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    root.mkdir()
    (root / "SKILL.md").write_bytes(_skill_md())
    (root / "big.bin").write_bytes(b"x" * MAX_PACKAGE_BYTES)

    with pytest.raises(SkillValidationError) as refused:
        package_from_directory(root)

    assert refused.value.rule == "package.bytes"
